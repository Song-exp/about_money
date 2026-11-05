"""Capture bounded official Mega name-search and map-area responses."""
import argparse
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx
from app.scripts.probe_paris_stores import read_bounded_response


def capture_mega(query, branch, *, client=None):
    if not query.strip() or len(query) > 50 or not branch.strip() or len(branch) > 150:
        raise ValueError('invalid_store_query')
    base = 'https://www.mega-mgccoffee.com'
    page_url = base + '/store/find/'
    context = nullcontext(client) if client is not None else httpx.Client(
        trust_env=False, timeout=30, follow_redirects=False, headers={'User-Agent': 'PayPickMapSync/1.0'})
    with context as client:
        response = client.get(base + '/robots.txt')
        policy = RobotFileParser()
        if response.status_code == 404:
            policy.parse([])
        else:
            response.raise_for_status()
            policy.parse(response.text.splitlines())
        for path in ('', 'store_search.php', 'store.php'):
            if not policy.can_fetch('PayPickMapSync/1.0', page_url + path):
                raise ValueError('source_disallows_fetch')
        # Same read-only GET routes and parameters used by the public page.
        time.sleep(2)
        with client.stream('GET', page_url + 'store_search.php', params={'store_search': query}) as search:
            search_raw = read_bounded_response(search)
            search_url = str(search.url)
        time.sleep(2)
        with client.stream('GET', page_url + 'store.php', params={'lat': '37.59', 'lng': '127.06'}) as response:
            raw = json.dumps(json.loads(read_bounded_response(response)), ensure_ascii=False)
            response_url = str(response.url)
        return raw, search_raw, {'query': query, 'branch': branch, 'source_url': page_url,
            'search_url': search_url, 'response_url': response_url,
            'capture_mode': 'public_search_and_map_area',
            'complete': False, 'map_center': {'lat': 37.59, 'lng': 127.06},
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'search_sha256': hashlib.sha256(search_raw.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raw, search_raw, receipt = capture_mega(args.query, args.branch)
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'positions.json').write_text(raw, encoding='utf-8')
    (root / 'search.html').write_text(search_raw, encoding='utf-8')
    (root / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'branch': args.branch, 'positions': len(json.loads(raw).get('positions', [])),
        'complete': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
