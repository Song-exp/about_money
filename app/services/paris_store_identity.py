"""Bind a Paris Baguette branch to its retained official search response."""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


def parse_paris_search(raw):
    soup = BeautifulSoup(raw, 'html.parser')
    lists = soup.select('ul[data-total-count]')
    if len(lists) != 1 or not str(lists[0]['data-total-count']).isdigit():
        raise ValueError('official_search_structure_changed')
    total = int(lists[0]['data-total-count'])
    items = lists[0].select('.store-list-item')
    if len(items) != total:
        raise ValueError('official_search_incomplete_or_ambiguous')
    rows = []
    for item in items:
        names = item.select('.store-name .label')
        addresses = item.select('.store-addr')
        code = item.get('data-id', '')
        if not code.isdigit() or int(code) <= 0 or len(names) != 1 or len(addresses) != 1:
            raise ValueError('official_store_identity_missing_or_ambiguous')
        name, address = names[0].get_text(' ', strip=True), addresses[0].get_text(' ', strip=True)
        if not name or not address:
            raise ValueError('official_store_identity_missing')
        try:
            lat, lng = float(item.get('data-latitude', '')), float(item.get('data-longitude', ''))
        except (ValueError, TypeError):
            raise ValueError('invalid_official_coordinates') from None
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('invalid_official_coordinates')
        rows.append({'official_id': code, 'official_name': name, 'address': address, 'lat': lat, 'lng': lng})
    if len({r['official_id'] for r in rows}) != len(rows):
        raise ValueError('official_search_duplicate_identity')
    return rows


def select_paris_region(area_raw, province, district):
    areas = json.loads(area_raw.lstrip('\ufeff'))
    if not isinstance(areas, dict):
        raise ValueError('official_area_structure_changed')
    provinces = [r for r in areas.values() if isinstance(r, dict) and r.get('name') == province]
    if len(provinces) != 1 or not isinstance(provinces[0].get('children'), list):
        raise ValueError('official_province_missing_or_ambiguous')
    districts = [r for r in provinces[0]['children'] if isinstance(r, dict) and r.get('name') == district]
    if len(districts) != 1:
        raise ValueError('official_district_missing_or_ambiguous')
    ids = [str(provinces[0].get('id', '')), str(districts[0].get('id', ''))]
    if not all(code.isdigit() and int(code) > 0 for code in ids):
        raise ValueError('invalid_official_area_code')
    return {'province': province, 'district': district, 'province_id': ids[0], 'district_id': ids[1]}


def paris_capture_rows(raw, receipt, now, area_raw=None):
    if receipt['source_url'] != 'https://www.paris.co.kr/store':
        raise ValueError('unsupported_capture_source')
    parsed = urlparse(receipt['response_url'])
    params = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.paris.co.kr'
            or parsed.path != '/wp-admin/admin-ajax.php' or parsed.fragment
            or params.get('action') != ['pb_store_get_list']
            or params.get('keyword') != [receipt['query']]):
        raise ValueError('capture_query_or_source_mismatch')
    if receipt.get('mode', 'name') == 'region':
        if area_raw is None or hashlib.sha256(area_raw.encode()).hexdigest() != receipt.get('area_response_sha256'):
            raise ValueError('official_area_evidence_missing_or_changed')
        region = receipt['region']
        selected = select_paris_region(area_raw, region['province'], region['district'])
        if (region != selected or receipt['query'] != '' or params.get('type') != ['#around']
                or params.get('area1') != [selected['province_id']]
                or params.get('area2') != [selected['district_id']]):
            raise ValueError('capture_region_mismatch')
    elif receipt.get('mode', 'name') != 'name' or params.get('type') != ['#search']:
        raise ValueError('capture_query_or_source_mismatch')
    if hashlib.sha256(raw.encode()).hexdigest() != receipt['response_sha256']:
        raise ValueError('capture_hash_mismatch')
    captured_at = datetime.fromisoformat(receipt['captured_at'])
    if captured_at.tzinfo is None or not timedelta(0) <= now - captured_at <= timedelta(days=7):
        raise ValueError('capture_not_recent')
    return parse_paris_search(raw)


def paris_candidate(raw, receipt, official_id, official_name, now, area_raw=None):
    rows = paris_capture_rows(raw, receipt, now, area_raw)
    matches = [r for r in rows if r['official_id'] == official_id and r['official_name'] == official_name]
    if len(matches) != 1:
        raise ValueError('official_store_identity_mismatch')
    row = matches[0]
    if not re.match(r'^(?:서울특별시|서울시|서울)\s+동대문구\s+', row['address']):
        raise ValueError('outside_review_region')
    if not (37 <= row['lat'] <= 38 and 126 <= row['lng'] <= 128):
        raise ValueError('invalid_official_coordinates')
    branch = official_name if official_name.endswith('점') else official_name + '점'
    return {**row, 'provider': 'paris', 'brand': '파리바게뜨', 'branch': branch,
        'name': '파리바게뜨 ' + branch, 'source_url': receipt['source_url'],
        'detail_url': f'https://www.paris.co.kr/store-detail/?id={official_id}',
        'response_url': receipt['response_url'], 'captured_at': receipt['captured_at'],
        'response_sha256': receipt['response_sha256']}
