"""Apply explicitly reviewed official addresses to local stores with an audit trail."""
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.store import Store
from app.models.map_verification import MapReviewEvent
from app.services.map_verification import location_hash, normalize_location_text


def parse_cj_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = soup.select('.store_info')
    if len(blocks) != 1:
        raise ValueError('official_store_block_missing_or_ambiguous')
    name, address = blocks[0].select_one('h2.h2_tit'), blocks[0].select_one('p.tit_info')
    if not name or not address:
        raise ValueError('official_identity_missing')
    return name.get_text(' ', strip=True), address.get_text(' ', strip=True)


def source_provider(review):
    parsed = urlparse(review['source_url'])
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
        raise ValueError('unsupported_official_source')
    if parsed.hostname in {'www.cjone.com', 'm.cjone.com'} and parsed.path == '/cjmmobile/store/storeAreaDetail.do':
        return 'cj'
    if parsed.hostname == 'www.starbucks.co.kr' and parsed.path == '/store/store_map.do':
        if review['brand'] != '스타벅스' or parse_qs(parsed.query).get('in_biz_cd') != [review.get('official_id')]:
            raise ValueError('official_store_code_mismatch')
        return 'starbucks'
    if parsed.hostname == 'www.tlj.co.kr' and parsed.path == '/store/search.asp':
        if review['brand'] != '뚜레쥬르' or parse_qs(parsed.query).get('store_cd') != [review.get('official_id')]:
            raise ValueError('official_store_code_mismatch')
        if not str(review.get('official_id', '')).isdigit():
            raise ValueError('official_store_code_missing')
        return 'tlj'
    raise ValueError('unsupported_official_detail')


def parse_starbucks_identity(raw, review):
    rows = json.loads(raw).get('list', [])
    matches = [row for row in rows if str(row.get('s_biz_code')) == review['official_id']]
    if len(matches) != 1 or matches[0].get('s_name') != review['official_name']:
        raise ValueError('official_store_identity_mismatch')
    address = matches[0].get('doro_address')
    if not address:
        raise ValueError('official_address_missing')
    # Only the reviewed mapping is accepted; no broad suffix/brand alias matching.
    if review['branch'] != review['official_name'] + '점':
        raise ValueError('official_branch_mapping_mismatch')
    return f"스타벅스 {review['branch']}", address


def parse_tlj_identity(html, review):
    soup = BeautifulSoup(html, 'html.parser')
    code = review['official_id']
    names = soup.select(f'#listName_{code}')
    addresses = soup.select(f'#listAddrD_{code}')
    if len(names) != 1 or len(addresses) != 1:
        raise ValueError('official_store_identity_missing_or_ambiguous')
    name = names[0].get_text(' ', strip=True)
    if normalize_location_text(name) != normalize_location_text(f"뚜레쥬르 {review['official_name']}"):
        raise ValueError('official_store_identity_mismatch')
    if review['branch'] != review['official_name'] + '점':
        raise ValueError('official_branch_mapping_mismatch')
    return f"뚜레쥬르 {review['branch']}", addresses[0].get_text(' ', strip=True)


def apply_review(db, review, html, fetched_at):
    provider = source_provider(review)
    if provider == 'starbucks':
        name, address = parse_starbucks_identity(html, review)
    elif provider == 'tlj':
        name, address = parse_tlj_identity(html, review)
    else:
        name, address = parse_cj_identity(html)
    if normalize_location_text(name) != normalize_location_text(f"{review['brand']} {review['branch']}"):
        raise ValueError('official_store_identity_mismatch')
    if normalize_location_text(address) != normalize_location_text(review['new_address']):
        raise ValueError('official_address_changed')
    row = db.scalar(select(Store).where(Store.brand == review['brand'], Store.branch == review['branch']))
    if not row:
        raise ValueError('store_missing')
    if row.address == review['new_address']:
        return {'id': row.id, 'status': 'unchanged'}
    if row.address != review['expected_old_address']:
        raise ValueError('store_address_changed_since_review')
    before = {'address': row.address, 'identity_hash': location_hash(row)}
    row.address = review['new_address']
    db.add(MapReviewEvent(kind='store', item_id=row.id, action='address_review', at=fetched_at,
        payload={'before': before, 'after': {'address': row.address, 'identity_hash': location_hash(row)},
                 'source_url': review['source_url'], 'source_hash': hashlib.sha256(html.encode()).hexdigest(),
                 'fetched_at': fetched_at.isoformat(), 'official_name': name, 'official_address': address,
                 'reviewer': 'official_detail_identity_review'}))
    # Existing location/evidence hashes become stale; never inherit verification.
    return {'id': row.id, 'status': 'address_corrected', 'before': before['address'], 'after': row.address}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reviews', default='data/map_store_address_reviews.json')
    parser.add_argument('--output', default='reports/map-improvement-20261013-address-review')
    parser.add_argument('--brand', help='Apply only reviewed entries for this exact brand')
    args = parser.parse_args()
    if settings.is_production or engine.dialect.name != 'sqlite':
        raise RuntimeError('Local SQLite address review only')
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    reviews = json.loads(Path(args.reviews).read_text(encoding='utf-8'))['reviews']
    if args.brand:
        reviews = [review for review in reviews if review['brand'] == args.brand]
        if not reviews:
            parser.error('no reviewed entries for requested brand')
    backup = Path('_backup/map-address-review')
    backup.mkdir(parents=True, exist_ok=True)
    backup_file = backup / f'before-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
    with sqlite3.connect(engine.url.database) as src, sqlite3.connect(backup_file) as dest:
        src.backup(dest)
    results = []
    with SessionLocal() as db, httpx.Client(timeout=20, follow_redirects=False) as client:
        for index, review in enumerate(reviews):
            provider = source_provider(review)
            if provider == 'starbucks':
                from app.scripts.probe_starbucks_stores import capture_starbucks
                content = capture_starbucks(review['official_name'], review['source_url'])
            else:
                response = client.get(review['source_url'])
                response.raise_for_status()
                content = response.text
            fetched_at = datetime.now(timezone.utc)
            raw = root / f'official-address-{index}.{"json" if provider == "starbucks" else "html"}'
            raw.write_text(content, encoding='utf-8')
            results.append({**apply_review(db, review, content, fetched_at), 'snapshot': raw.name})
        db.commit()
    output = {'backup': str(backup_file), 'results': results}
    (root / 'address-reviews.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
