import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent, MapLocation, MapJobLease
from app.models.store import Store, StoreOffer
from app.services.store_directory_review import identity_hash, record_review
from app.services import store_directory_revalidation as service


@pytest.fixture
def change(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'kakao_rest_api_key', 'test-key')
    row = {'official_id': '279', 'official_name': '청량리역사점',
        'address': '서울 동대문구 왕산로 214 5층', 'lat': 37.585, 'lng': 127.051139, 'delivery_only': False}
    raw = b'official archived response'
    path = tmp_path / 'source.json'
    path.write_bytes(raw)
    now = datetime.now(timezone.utc)
    state = MapSourceState(source_key='store-directory:test', source_kind='store_directory',
        source_url='https://m.outback.co.kr/store/findStore.do', status='ok', failures=0,
        records=[row], content_hash=hashlib.sha256(raw).hexdigest(), last_success_at=now, last_attempt_at=now)
    db.add(state)
    event = MapReviewEvent(kind='pipeline', item_id=0, action='store_directory_changed', at=now,
        payload={'source_key': state.source_key, 'provider': 'outback', 'after_content_hash': state.content_hash,
            'after_identity_hash': identity_hash([row]), 'archive': str(path),
            'changes': [{'official_id': '279', 'kind': 'modified', 'before': None, 'after': row}]})
    db.add(event)
    db.commit()
    return event


def acknowledge(db, change):
    record_review(db, change.id, change.payload['after_identity_hash'], 'Verify place separately')


def document(change):
    row = change.payload['changes'][0]['after']
    return {'id': '123', 'place_name': '아웃백스테이크하우스 청량리역사점',
        'road_address_name': '서울 동대문구 왕산로 214', 'address_name': '',
        'x': str(row['lng']), 'y': str(row['lat']), 'place_url': 'http://place.map.kakao.com/123'}


def run(db, change, payload=None, handler=None):
    if payload is None:
        payload = {'documents': [document(change)], 'meta': {'is_end': True}}
    with httpx.Client(transport=httpx.MockTransport(handler or (lambda req: httpx.Response(200, json=payload)))) as client:
        return service.revalidate_change(db, change.id, '279', change.payload['after_identity_hash'], client=client)


def test_requires_review_then_records_bound_evidence_without_publication(db, change):
    with pytest.raises(ValueError, match='review_required'):
        run(db, change)
    acknowledge(db, change)
    result = run(db, change)
    assert result['status'] == 'matched' and result['place']['place_id'] == '123'
    assert result['review_id'] and result['response_sha256'] and result['lookup_started_at']
    assert result['map_publication'] is False and result['benefit_approval'] is False
    assert service.list_checks(db, change.id)[0]['check_id'] == result['check_id']
    for model in (Store, StoreOffer, MapLocation, MapJobLease):
        assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('mode,reason', [
    ('zero', 'no_unique_exact_place'), ('duplicate', 'no_unique_exact_place'),
    ('name', 'no_unique_exact_place'), ('address', 'no_unique_exact_place'),
    ('distant', 'official_place_coordinates_disagree'), ('url', 'invalid_place_source'),
    ('nan', 'invalid_place_coordinates'), ('missing', 'invalid_place_response'),
    ('incomplete', 'place_results_incomplete'), ('badmeta', 'invalid_place_response'),
    ('badname', 'invalid_place_response')])
def test_mismatches_and_incomplete_responses_remain_pending(db, change, mode, reason):
    acknowledge(db, change)
    doc = document(change)
    data = {'documents': [doc], 'meta': {'is_end': True}}
    if mode == 'zero': data['documents'] = []
    if mode == 'duplicate': data['documents'].append(dict(doc))
    if mode == 'name': doc['place_name'] = '다른 지점'
    if mode == 'address': doc['road_address_name'] = '서울 동대문구 왕산로 215'
    if mode == 'distant': doc['y'] = '37.58056576973557'
    if mode == 'url': doc['place_url'] = 'https://example.com/123'
    if mode == 'nan': doc['x'] = 'NaN'
    if mode == 'missing': del doc['place_name']
    if mode == 'incomplete': data['meta']['is_end'] = False
    if mode == 'badmeta': data['meta'] = []
    if mode == 'badname': doc['place_name'] = 123
    result = run(db, change, data)
    assert result['status'] == 'pending' and result['reason'] == reason
    assert 'place' not in result


@pytest.mark.parametrize('mode', ['timeout', 'redirect', 'large', 'badjson'])
def test_network_failures_are_audited_without_secrets(db, change, mode):
    acknowledge(db, change)
    def respond(req):
        if mode == 'timeout': raise httpx.ReadTimeout('private details test-key', request=req)
        if mode == 'redirect': return httpx.Response(302, headers={'Location': 'https://example.com'})
        if mode == 'large': return httpx.Response(200, content=b'x' * 262145)
        return httpx.Response(200, text='not json test-key')
    result = run(db, change, handler=respond)
    assert result['status'] == 'pending' and 'test-key' not in str(result)
    assert len(service.list_checks(db, change.id)) == 1


@pytest.mark.parametrize('mode,reason', [('archive', 'evidence_invalid'),
    ('age', 'observation_not_recent'), ('version', 'version_mismatch'), ('key', 'api_key_required'),
    ('missing', 'missing_not_place_evidence'), ('delivery', 'not_confirmed_walk_in')])
def test_prerequisites_block_requests(db, change, monkeypatch, mode, reason):
    acknowledge(db, change)
    if mode == 'archive':
        from pathlib import Path
        Path(change.payload['archive']).write_bytes(b'corrupt')
    if mode == 'age':
        db.get(MapSourceState, change.payload['source_key']).last_success_at = datetime.now(timezone.utc) - timedelta(days=8)
        db.commit()
    if mode == 'version':
        with pytest.raises(ValueError, match=reason):
            service.revalidate_change(db, change.id, '279', '0' * 64)
        return
    if mode == 'key': monkeypatch.setattr(settings, 'kakao_rest_api_key', '')
    if mode in ('missing', 'delivery'):
        payload = dict(change.payload)
        payload['changes'] = [dict(payload['changes'][0])]
        if mode == 'missing': payload['changes'][0]['after'] = None
        else: payload['changes'][0]['after'] = {**payload['changes'][0]['after'], 'delivery_only': True}
        change.payload = payload
        db.commit()
    with pytest.raises(ValueError, match=reason):
        run(db, change, {'documents': [], 'meta': {'is_end': True}})
    assert service.list_checks(db, change.id) == []


def test_source_change_during_lookup_discards_result(db, change):
    acknowledge(db, change)
    data = {'documents': [document(change)], 'meta': {'is_end': True}}
    def respond(req):
        state = db.get(MapSourceState, change.payload['source_key'])
        state.last_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.commit()
        return httpx.Response(200, json=data)
    with pytest.raises(ValueError, match='review_stale'):
        run(db, change, handler=respond)
    assert service.list_checks(db, change.id) == []


@pytest.mark.parametrize('provider,branch,name', [('starbucks', '경희대', '스타벅스 경희대점'),
    ('mega', '경희대점', '메가MGC커피 경희대점'), ('paris', '테스트', '파리바게뜨 테스트점')])
def test_provider_names_without_outback_only_delivery_field(change, provider, branch, name):
    change.payload['provider'] = provider
    row = change.payload['changes'][0]['after']
    row['official_name'] = branch
    row.pop('delivery_only')
    assert service.candidate_for(change, '279')['name'] == name


def test_api_requires_admin_and_returns_service_result(client, db, change, monkeypatch):
    url = f'/admin/map/store-directories/changes/{change.id}/place-check'
    headers = {'X-Admin-Token': 'test-admin-token'}
    body = {'official_id': '279', 'expected_identity_hash': change.payload['after_identity_hash']}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=headers).status_code == 409
    acknowledge(db, change)
    monkeypatch.setattr(settings, 'kakao_rest_api_key', '')
    assert client.post(url, json=body, headers=headers).status_code == 503
    assert client.get(url + 's', headers=headers).json()['items'] == []
    def mock(db, change_id, official_id, expected_hash):
        assert change_id == change.id and official_id == '279'
        return {'status': 'matched', 'map_publication': False}
    monkeypatch.setattr(service, 'revalidate_change', mock)
    assert client.post(url, json=body, headers=headers).json()['status'] == 'matched'
