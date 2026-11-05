import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent
from app.models.store import Store, StoreOffer
from app.services import store_directory_collection as collector
from app.services.store_directory_review import compare_records, identity_hash, list_changes, record_review

KEY = 'store-directory:outback-test'
RAW = {'stIdx': 279, 'stNm': '청량리역사점', 'addrN': '서울 동대문구 왕산로 214',
    'addrDetail': '4층', 'lat': '37.5841971', 'lng': '127.051139', 'deliveryOnly': 'N'}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'outback-test', 'provider': 'outback', 'query': '청량리'}]))
    monkeypatch.setattr(settings, 'card_source_archive_dir', str(tmp_path))
    monkeypatch.setattr(collector.time, 'sleep', lambda _: None)


def observe(db, rows):
    def respond(request):
        return httpx.Response(200, text='User-agent: *\nAllow: /') if request.url.path == '/robots.txt' else (
            httpx.Response(200, json={'resultCode': '0005', 'resultData': {'storeList': rows}}))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        return collector.collect_store_directories(db, client=client, force=True)


def changed(db):
    observe(db, [RAW])
    observe(db, [{**RAW, 'addrDetail': '5층', 'lat': '37.585'}])
    return list_changes(db)['items'][0]


def test_baseline_reorder_and_equivalent_spelling_are_not_changes(db):
    other = {**RAW, 'stIdx': 280, 'stNm': '다른지점'}
    observe(db, [RAW, other])
    observe(db, [other, {**RAW, 'addrN': '서울특별시  동대문구 왕산로 214'}])
    assert list_changes(db)['items'] == []


def test_changed_fields_before_after_evidence_and_idempotent_review(db):
    item = changed(db)
    assert item['status'] == 'pending' and item['current'] and item['reviewable']
    delta = item['changes'][0]
    assert delta['kind'] == 'modified' and set(delta['fields']) == {'address', 'lat'}
    assert delta['before']['address'].endswith('4층') and delta['after']['address'].endswith('5층')
    state = db.get(MapSourceState, KEY)
    observed_at = state.last_success_at
    first = record_review(db, item['id'], item['after_identity_hash'], '추가 위치 확인 필요')
    second = record_review(db, item['id'], item['after_identity_hash'], '중복 요청')
    assert first['status'] == 'reviewed' and second['status'] == 'already_reviewed'
    assert first['review_id'] == second['review_id']
    assert state.last_success_at == observed_at
    assert list_changes(db)['items'][0]['status'] == 'reviewed'
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert db.scalar(select(func.count()).select_from(StoreOffer)) == 0


def test_addition_and_missing_are_distinct_and_do_not_mean_closure(db):
    observe(db, [RAW])
    other = {**RAW, 'stIdx': 280, 'stNm': '새지점'}
    observe(db, [RAW, other])
    assert list_changes(db)['items'][0]['changes'][0]['kind'] == 'added'
    observe(db, [other])
    missing = list_changes(db)['items'][0]
    assert missing['changes'][0]['kind'] == 'missing_from_response'
    assert missing['missing_is_closure_evidence'] is False


def test_whole_list_disappearance_retains_state_and_retries_do_not_duplicate_queue(db):
    observe(db, [RAW])
    state = db.get(MapSourceState, KEY)
    previous = (state.records, state.content_hash, state.last_success_at)
    observe(db, [])
    observe(db, [])
    items = list_changes(db)['items']
    assert len(items) == 1 and items[0]['state_retained'] and items[0]['current']
    assert state.status == 'error' and (state.records, state.content_hash, state.last_success_at) == previous
    assert record_review(db, items[0]['id'], items[0]['after_identity_hash'], '누락 확인 대기')['status'] == 'reviewed'
    # A successful recovery followed by a new disappearance is a new occurrence.
    observe(db, [RAW])
    observe(db, [])
    items = list_changes(db)['items']
    assert len(items) == 2 and items[0]['reviewable'] and not items[1]['current']


def test_newer_observation_and_wrong_expected_version_block_review(db):
    item = changed(db)
    with pytest.raises(ValueError, match='version_mismatch'):
        record_review(db, item['id'], '0' * 64, '검토')
    observe(db, [{**RAW, 'addrDetail': '6층'}])
    with pytest.raises(ValueError, match='review_stale'):
        record_review(db, item['id'], item['after_identity_hash'], '이전 자료')
    assert not next(r for r in list_changes(db)['items'] if r['id'] == item['id'])['current']


def test_raw_format_change_without_identity_change_does_not_stale_review(db):
    item = changed(db)
    observe(db, [{**RAW, 'addrN': '서울특별시 동대문구 왕산로 214', 'addrDetail': '5층', 'lat': '37.585000'}])
    assert list_changes(db)['items'][0]['id'] == item['id']
    assert record_review(db, item['id'], item['after_identity_hash'], '동일 내용 확인')['status'] == 'reviewed'


def test_floor_and_building_number_boundaries_are_preserved():
    old = {'official_id': '1', 'official_name': '지점', 'address': '서울 동대문구 테스트로 18 1층', 'lat': 37.5, 'lng': 127.0}
    new = {**old, 'address': '서울 동대문구 테스트로 181층'}
    assert compare_records([old], [new])[0]['fields'] == ['address']
    assert identity_hash([old]) != identity_hash([new])


def test_returning_to_old_value_does_not_reactivate_an_old_change(db):
    first = changed(db)
    observe(db, [RAW])
    observe(db, [{**RAW, 'addrDetail': '5층', 'lat': '37.585'}])
    items = list_changes(db)['items']
    assert items[0]['current'] and not next(r for r in items if r['id'] == first['id'])['current']
    with pytest.raises(ValueError, match='review_stale'):
        record_review(db, first['id'], first['after_identity_hash'], '이전 변경 재검토')


@pytest.mark.parametrize('mode', ['missing', 'corrupt'])
def test_review_requires_the_retained_source_evidence(db, mode):
    from pathlib import Path
    item = changed(db)
    archive = Path(item['archive'])
    if mode == 'missing':
        archive.unlink()
    else:
        archive.write_text('corrupt', encoding='utf-8')
    with pytest.raises(ValueError, match='change_evidence_'):
        record_review(db, item['id'], item['after_identity_hash'], '근거 확인')
    assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(
        MapReviewEvent.action == 'store_directory_reviewed')) == 0


def test_admin_api_authentication_validation_and_review_flow(client, db):
    item = changed(db)
    url = f"/admin/map/store-directories/changes/{item['id']}/review"
    headers = {'X-Admin-Token': 'test-admin-token'}
    body = {'expected_identity_hash': item['after_identity_hash'], 'note': '공식 변경 검토'}
    assert client.get('/admin/map/store-directories/changes').status_code == 401
    assert client.post(url, json=body).status_code == 401
    assert client.get('/admin/map/store-directories/changes', headers=headers).json()['items'][0]['id'] == item['id']
    assert client.post(url, json={**body, 'note': ' '}, headers=headers).status_code == 422
    assert client.post(url, json={**body, 'expected_identity_hash': '0' * 64}, headers=headers).status_code == 409
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200 and response.json()['map_publication'] is False
