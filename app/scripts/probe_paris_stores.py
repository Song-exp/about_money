"""Capture one bounded public Paris Baguette branch-name search."""
import argparse
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import httpx
from app.services.robots_policy import prefix_robots_allowed
from app.services.paris_store_identity import parse_paris_search, select_paris_region


MAX_RESPONSE_BYTES = 2_000_000


def capture_paris(query, *, client=None, region=None):
    if (not region and not query.strip()) or len(query) > 50 or (region and query):
        raise ValueError('invalid_store_query')
    base = 'https://www.paris.co.kr'
    endpoint = base + '/wp-admin/admin-ajax.php'
    context = nullcontext(client) if client is not None else httpx.Client(
        trust_env=False, timeout=30, follow_redirects=False,
        headers={'User-Agent': 'PayPickMapSync/1.0'})
    with context as client:
        robots = client.get(base + '/robots.txt')
        if robots.status_code == 404:
            policy_text = ''
        else:
            robots.raise_for_status()
            policy_text = robots.text
        if not prefix_robots_allowed(policy_text, 'PayPickMapSync/1.0', endpoint):
            raise ValueError('source_disallows_fetch')
        params = {'action': 'pb_store_get_list', 'type': '#search', 'keyword': query,
            'area1': '', 'area2': '', **{key: 'false' for key in
                ('is_delivery', 'is_cafe', 'is_parking', 'is_pickup', 'is_catering', 'is_hybrid')}}
        region_metadata = {}
        if region:
            time.sleep(2)
            with client.stream('GET', endpoint, params={'action': 'pb_store_get_area'}) as response:
                area_raw = read_bounded_response(response)
            selected = select_paris_region(area_raw, *region)
            params.update(type='#around', area1=selected['province_id'], area2=selected['district_id'])
            region_metadata = {'mode': 'region', 'region': selected, 'area_response': area_raw,
                'area_response_sha256': hashlib.sha256(area_raw.encode()).hexdigest()}
        time.sleep(2)
        # Stop downloading at the limit instead of checking after a full read.
        with client.stream('GET', endpoint, params=params) as response:
            raw = read_bounded_response(response)
            response_url = str(response.url)
    try:
        rows = parse_paris_search(raw)
        interpretation = {'parse_status': 'parsed', 'records': len(rows)}
    except ValueError as exc:
        interpretation = {'parse_status': 'needs_review', 'parse_error': str(exc), 'records': None}
    return raw, {**interpretation, **region_metadata, 'query': query, 'source_url': base + '/store', 'response_url': response_url,
        'captured_at': datetime.now(timezone.utc).isoformat(), 'complete': False,
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}


def read_bounded_response(response):
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError('official_response_too_large')
        chunks.append(chunk)
    return b''.join(chunks).decode(response.encoding or 'utf-8', errors='strict')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--query')
    scope.add_argument('--region', nargs=2, metavar=('PROVINCE', 'DISTRICT'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    try:
        raw, receipt = capture_paris('', region=args.region) if args.region else capture_paris(args.query)
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        # Keep prior successful snapshots intact. Failure is never an empty list
        # of stores, a closure signal, or a successful observation timestamp.
        reason = 'source_http_error' if isinstance(exc, httpx.HTTPError) else (
            'invalid_response_encoding' if isinstance(exc, UnicodeError) else str(exc))
        failure = {'status': 'failed', 'query': args.query or '', 'region': args.region, 'reason': reason,
            'failed_at': datetime.now(timezone.utc).isoformat(), 'complete': False,
            'database_changed': False}
        if isinstance(exc, httpx.HTTPStatusError):
            failure['http_status'] = exc.response.status_code
        (root / 'failure.json').write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(failure, ensure_ascii=False))
        raise SystemExit(1) from None
    (root / 'search.html').write_text(raw, encoding='utf-8')
    if 'area_response' in receipt:
        (root / 'areas.json').write_text(receipt.pop('area_response'), encoding='utf-8')
    (root / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'query': receipt['query'], 'region': receipt.get('region'), 'bytes': len(raw.encode()), 'complete': False,
        'parse_status': receipt['parse_status'], 'records': receipt['records']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
