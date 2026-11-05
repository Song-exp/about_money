"""Read official Outback locator data without executing its JavaScript."""
import ast
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse


SOURCE = 'https://m.outback.co.kr/store/findStore.do'
SEARCH_SOURCE = 'https://m.outback.co.kr/store/storeJsonList.do'


def parse_outback_stores(html):
    values = re.findall(r"\bvar\s+storeJsonString\s*=\s*('(?:[^'\\]|\\.)*')\s*;", html)
    if len(values) != 1:
        raise ValueError('official_store_data_missing_or_ambiguous')
    try:
        rows = json.loads(ast.literal_eval(values[0]))
    except (ValueError, SyntaxError):
        raise ValueError('official_store_data_invalid') from None
    return normalize_outback_rows(rows)


def parse_outback_search(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('resultCode') != '0005':
        raise ValueError('official_search_unsuccessful')
    payload = data.get('resultData')
    if not isinstance(payload, dict) or 'storeList' not in payload:
        raise ValueError('official_search_structure_changed')
    return normalize_outback_rows(payload['storeList'], allow_empty=True)


def normalize_outback_rows(rows, allow_empty=False):
    if not isinstance(rows, list) or (not rows and not allow_empty):
        raise ValueError('official_store_data_invalid')
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('official_store_data_invalid')
        code = str(row.get('stIdx', ''))
        name, address = row.get('stNm'), row.get('addrN')
        if not code.isdigit() or int(code) <= 0 or not isinstance(name, str) or not name.strip() or not isinstance(address, str) or not address.strip():
            raise ValueError('official_store_identity_missing')
        try:
            lat, lng = float(row.get('lat', '')), float(row.get('lng', ''))
        except (TypeError, ValueError):
            raise ValueError('invalid_official_coordinates') from None
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('invalid_official_coordinates')
        detail = row.get('addrDetail') or ''
        if not isinstance(detail, str) or row.get('deliveryOnly') not in ('Y', 'N'):
            raise ValueError('official_store_type_or_address_missing')
        result.append({'official_id': code, 'official_name': name.strip(),
            'address': ' '.join(part for part in (address.strip(), detail.strip()) if part),
            'lat': lat, 'lng': lng, 'delivery_only': row['deliveryOnly'] == 'Y'})
    if len({r['official_id'] for r in result}) != len(result):
        raise ValueError('official_store_duplicate_identity')
    return result


def outback_candidate(html, receipt, official_id, official_name, now):
    parsed = urlparse(receipt['source_url'])
    is_search = parsed.scheme == 'https' and parsed.netloc == 'm.outback.co.kr' and parsed.path == '/store/storeJsonList.do' and not parsed.fragment
    if receipt['source_url'] != SOURCE and not is_search:
        raise ValueError('unsupported_capture_source')
    if is_search:
        params = parse_qs(parsed.query, keep_blank_values=True)
        if (not receipt.get('query') or params != {'sdCd': [''], 'sggCd': [''], 'searchKeyword': [receipt['query']]}):
            raise ValueError('capture_query_mismatch')
    if hashlib.sha256(html.encode()).hexdigest() != receipt['response_sha256']:
        raise ValueError('capture_hash_mismatch')
    captured = datetime.fromisoformat(receipt['captured_at'])
    if captured.tzinfo is None or not timedelta(0) <= now - captured <= timedelta(days=7):
        raise ValueError('capture_not_recent')
    parsed_rows = parse_outback_search(html) if is_search else parse_outback_stores(html)
    rows = [r for r in parsed_rows if r['official_id'] == official_id and r['official_name'] == official_name]
    if len(rows) != 1:
        raise ValueError('official_store_identity_mismatch')
    row = rows[0]
    if row['delivery_only']:
        raise ValueError('delivery_only_store_not_map_restaurant')
    if not re.match(r'^(?:서울특별시|서울시|서울)\s+동대문구\s+', row['address']):
        raise ValueError('outside_review_region')
    if not (37 <= row['lat'] <= 38 and 126 <= row['lng'] <= 128):
        raise ValueError('invalid_official_coordinates')
    if not official_name.endswith('점'):
        raise ValueError('official_branch_mapping_requires_review')
    return {**row, 'provider': 'outback', 'brand': '아웃백', 'branch': official_name,
        'name': '아웃백스테이크하우스 ' + official_name,
        'source_url': receipt['source_url'], 'captured_at': receipt['captured_at'],
        'response_sha256': receipt['response_sha256']}
