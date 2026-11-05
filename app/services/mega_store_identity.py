"""Parse paired branch identities from Mega's public search response."""
import re
import hashlib
import json
import math
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.services.map_verification import location_address_key, normalize_location_text


def parse_mega_search(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for item in soup.select('li'):
        name = item.select('a .cont_text b')
        info = item.select('a .cont_text .cont_text_info')
        anchor = item.select('a[onclick]')
        if len(name) != 1 or len(info) != 1 or len(anchor) != 1:
            raise ValueError('official_search_identity_missing_or_ambiguous')
        action = re.fullmatch(r"panTo\('([^']+)'\);?", anchor[0]['onclick'].strip())
        if not action:
            raise ValueError('unsupported_official_address_action')
        text = info[0].get_text(' ', strip=True)
        phone = re.search(r'\s+(0\d{1,2}-\d{3,4}-\d{4})\s*$', text)
        address = text[:phone.start()].strip() if phone else text.strip()
        # Keep floor/unit information; remove only the formatting comma before it.
        address = re.sub(r'\s+,\s*', ' ', address)
        map_address = action[1].strip()
        # Non-Seoul results can append floor/unit text to the map address.
        # A prefix alone cannot distinguish building 18 from building 180.
        suffix = address[len(map_address):] if address.startswith(map_address) else None
        same_prefix = suffix is not None and (not suffix or suffix[0] in (' ', ','))
        if location_address_key(map_address) != location_address_key(address) and not same_prefix:
            raise ValueError('official_search_address_disagrees')
        branch = name[0].get_text(' ', strip=True)
        if not branch or not address:
            raise ValueError('official_search_identity_missing')
        rows.append({'branch': branch, 'address': address, 'map_address': action[1].strip(),
            'phone': phone[1] if phone else None})
    if not rows and not soup.select_one('.store_search_list_none'):
        raise ValueError('official_search_structure_changed')
    if len({row['branch'] for row in rows}) != len(rows):
        raise ValueError('official_search_duplicate_branch')
    return rows


def mega_candidate(raw, search_raw, receipt, official_id, branch, now):
    base = 'https://www.mega-mgccoffee.com/store/find/'
    if receipt['source_url'] != base:
        raise ValueError('unsupported_capture_source')
    for key, path in [('response_url', 'store.php'), ('search_url', 'store_search.php')]:
        parsed = urlparse(receipt[key])
        if (parsed.scheme != 'https' or parsed.netloc != 'www.mega-mgccoffee.com'
                or parsed.path != '/store/find/' + path):
            raise ValueError('unsupported_capture_source')
    if parse_qs(urlparse(receipt['search_url']).query) != {'store_search': [receipt['query']]}:
        raise ValueError('capture_query_mismatch')
    if (hashlib.sha256(raw.encode()).hexdigest() != receipt['response_sha256']
            or hashlib.sha256(search_raw.encode()).hexdigest() != receipt['search_sha256']):
        raise ValueError('capture_hash_mismatch')
    captured = datetime.fromisoformat(receipt['captured_at'])
    if captured.tzinfo is None or not timedelta(0) <= now - captured <= timedelta(days=7):
        raise ValueError('capture_not_recent')
    searched = [r for r in parse_mega_search(search_raw) if r['branch'] == branch]
    rows = [r for r in json.loads(raw)['positions'] if str(r.get('idx')) == official_id]
    if (not official_id.isdigit() or len(searched) != 1 or len(rows) != 1
            or rows[0].get('store_name2') != branch):
        raise ValueError('official_store_identity_mismatch')
    row = rows[0]
    address = row['store_address']
    if normalize_location_text(address) != normalize_location_text(searched[0]['address']):
        raise ValueError('official_search_map_address_disagrees')
    if not re.match(r'^(?:서울특별시|서울시|서울) 동대문구 ', address):
        raise ValueError('outside_review_region')
    lat, lng = float(row['store_lat']), float(row['store_lng'])
    if not (math.isfinite(lat) and math.isfinite(lng) and 37 <= lat <= 38 and 126 <= lng <= 128):
        raise ValueError('invalid_official_coordinates')
    return {'provider': 'mega', 'brand': '메가MGC커피', 'branch': branch,
        'name': '메가MGC커피 ' + branch, 'official_id': official_id, 'official_name': branch,
        'source_url': base, 'response_url': receipt['response_url'], 'search_url': receipt['search_url'],
        'address': address, 'lat': lat, 'lng': lng, 'captured_at': captured.isoformat(),
        'response_sha256': receipt['response_sha256'], 'search_sha256': receipt['search_sha256']}
