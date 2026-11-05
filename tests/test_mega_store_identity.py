import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.mega_store_identity import mega_candidate, parse_mega_search


NOW = datetime.now(timezone.utc)
HTML = """<li><a onclick="panTo('서울특별시 동대문구 경희대로 18 ');">
<div class="cont_text"><div><b>경희대점</b></div>
<div class="cont_text_info">서울특별시 동대문구 경희대로 18 , 1층 (회기동) 02-959-4282</div>
</div></a></li>"""


def evidence(rows=None, html=HTML):
    raw = json.dumps({'positions': rows if rows is not None else [{'idx': '732',
        'store_name2': '경희대점', 'store_address': '서울특별시 동대문구 경희대로 18 1층 (회기동)',
        'store_lat': '37.59298', 'store_lng': '127.05285'}]})
    receipt = {'source_url': 'https://www.mega-mgccoffee.com/store/find/',
        'response_url': 'https://www.mega-mgccoffee.com/store/find/store.php?lat=37.59&lng=127.06',
        'search_url': 'https://www.mega-mgccoffee.com/store/find/store_search.php?store_search=경희',
        'query': '경희', 'captured_at': NOW.isoformat(),
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'search_sha256': hashlib.sha256(html.encode()).hexdigest()}
    return raw, html, receipt


def test_name_and_full_address_are_paired_and_floor_preserved():
    row = parse_mega_search(HTML)[0]
    assert row['branch'] == '경희대점' and row['phone'] == '02-959-4282'
    assert row['address'] == '서울특별시 동대문구 경희대로 18 1층 (회기동)'
    candidate = mega_candidate(*evidence(), '732', '경희대점', NOW)
    assert candidate['provider'] == 'mega' and candidate['official_id'] == '732'
    assert '1층' in candidate['address']


@pytest.mark.parametrize('html', [HTML + HTML, '<footer>서울 동대문구 경희대로 18</footer>',
    HTML.replace('경희대로 18 \');', '경희대로 19 \');'), HTML.replace('<b>경희대점</b>', ''),
    HTML.replace("panTo('", "other('")])
def test_changed_structure_duplicate_branch_and_footer_are_rejected(html):
    with pytest.raises(ValueError):
        parse_mega_search(html)


def test_explicit_empty_search_is_distinct_from_changed_html():
    assert parse_mega_search('<div class="store_search_list_none">검색 결과가 없습니다.</div>') == []


def test_building_number_prefix_cannot_identify_another_building():
    html = HTML.replace('경희대로 18 ,', '경희대로 180 ,')
    with pytest.raises(ValueError, match='address_disagrees'):
        parse_mega_search(html)


def test_other_region_search_result_can_keep_unit_after_parenthetical_address():
    html = HTML.replace('서울특별시 동대문구 경희대로 18 ', '경기 수원시 영통구 덕영대로 1705 (영통동)')
    html = html.replace(', 1층 (회기동)', ', 101호')
    row = parse_mega_search(html)[0]
    assert row['address'].endswith(', 101호')


@pytest.mark.parametrize('change', ['raw_hash', 'search_hash', 'old', 'future', 'host', 'query'])
def test_capture_binds_both_sources_and_query(change):
    raw, html, receipt = evidence()
    if change == 'raw_hash': receipt['response_sha256'] = '0' * 64
    if change == 'search_hash': receipt['search_sha256'] = '0' * 64
    if change == 'old': receipt['captured_at'] = (NOW - timedelta(days=8)).isoformat()
    if change == 'future': receipt['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'host': receipt['response_url'] = 'https://www.mega-mgccoffee.com.evil/store/find/store.php'
    if change == 'query': receipt['query'] = '외대'
    with pytest.raises(ValueError):
        mega_candidate(raw, html, receipt, '732', '경희대점', NOW)


@pytest.mark.parametrize('change', ['id', 'name', 'floor', 'nan', 'duplicate'])
def test_official_search_and_map_must_agree_on_exact_identity(change):
    raw, _, _ = evidence()
    row = json.loads(raw)['positions'][0]
    if change == 'id': row['idx'] = '999'
    if change == 'name': row['store_name2'] = '경희대정문점'
    if change == 'floor': row['store_address'] = row['store_address'].replace('1층', '2층')
    if change == 'nan': row['store_lat'] = 'nan'
    rows = [row, row] if change == 'duplicate' else [row]
    with pytest.raises(ValueError):
        mega_candidate(*evidence(rows), '732', '경희대점', NOW)
