import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.scripts import probe_paris_stores as collector
from app.services.paris_store_identity import paris_capture_rows, select_paris_region


AREAS = json.dumps({'cat_10': {'id': 10, 'name': '서울', 'children': [
    {'id': 20, 'name': '동대문구'}, {'id': 21, 'name': '성북구'}]}}, ensure_ascii=False)


def test_region_is_selected_from_official_hierarchy_and_receipt_binds_it(monkeypatch):
    monkeypatch.setattr(collector.time, 'sleep', lambda _: None)
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        if request.url.params['action'] == 'pb_store_get_area':
            return httpx.Response(200, text=AREAS)
        assert request.url.params['type'] == '#around'
        assert request.url.params['area1'] == '10' and request.url.params['area2'] == '20'
        assert request.url.params['keyword'] == ''
        return httpx.Response(200, text='<ul data-total-count="0"></ul>')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        raw, receipt = collector.capture_paris('', region=('서울', '동대문구'), client=client)
    assert len(requests) == 3 and receipt['region']['district_id'] == '20'
    assert receipt['area_response_sha256'] == hashlib.sha256(AREAS.encode()).hexdigest()
    assert paris_capture_rows(raw, receipt, datetime.now(timezone.utc), AREAS) == []
    with pytest.raises(ValueError, match='area_evidence'):
        paris_capture_rows(raw, receipt, datetime.now(timezone.utc))
    with pytest.raises(ValueError, match='area_evidence'):
        paris_capture_rows(raw, receipt, datetime.now(timezone.utc), AREAS + ' ')
    receipt['region']['district_id'] = '21'
    with pytest.raises(ValueError, match='region_mismatch'):
        paris_capture_rows(raw, receipt, datetime.now(timezone.utc), AREAS)


@pytest.mark.parametrize('raw', [json.dumps({}),
    AREAS.replace('동대문구', '다른구'), AREAS.replace('"id": 20', '"id": "bad"'),
    json.dumps({'a': {'id': 10, 'name': '서울', 'children': []},
                'b': {'id': 11, 'name': '서울', 'children': []}})])
def test_missing_ambiguous_or_invalid_region_is_rejected(raw):
    with pytest.raises(ValueError):
        select_paris_region(raw, '서울', '동대문구')


def test_name_and_region_cannot_be_combined():
    with pytest.raises(ValueError, match='invalid_store_query'):
        collector.capture_paris('경희', region=('서울', '동대문구'))


def test_similar_region_name_never_merges_an_unmatched_candidate():
    from types import SimpleNamespace
    from app.scripts.review_paris_region import compare_candidate
    candidate = SimpleNamespace(id=40, branch='경희대점', address='회기로 201')
    rows = [{'official_id': '1050', 'official_name': '수원경희대', 'address': '경기 수원시 덕영대로 1705'}]
    result = compare_candidate(candidate, rows)
    assert result['status'] == 'pending' and result['database_changed'] is False


def test_same_name_different_address_requires_review_before_location_check():
    from types import SimpleNamespace
    from app.scripts.review_paris_region import compare_candidate
    candidate = SimpleNamespace(id=40, branch='검증용점', address='서울 동대문구 테스트로 18')
    rows = [{'official_id': '123', 'official_name': '검증용', 'address': '서울 동대문구 테스트로 180'}]
    assert compare_candidate(candidate, rows)['status'] == 'needs_address_review'
    rows[0]['address'] = '서울 동대문구 테스트로 18'
    assert compare_candidate(candidate, rows)['status'] == 'needs_place_check'
