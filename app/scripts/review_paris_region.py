"""Compare active local candidates with one retained official region response.

Read-only: absence from a response is never a closure or deletion instruction.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.store import Store
from app.services.map_verification import location_address_key
from app.services.paris_store_identity import paris_capture_rows


def compare_candidate(store, rows):
    matches = [r for r in rows if (r['official_name'] if r['official_name'].endswith('점')
        else r['official_name'] + '점') == store.branch]
    result = {'store_id': store.id, 'branch': store.branch, 'address': store.address,
        'official_name_matches': len(matches), 'database_changed': False}
    if len(matches) != 1:
        return {**result, 'status': 'pending', 'reason': 'official_name_missing_or_ambiguous'}
    row = matches[0]
    # Only fully qualified existing addresses can match without a separate review.
    same_address = location_address_key(store.address) == location_address_key(row['address'])
    return {**result, 'status': 'needs_place_check' if same_address else 'needs_address_review',
        'official_id': row['official_id'], 'official_name': row['official_name'],
        'official_address': row['address']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(args.capture_dir)
    raw = (root / 'search.html').read_text(encoding='utf-8')
    area_raw = (root / 'areas.json').read_text(encoding='utf-8')
    receipt = json.loads((root / 'receipt.json').read_text(encoding='utf-8'))
    if receipt.get('mode') != 'region' or receipt['region']['province'] != '서울' or receipt['region']['district'] != '동대문구':
        raise ValueError('unsupported_review_region')
    rows = paris_capture_rows(raw, receipt, datetime.now(timezone.utc), area_raw)
    with SessionLocal() as db:
        stores = db.scalars(select(Store).where(Store.brand == '파리바게뜨', Store.is_active.is_(True))).all()
        comparisons = [compare_candidate(store, rows) for store in stores]
    result = {'region': receipt['region'], 'captured_at': receipt['captured_at'],
        'response_sha256': receipt['response_sha256'], 'area_response_sha256': receipt['area_response_sha256'],
        'official_records': len(rows), 'official_stores': rows, 'candidates': comparisons,
        'database_changed': False, 'complete': False,
        'missing_is_closure_evidence': False}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('official_records', 'candidates', 'database_changed')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
