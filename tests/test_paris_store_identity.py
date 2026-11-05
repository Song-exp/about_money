import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.services.paris_store_identity import parse_paris_search, paris_candidate, paris_capture_rows


NOW = datetime.now(timezone.utc)
ITEM = '''<li><div class="store-list-item" data-id="123" data-latitude="37.59" data-longitude="127.05">
<h3 class="store-name"><span class="label">검증용지점</span><i>icon</i></h3>
<p class="store-addr">서울 동대문구 테스트로 18 1층 101호</p></div></li>'''
HTML = '<ul data-total-count="1">' + ITEM + '</ul>'


def receipt(raw=HTML):
    return {'query': '검증용', 'source_url': 'https://www.paris.co.kr/store',
        'response_url': 'https://www.paris.co.kr/wp-admin/admin-ajax.php?action=pb_store_get_list&type=%23search&keyword=검증용',
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'captured_at': NOW.isoformat()}


def test_branch_identity_pairs_id_name_full_address_and_coordinates():
    rows = parse_paris_search(HTML)
    assert rows[0]['official_id'] == '123'
    assert rows[0]['official_name'] == '검증용지점'
    assert rows[0]['address'].endswith('1층 101호')
    c = paris_candidate(HTML, receipt(), '123', '검증용지점', NOW)
    assert c['provider'] == 'paris' and c['branch'] == '검증용지점'


@pytest.mark.parametrize('raw', ['\ufeff\t', '', '<footer>서울 동대문구 경희대로 18</footer>',
    HTML.replace('data-total-count="1"', 'data-total-count="2"'),
    HTML + HTML, '<ul data-total-count="2">' + ITEM + ITEM + '</ul>',
    HTML.replace('data-latitude="37.59"', 'data-latitude="nan"'),
    HTML.replace('data-latitude="37.59"', ''), HTML.replace('data-id="123"', 'data-id="bad"'),
    HTML.replace('<span class="label">검증용지점</span>', ''),
    HTML.replace('서울 동대문구 테스트로 18 1층 101호', '')])
def test_missing_changed_ambiguous_or_blank_response_cannot_approve(raw):
    with pytest.raises(ValueError):
        parse_paris_search(raw)


def test_explicit_empty_list_is_distinct_from_unstructured_blank_response():
    assert parse_paris_search('<ul data-total-count="0"></ul>') == []


@pytest.mark.parametrize('change', ['hash', 'old', 'future', 'host', 'action', 'query'])
def test_receipt_must_bind_source_query_time_and_content(change):
    r = receipt()
    if change == 'hash': r['response_sha256'] = '0' * 64
    if change == 'old': r['captured_at'] = (NOW - timedelta(days=8)).isoformat()
    if change == 'future': r['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'host': r['response_url'] = r['response_url'].replace('www.paris.co.kr', 'example.com')
    if change == 'action': r['response_url'] = r['response_url'].replace('pb_store_get_list', 'paris_get_timetable')
    if change == 'query': r['query'] = '다른지점'
    with pytest.raises(ValueError):
        paris_capture_rows(HTML, r, NOW)


def test_suwon_store_cannot_correct_seoul_candidate_with_similar_name():
    raw = HTML.replace('서울 동대문구 테스트로 18 1층 101호', '경기 수원시 영통구 덕영대로 1705 (영통동)')
    with pytest.raises(ValueError, match='outside_review_region'):
        paris_candidate(raw, receipt(raw), '123', '검증용지점', NOW)


def test_official_name_and_id_must_both_match():
    with pytest.raises(ValueError, match='identity_mismatch'):
        paris_candidate(HTML, receipt(), '123', '다른지점', NOW)
    with pytest.raises(ValueError, match='identity_mismatch'):
        paris_candidate(HTML, receipt(), '999', '검증용지점', NOW)
