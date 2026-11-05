"""Collect official API snapshots and validate a bounded policy batch, with backup."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

import httpx
from sqlalchemy import select, func

from app.core.db import SessionLocal, engine
from app.crawlers.public import OnjungchoungnyeonCrawler
from app.crawlers.registry import run_crawler
from app.models.scholarship import Scholarship
from app.services.policy_sources import CODEBOOK, parse_codebook, parse_display_rules
from app.services.policy_validation import run_batch, rebuild_archived_snapshots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--ids', nargs='+', type=int)
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--rebuild-archives', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=args.output / 'pipeline.log', level=logging.WARNING, encoding='utf-8')
    result = {'apply': args.apply, 'scope': 'official_api_text', 'limit': args.limit}
    with SessionLocal() as db:
        if args.apply:
            if engine.dialect.name != 'sqlite':
                raise ValueError('this apply runner requires local SQLite for backup')
            backup = Path('data/backups') / ('before-policy-validation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(engine.url.database) as source, sqlite3.connect(backup) as target:
                source.backup(target)
            result['backup'] = str(backup)
            if args.refresh:
                url = 'https://www.youthcenter.go.kr/youthPolicy/ythPlcyTotalSearch'
                response = httpx.get(url, timeout=25, follow_redirects=True)
                response.raise_for_status()
                codes = parse_codebook(response.content)
                if not codes:
                    raise ValueError('official condition code labels unavailable')
                (args.output / 'official-search.html').write_bytes(response.content)
                book = {'source_url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                        'sha256': hashlib.sha256(response.content).hexdigest(), 'codes': codes,
                        'rules': parse_display_rules(response.content)}
                CODEBOOK.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding='utf-8')
                result['codebook_count'] = len(codes)
                result['collection'] = run_crawler(OnjungchoungnyeonCrawler(), db)
                print(json.dumps({'collection': result['collection'], 'codebook_count': len(codes)}, ensure_ascii=False), flush=True)
            if args.rebuild_archives:
                result['archive_rebuild'] = rebuild_archived_snapshots(db)
            if result.get('collection', {}).get('skipped'):
                result['validation'] = {'processed': 0, 'passed': 0, 'needs_review': 0, 'skipped': 'source_refresh_failed'}
            else:
                result['validation'] = run_batch(db, limit=args.limit, ids=args.ids, retry=args.retry)
        rows = db.scalars(select(Scholarship).where(Scholarship.source_platform == 'onjungchoungnyeon')).all()
        from collections import Counter
        result['validation_counts'] = dict(Counter((r.eligibility.get('policy_validation') or {}).get('status', 'unverified') for r in rows))
        result['count'] = len(rows)
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
