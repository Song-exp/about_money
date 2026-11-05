"""Build read-only store matching candidates from an operator-captured public response."""
import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.store import Store
from app.services.issuer_merchant_matching import parse_shinhan_directory, merchant_match, DIRECTORY_URL, LOOKUP_URL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--captured-at', required=True, type=datetime.fromisoformat)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.captured_at.tzinfo is None or not now-timedelta(days=7) <= args.captured_at <= now:
        parser.error('capture time must have timezone and be within the last 7 days')
    if not args.input.is_file() or args.input.stat().st_size > 2_000_000:
        parser.error('a local public response of at most 2 MB is required')
    raw = args.input.read_bytes()
    try:
        directory = parse_shinhan_directory(raw)
    except ValueError as exc:
        parser.error(str(exc))
    with SessionLocal() as db:
        stores = db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.id)).all()
        matches = [merchant_match(store, directory) for store in stores]
    result = {'scope': 'operator_supplied_lookup_review_not_approval', 'source_url': DIRECTORY_URL,
        'lookup_url': LOOKUP_URL, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
        'declared_captured_at': args.captured_at.isoformat(), 'capture_claim_verified': False,
        'complete_for_submitted_query_only': directory['complete'], 'merchant_records': len(directory['records']),
        'stores_checked': len(matches), 'unique_exact_matches': sum(m['status']=='unique_exact_match' for m in matches),
        'public_approval': False, 'matches': matches}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key: value for key,value in result.items() if key != 'matches'},ensure_ascii=False))


if __name__ == '__main__':
    main()
