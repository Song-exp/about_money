import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.outback_store_identity import SOURCE, SEARCH_SOURCE, outback_candidate, parse_outback_stores, parse_outback_search


NOW = datetime.now(timezone.utc)
ROW = {'stIdx': 279, 'stNm': '청량리역사점', 'addrN': '서울 동대문구 왕산로 214 (전농동)',
    'addrDetail': 'Connect Place 4층', 'lat': '37.5841971', 'lng': '127.051139', 'deliveryOnly': 'N'}


def html(rows=None):
    return "<script>var storeJsonString = '" + json.dumps([ROW] if rows is None else rows, ensure_ascii=False) + "';</script>"


def receipt(raw):
    return {'source_url': SOURCE, 'captured_at': NOW.isoformat(),
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}


def test_retained_store_keeps_detail_and_binds_code_and_name():
    raw = html()
    row = outback_candidate(raw, receipt(raw), '279', '청량리역사점', NOW)
    assert row['address'].endswith('Connect Place 4층')
    assert row['brand'] == '아웃백' and row['name'] == '아웃백스테이크하우스 청량리역사점'
    with pytest.raises(ValueError, match='identity_mismatch'):
        outback_candidate(raw, receipt(raw), '279', '청량리점', NOW)


@pytest.mark.parametrize('rows', [[], [1], [ROW, ROW], [{**ROW, 'stIdx': ''}],
    [{**ROW, 'stNm': ''}], [{**ROW, 'lat': 'nan'}], [{**ROW, 'deliveryOnly': ''}]])
def test_changed_or_ambiguous_official_data_is_rejected(rows):
    with pytest.raises(ValueError):
        parse_outback_stores(html(rows))


def test_footer_and_duplicate_embedded_arrays_do_not_supply_identity():
    with pytest.raises(ValueError):
        parse_outback_stores('<footer>서울 동대문구 왕산로 214</footer>')
    with pytest.raises(ValueError):
        parse_outback_stores(html() + html())


@pytest.mark.parametrize('change', ['hash', 'old', 'future', 'source', 'delivery', 'region'])
def test_only_fresh_exact_non_delivery_official_store_can_be_candidate(change):
    raw = html([{**ROW, 'deliveryOnly': 'Y'}]) if change == 'delivery' else (
        html([{**ROW, 'addrN': '서울 강남구 테스트로 18'}]) if change == 'region' else html())
    r = receipt(raw)
    if change == 'hash': r['response_sha256'] = '0' * 64
    if change == 'old': r['captured_at'] = (NOW - timedelta(days=8)).isoformat()
    if change == 'future': r['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'source': r['source_url'] = 'https://example.com/'
    with pytest.raises(ValueError):
        outback_candidate(raw, r, '279', '청량리역사점', NOW)


def search_raw(rows=None, result_code='0005'):
    return json.dumps({'resultCode': result_code, 'resultData': {'storeList': [ROW] if rows is None else rows}})


def search_receipt(raw):
    return {**receipt(raw), 'query': '청량리',
        'source_url': SEARCH_SOURCE + '?sdCd=&sggCd=&searchKeyword=%EC%B2%AD%EB%9F%89%EB%A6%AC'}


def test_official_query_response_binds_success_code_and_requested_keyword():
    raw = search_raw()
    r = search_receipt(raw)
    c = outback_candidate(raw, r, '279', '청량리역사점', NOW)
    assert c['official_id'] == '279' and c['lat'] == float(ROW['lat'])
    assert c['source_url'] == r['source_url']
    with pytest.raises(ValueError, match='query_mismatch'):
        outback_candidate(raw, {**r, 'query': '다른지점'}, '279', '청량리역사점', NOW)


def test_explicit_successful_empty_search_is_not_a_missing_payload():
    assert parse_outback_search(search_raw([])) == []
    with pytest.raises(ValueError, match='structure_changed'):
        parse_outback_search('{"resultCode":"0005","resultData":{}}')


@pytest.mark.parametrize('raw', [search_raw(result_code='0000'), search_raw(result_code='ERROR'),
    'null', '[]', '{"resultCode":"0005","resultData":{"storeList":null}}', search_raw([ROW, ROW])])
def test_failed_changed_or_duplicate_api_result_cannot_supply_candidate(raw):
    with pytest.raises(ValueError):
        parse_outback_search(raw)


@pytest.mark.parametrize('url', [SEARCH_SOURCE + '?searchKeyword=청량리',
    SEARCH_SOURCE + '?sdCd=11&sggCd=&searchKeyword=청량리',
    SEARCH_SOURCE + '?sdCd=&sggCd=&searchKeyword=청량리&searchKeyword=서울',
    'https://m.outback.co.kr.example.com/store/storeJsonList.do?sdCd=&sggCd=&searchKeyword=청량리'])
def test_search_source_cannot_silently_change_scope_or_host(url):
    raw = search_raw()
    with pytest.raises(ValueError):
        outback_candidate(raw, {**search_receipt(raw), 'source_url': url}, '279', '청량리역사점', NOW)
