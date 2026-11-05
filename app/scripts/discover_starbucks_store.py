"""Review one retained official Starbucks identity against a fresh Kakao lookup.

Default is read-only. Only --apply can register a unique cross-checked store;
existing stores and their offers are never renamed, merged or overwritten.
"""
import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.map_verification import MapLocation, MapReviewEvent
from app.models.store import Store, StoreCategory
from app.scripts.resolve_map_locations import SDKLoadError, exact_matches, open_sdk_page, query_place_candidates
from app.scripts.review_store_addresses import parse_starbucks_identity, source_provider
from app.services.map_verification import location_hash


def official_candidate(raw, receipt, official_id, official_name, now):
    """Bind the selected identity to a fresh retained response, not a fuzzy name."""
    if receipt['source_url'] != 'https://www.starbucks.co.kr/store/store_map.do':
        raise ValueError('unsupported_capture_source')
    if hashlib.sha256(raw.encode('utf-8')).hexdigest() != receipt['response_sha256']:
        raise ValueError('capture_hash_mismatch')
    captured_at = datetime.fromisoformat(receipt['captured_at'])
    if captured_at.tzinfo is None or not timedelta(0) <= now - captured_at <= timedelta(days=7):
        raise ValueError('capture_not_recent')
    review = {'brand': '스타벅스', 'branch': official_name + '점',
        'official_id': official_id, 'official_name': official_name,
        'source_url': f'https://www.starbucks.co.kr/store/store_map.do?in_biz_cd={official_id}'}
    if not official_id.isdigit():
        raise ValueError('invalid_official_id')
    source_provider(review)
    name, address = parse_starbucks_identity(raw, review)
    if not address.startswith('서울특별시 동대문구 '):
        raise ValueError('outside_review_region')
    row = next(row for row in json.loads(raw)['list'] if str(row.get('s_biz_code')) == official_id)
    lat, lng = float(row['lat']), float(row['lot'])
    if not (math.isfinite(lat) and math.isfinite(lng) and 37 <= lat <= 38 and 126 <= lng <= 128):
        raise ValueError('invalid_official_coordinates')
    return {**review, 'name': name, 'address': address, 'lat': lat, 'lng': lng,
        'captured_at': captured_at.isoformat(), 'response_sha256': receipt['response_sha256']}


def validated_place(candidate, documents, lookup_status):
    matches = exact_matches(documents, candidate['name'], candidate['address'])
    if lookup_status != 'OK' or len(matches) != 1:
        raise ValueError('no_unique_exact_place')
    match = matches[0]
    place_id = str(match['id'])
    url = match['place_url'].replace('http://', 'https://', 1)
    if not place_id.isdigit() or url != f'https://place.map.kakao.com/{place_id}':
        raise ValueError('invalid_place_source')
    lat, lng = float(match['y']), float(match['x'])
    if not (math.isfinite(lat) and math.isfinite(lng) and 37 <= lat <= 38 and 126 <= lng <= 128):
        raise ValueError('invalid_place_coordinates')
    # Even an exact text match cannot justify a distant pin.
    if abs(lat - candidate['lat']) > .002 or abs(lng - candidate['lng']) > .002:
        raise ValueError('official_place_coordinates_disagree')
    return match, place_id, url, lat, lng


def register_match(db, candidate, documents, lookup_status, checked_at):
    match, place_id, url, lat, lng = validated_place(candidate, documents, lookup_status)
    provider = candidate.get('provider', 'starbucks')
    providers = {'starbucks': ('스타벅스', StoreCategory.CAFE, 'S', '#00704A'),
        'mega': ('메가MGC커피', StoreCategory.CAFE, 'M', '#3a1d1d'),
        'paris': ('파리바게뜨', StoreCategory.BAKERY, 'P', '#00529B'),
        'outback': ('아웃백', StoreCategory.RESTAURANT, 'O', '#8b1a1a')}
    if provider not in providers:
        raise ValueError('unsupported_store_provider')
    brand, category, mark, color = providers[provider]
    if candidate['brand'] != brand:
        raise ValueError('official_provider_brand_mismatch')
    row = db.scalar(select(Store).where(Store.brand == candidate['brand'], Store.branch == candidate['branch']))
    prior = db.scalars(select(MapReviewEvent).where(MapReviewEvent.kind == 'store',
        MapReviewEvent.action == 'official_store_discovery')).all()
    for event in prior:
        if (event.payload.get('official_id') == candidate['official_id']
                and event.payload.get('provider') == provider
                and (row is None or event.item_id != row.id)):
            raise ValueError('official_id_already_bound')
    if row:
        if row.address != candidate['address'] or not row.is_active:
            raise ValueError('existing_store_requires_separate_review')
        location = db.get(MapLocation, ('store', row.id))
        if (not location or location.identity_hash != location_hash(row)
                or location.place_id != place_id):
            raise ValueError('existing_store_requires_separate_review')
        # An identical import does not manufacture a new observation timestamp.
        return {'status': 'unchanged', 'store_id': row.id}
    bound = db.scalar(select(MapLocation).where(MapLocation.kind == 'store', MapLocation.place_id == place_id))
    if bound:
        raise ValueError('place_already_bound')
    row = Store(brand=candidate['brand'], branch=candidate['branch'], address=candidate['address'],
        category=category, lat=lat, lng=lng, is_active=True, mark=mark, color=color)
    db.add(row)
    db.flush()
    db.add(MapLocation(kind='store', item_id=row.id, identity_hash=location_hash(row),
        place_id=place_id, lat=lat, lng=lng, source_url=url,
        checked_at=checked_at, reviewer='exact_name_address_match'))
    db.add(MapReviewEvent(kind='store', item_id=row.id, action='official_store_discovery', at=checked_at,
        payload={**candidate, 'provider': provider, 'place_id': place_id,
            'place_source_url': url, 'benefit_approved': False}))
    db.add(MapReviewEvent(kind='store', item_id=row.id, action='location_match', at=checked_at,
        payload={'place_id': place_id, 'name': match['place_name'], 'region': '서울 동대문구',
            'source_address': row.address, 'qualified_address': row.address,
            'address': match.get('road_address_name') or match['address_name']}))
    return {'status': 'registered', 'store_id': row.id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--official-id', required=True)
    parser.add_argument('--official-name', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if settings.is_production or engine.dialect.name != 'sqlite':
        raise RuntimeError('Local SQLite review only')
    manifest = Path(args.manifest)
    receipts = [r for r in json.loads(manifest.read_text(encoding='utf-8')) if r['query'] == args.query]
    if len(receipts) != 1:
        raise ValueError('ambiguous_capture_receipt')
    receipt = receipts[0]
    raw_path = (manifest.parent / receipt['raw_ref']).resolve()
    if raw_path.parent != manifest.parent.resolve():
        raise ValueError('capture_path_outside_manifest')
    candidate = official_candidate(raw_path.read_text(encoding='utf-8'), receipt,
        args.official_id, args.official_name, datetime.now(timezone.utc))
    review_candidate(candidate, args.output, args.apply)


def review_candidate(candidate, output_path, apply=False):
    """Fresh SDK comparison shared by explicitly supported official providers."""
    if settings.is_production or engine.dialect.name != 'sqlite':
        raise RuntimeError('Local SQLite review only')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            page = open_sdk_page(browser, standalone=True)
            documents, status, attempts = query_place_candidates(page, candidate['name'],
                candidate['name'] + ' 서울 동대문구')
        except SDKLoadError as exc:
            documents, status = [], 'SDK_UNAVAILABLE'
            attempts = [{'status': status, 'diagnostics': exc.diagnostics}]
        finally:
            browser.close()
    checked_at = datetime.now(timezone.utc)
    matches = exact_matches(documents, candidate['name'], candidate['address'])
    result = {'candidate': candidate, 'checked_at': checked_at.isoformat(), 'lookup_status': status,
        'lookup_attempts': attempts, 'documents': documents, 'exact_matches': len(matches),
        'applied': False, 'benefit_approved': False}
    try:
        validated_place(candidate, documents, status)
        result['match_validated'] = True
    except ValueError as exc:
        result.update(match_validated=False,
            reason='source_lookup_failed' if status not in ('OK', 'ZERO_RESULT') else str(exc))
    if apply and result['match_validated']:
        backup = Path('_backup/map-location-review') / f'before-discovery-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(engine.url.database) as src, sqlite3.connect(backup) as dest:
            src.backup(dest)
        with SessionLocal() as db:
            result.update(register_match(db, candidate, documents, status, checked_at))
            db.commit()
        result.update(applied=True, backup=str(backup))
    else:
        result['status'] = 'preview' if result['match_validated'] else 'pending'
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'applied', 'exact_matches')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
