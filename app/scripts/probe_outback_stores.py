"""Retain one public official Outback locator response; never update stores."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx
from app.scripts.probe_paris_stores import read_bounded_response
from app.services.outback_store_identity import SOURCE, SEARCH_SOURCE, parse_outback_stores, parse_outback_search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--query', help='Search one branch name instead of retaining the locator page')
    args = parser.parse_args()
    if args.query is not None and (not args.query.strip() or len(args.query) > 50):
        parser.error('invalid_store_query')
    endpoint = SEARCH_SOURCE if args.query else SOURCE
    with httpx.Client(trust_env=False, timeout=30, follow_redirects=False,
            headers={'User-Agent': 'PayPickMapSync/1.0'}) as client:
        robots = client.get('https://m.outback.co.kr/robots.txt')
        policy = RobotFileParser()
        if robots.status_code == 404:
            policy.parse([])
        else:
            robots.raise_for_status()
            policy.parse(robots.text.splitlines())
        if not policy.can_fetch('PayPickMapSync/1.0', endpoint):
            raise ValueError('source_disallows_fetch')
        time.sleep(2)
        params = {'sdCd': '', 'sggCd': '', 'searchKeyword': args.query} if args.query else None
        with client.stream('GET', endpoint, params=params) as response:
            raw = read_bounded_response(response)
            source_url = str(response.url)
    rows = parse_outback_search(raw) if args.query else parse_outback_stores(raw)
    receipt = {'source_url': source_url, 'query': args.query, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'complete': False,
        'records': len(rows)}
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    (root / ('search.json' if args.query else 'locator.html')).write_text(raw, encoding='utf-8')
    (root / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'records': len(rows), 'complete': False}))


if __name__ == '__main__':
    main()
