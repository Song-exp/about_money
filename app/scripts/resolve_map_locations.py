"""Verify unique exact name/address matches with the local registered Kakao SDK."""
import json
import argparse
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse
from sqlalchemy import select
from app.core.db import SessionLocal, engine
from app.core.config import settings
from app.models.store import Store
from app.models.local_benefit import LocalBenefitMerchant
from app.models.map_verification import MapLocation, MapReviewEvent
from app.services.map_verification import location_hash
from app.services.map_verification import normalize_location_text as normalize, location_address_key as address_key


def expected_identity(row, kind, region):
    if kind == 'local':
        return row.merchant_name, row.address, row.search_query
    name = f'{row.brand} {row.branch}'
    address = (row.address or '').strip()
    # Only an explicitly supplied region can qualify incomplete legacy roads.
    # Campus building labels and descriptive addresses cannot identify a pin.
    if re.fullmatch(r'[가-힣A-Za-z0-9·]+(?:로|길)\s+\d+(?:-\d+)?', address):
        address = f'{region} {address}' if region else None
    elif not re.match(r'^(?:서울특별시|서울시|서울)\s+[가-힣]+구\s+', address):
        address = None
    return name, address, f'{name} {region or ""}'.strip()


def exact_matches(documents, name, address):
    if not address:
        return []
    return [d for d in documents if normalize(d['place_name']) == normalize(name)
            and address_key(d.get('road_address_name') or d['address_name']) == address_key(address)]


def query_place_candidates(page,name,query):
    attempts=[]
    documents=[]
    status='NOT_REQUESTED'
    for keyword in dict.fromkeys([query,name]):
        page.wait_for_timeout(2000)
        response=page.evaluate('''query => new Promise(resolve => {
            const timeout=setTimeout(()=>resolve({status:'TIMEOUT',documents:[]}),15000);
            new kakao.maps.services.Places().keywordSearch(query,(items,status)=>{
                clearTimeout(timeout);resolve({status:String(status),documents:status==='OK'?items:[]});
            });
        })''',keyword)
        status=response['status']
        documents=response['documents']
        attempts.append({'query':keyword,'status':status,'candidates':len(documents)})
        if status!='ZERO_RESULT':
            break
    return documents,status,attempts


class SDKLoadError(RuntimeError):
    def __init__(self, diagnostics):
        super().__init__('public_map_sdk_unavailable')
        self.diagnostics = diagnostics


def open_sdk_page(browser, standalone=False):
    """Load the public SDK on the project's registered localhost origin."""
    page = browser.new_page()
    diagnostics = []

    def record_response(response):
        host = urlparse(response.url).hostname or ''
        if host.endswith(('.kakao.com', '.daumcdn.net')) and response.status >= 400:
            diagnostics.append({'host': host, 'status': response.status})

    def record_failure(request):
        host = urlparse(request.url).hostname or ''
        if host.endswith(('.kakao.com', '.daumcdn.net')):
            diagnostics.append({'host': host, 'status': 'network_error'})

    page.on('response', record_response)
    page.on('requestfailed', record_failure)
    if standalone:
        probe_url = 'http://localhost:5173/__map-location-probe__'
        page.route(probe_url, lambda route: route.fulfill(status=200, content_type='text/html',
            body='<html><head><title>Local place verification</title></head><body></body></html>'))
        page.goto(probe_url, wait_until='domcontentloaded')
    else:
        page.goto('http://localhost:5173', wait_until='domcontentloaded')
    key = None
    for filename in ('frontend/.env', 'frontend/.env.local', 'frontend/.env.development', 'frontend/.env.development.local'):
        path = Path(filename)
        if path.exists():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if line.startswith('VITE_KAKAO_MAP_KEY='):
                    key = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not key:
        raise RuntimeError('public_map_key_missing')
    page.evaluate('''key => {
        const script=document.createElement('script');
        script.src='https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&libraries=services&appkey='+encodeURIComponent(key);
        script.onload=()=>kakao.maps.load(()=>{});
        document.head.append(script);
    }''', key)
    try:
        page.wait_for_function('!!window.kakao?.maps?.services?.Places', timeout=30000)
    except PlaywrightTimeoutError:
        # Only host/status are retained; SDK URLs can contain application keys.
        raise SDKLoadError(diagnostics or [{'status': 'initialization_timeout'}]) from None
    return page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='reports/map-improvement-20261013/locations-live.json')
    parser.add_argument('--kind', choices=['local', 'store'], default='local')
    parser.add_argument('--region', help='Explicit region for legacy road-only addresses, e.g. 서울 동대문구')
    parser.add_argument('--store-ids', nargs='+', type=int, help='Only review these store IDs')
    parser.add_argument('--standalone-sdk',action='store_true',help='Use a minimal localhost SDK page without the frontend server')
    args = parser.parse_args()
    if args.store_ids and args.kind != 'store':
        parser.error('--store-ids requires --kind store')
    if settings.is_production or engine.dialect.name != 'sqlite':
        raise RuntimeError('Local SQLite location review only')
    if args.region and not re.fullmatch(r'서울\s+[가-힣]+구', args.region):
        parser.error('--region must be a Seoul district')
    backup = Path('_backup/map-location-review')
    backup.mkdir(parents=True, exist_ok=True)
    backup_file = backup / f'before-{args.kind}-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
    with sqlite3.connect(engine.url.database) as src, sqlite3.connect(backup_file) as dest:
        src.backup(dest)
    result = {'kind': args.kind, 'region': args.region, 'backup': str(backup_file), 'approved': 0, 'pending': 0, 'items': []}
    with SessionLocal() as db, sync_playwright() as playwright:
        rows = db.scalars(select(Store).where(Store.is_active.is_(True)) if args.kind == 'store'
            else select(LocalBenefitMerchant).where(LocalBenefitMerchant.source_type=='geojimap')).all()
        if args.store_ids:
            rows = [row for row in rows if row.id in args.store_ids]
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = open_sdk_page(browser, args.standalone_sdk)
        for row in rows:
            name, address, query = expected_identity(row, args.kind, args.region)
            documents,lookup_status,attempts=query_place_candidates(page,name,query)
            matches = exact_matches(documents, name, address)
            approved = lookup_status=='OK' and len(matches) == 1
            result['approved' if approved else 'pending'] += 1
            result['items'].append({'id': row.id, 'name': name, 'candidates': len(documents),
                                    'exact_matches': len(matches), 'status': 'verified' if approved else 'pending',
                                    'source_address': row.address,
                                    'qualified_address': address,
                                    'lookup_status':lookup_status,'lookup_attempts':attempts,
                                    'reason': None if approved else ('source_lookup_failed' if lookup_status not in ('OK','ZERO_RESULT')
                                        else 'address_not_specific' if not address else 'no_unique_exact_match'),
                                    'candidate_identity': [{'place_id': d['id'], 'name': d['place_name'], 'address': d.get('road_address_name') or d['address_name']} for d in documents]})
            print(json.dumps({'id':row.id,'status':'verified' if approved else 'pending',
                'candidates':len(documents)},ensure_ascii=False),flush=True)
            if approved:
                document = matches[0]
                location = db.get(MapLocation, (args.kind, row.id)) or MapLocation(kind=args.kind, item_id=row.id)
                location.identity_hash, location.place_id = location_hash(row), document['id']
                location.lat, location.lng = float(document['y']), float(document['x'])
                location.source_url = document['place_url'].replace('http://', 'https://')
                location.reviewer, location.checked_at = 'exact_name_address_match', datetime.now(timezone.utc)
                db.add(location)
                db.add(MapReviewEvent(kind=args.kind, item_id=row.id, action='location_match', at=location.checked_at,
                    payload={'place_id': document['id'], 'name': document['place_name'],
                             'region': args.region, 'source_address': row.address, 'qualified_address': address,
                             'address': document.get('road_address_name') or document['address_name']}))
                db.commit()
        browser.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'approved': result['approved'], 'pending': result['pending']}, ensure_ascii=False))


if __name__ == '__main__': main()
