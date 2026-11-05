from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.models.map_source import MapSourceState
from app.models.map_verification import MapLocation, MapReviewEvent, MapJobLease
from app.models.store import Store, StoreCategory, StoreOffer, PayMethod
from app.services.map_verification import location_hash, item_hash
from app.services.store_directory_publication import publication_preview, apply_publication, APPLY_ACTION
from test_store_directory_revalidation import change, acknowledge, run


@pytest.fixture
def prepared(db, change):
    payload = deepcopy(change.payload)
    payload['changes'][0]['before'] = {**payload['changes'][0]['after'], 'address': '서울 동대문구 왕산로 214 4층'}
    change.payload = payload
    store = Store(brand='아웃백', branch='청량리역사점', category=StoreCategory.RESTAURANT,
        address=payload['changes'][0]['before']['address'], lat=37.58, lng=127.05, is_active=True)
    db.add(store)
    db.flush()
    db.add(StoreOffer(store_id=store.id, pay_method=PayMethod.KAKAO, discount_rate=5,
        is_active=True, is_sample=False, condition_text='existing rule'))
    db.add(MapLocation(kind='store', item_id=store.id, identity_hash=location_hash(store),
        place_id='321', lat=store.lat, lng=store.lng, source_url='https://place.map.kakao.com/321',
        checked_at=datetime.now(timezone.utc) - timedelta(days=2), reviewer='previous'))
    db.commit()
    acknowledge(db, change)
    check = run(db, change)
    return change, store, check


def preview(db, prepared):
    change, store, check = prepared
    return publication_preview(db, change.id, check['check_id'], store.id, change.payload['after_identity_hash'])


def apply(db, prepared, version=None):
    change, store, check = prepared
    return apply_publication(db, change.id, check['check_id'], store.id, change.payload['after_identity_hash'],
        version or preview(db, prepared)['expected_store_version'], 'admin-reviewer', 'Official address checked')


def test_preview_apply_audit_and_idempotence_preserve_offers(db, prepared):
    change, store, check = prepared
    offer = db.scalar(select(StoreOffer))
    before_offer_hash = item_hash(offer)
    data = preview(db, prepared)
    assert data['before']['address'].endswith('4층') and data['after']['address'].endswith('5층')
    assert store.address.endswith('4층')  # Preview is read-only.
    result = apply(db, prepared, data['expected_store_version'])
    assert result['status'] == 'applied'
    db.expire_all()
    assert store.address.endswith('5층') and store.lat == 37.585
    location = db.get(MapLocation, ('store', store.id))
    assert location.place_id == '123' and location.identity_hash == location_hash(store)
    assert location.checked_at.isoformat() == db.get(MapReviewEvent, check['check_id']).at.isoformat()
    audit = db.get(MapReviewEvent, result['event_id'])
    assert audit.payload['before']['location']['place_id'] == '321'
    assert audit.payload['after']['location']['place_id'] == '123' and audit.payload['provider'] == 'outback'
    assert audit.payload['benefit_approval'] is False
    assert offer.discount_rate == 5 and offer.condition_text == 'existing rule' and offer.is_active
    assert item_hash(offer) != before_offer_hash  # Old benefit approval cannot survive the address change.
    repeated = apply(db, prepared, data['expected_store_version'])
    assert repeated['status'] == 'already_applied' and repeated['event_id'] == result['event_id']
    assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(MapReviewEvent.action == APPLY_ACTION)) == 1
    assert db.scalar(select(func.count()).select_from(MapJobLease)) == 0


@pytest.mark.parametrize('field', ['address', 'lat', 'is_active', 'pin'])
def test_store_or_pin_edit_after_preview_blocks_apply(db, prepared, field):
    data = preview(db, prepared)
    store = prepared[1]
    if field == 'address': store.address += ' 101호'
    if field == 'lat': store.lat += .001
    if field == 'is_active': store.is_active = False
    if field == 'pin': db.get(MapLocation, ('store', store.id)).reviewer = 'new reviewer'
    db.commit()
    with pytest.raises(ValueError, match='version_mismatch|target_ineligible'):
        apply(db, prepared, data['expected_store_version'])
    assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(MapReviewEvent.action == APPLY_ACTION)) == 0


@pytest.mark.parametrize('mode,reason', [('expired', 'check_expired'), ('pending', 'check_not_current'),
    ('newer', 'check_not_current'), ('corrupt', 'evidence_invalid'), ('source', 'review_stale')])
def test_stale_or_invalid_evidence_blocks_apply(db, prepared, mode, reason):
    change, store, check = prepared
    record = db.get(MapReviewEvent, check['check_id'])
    payload = deepcopy(record.payload)
    if mode == 'expired':
        payload['lookup_started_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    if mode == 'pending': payload['status'] = 'pending'
    if mode == 'corrupt': payload['place']['lat'] += .01
    if mode == 'newer':
        db.add(MapReviewEvent(kind='pipeline', item_id=0, action=record.action,
            at=datetime.now(timezone.utc), payload={**payload, 'status': 'pending'}))
    if mode == 'source':
        state = db.get(MapSourceState, change.payload['source_key'])
        state.records = [{**state.records[0], 'address': '서울 동대문구 왕산로 999'}]
    record.payload = payload
    db.commit()
    with pytest.raises(ValueError, match=reason):
        apply(db, prepared)
    assert store.address.endswith('4층')


@pytest.mark.parametrize('mode,reason', [('branch', 'branch_conflict'), ('place', 'place_already_bound'),
    ('binding', 'official_id_already_bound'), ('other_id', 'another_official_id')])
def test_conflicting_targets_are_blocked(db, prepared, mode, reason):
    change, store, check = prepared
    other = Store(brand='아웃백', branch='다른점', address='서울 동대문구 왕산로 215',
        category=StoreCategory.RESTAURANT, lat=37.58, lng=127.05)
    db.add(other)
    db.flush()
    if mode == 'branch':
        store.branch = '이전이름점'
        other.branch = '청량리역사점'
        db.add(MapReviewEvent(kind='store', item_id=store.id, action='official_store_discovery',
            at=datetime.now(timezone.utc), payload={'provider': 'outback', 'official_id': '279'}))
    if mode == 'place':
        db.add(MapLocation(kind='store', item_id=other.id, identity_hash=location_hash(other),
            place_id='123', lat=other.lat, lng=other.lng, source_url='https://place.map.kakao.com/123',
            checked_at=datetime.now(timezone.utc), reviewer='other'))
    if mode in ('binding', 'other_id'):
        db.add(MapReviewEvent(kind='store', item_id=other.id if mode == 'binding' else store.id,
            action='official_store_discovery', at=datetime.now(timezone.utc),
            payload={'provider': 'outback', 'official_id': '279' if mode == 'binding' else '999'}))
    db.commit()
    with pytest.raises(ValueError, match=reason):
        apply(db, prepared)


def test_unbound_legacy_name_is_not_guessed_but_prior_id_binding_allows_rename(db, prepared):
    change, store, check = prepared
    store.branch = '레거시 이름'
    db.commit()
    with pytest.raises(ValueError, match='binding_required'):
        preview(db, prepared)
    db.add(MapReviewEvent(kind='store', item_id=store.id, action='official_store_discovery',
        at=datetime.now(timezone.utc), payload={'provider': 'outback', 'official_id': '279'}))
    db.commit()
    assert apply(db, prepared)['status'] == 'applied'
    assert store.branch == '청량리역사점'


def test_api_preview_apply_auth_and_body_validation(client, db, prepared):
    change, store, check = prepared
    base = f'/admin/map/store-directories/changes/{change.id}'
    params = {'check_id': check['check_id'], 'store_id': store.id,
        'expected_identity_hash': change.payload['after_identity_hash']}
    headers = {'X-Admin-Token': 'test-admin-token'}
    assert client.get(base + '/publication-preview', params=params).status_code == 401
    response = client.get(base + '/publication-preview', params=params, headers=headers)
    assert response.status_code == 200
    body = {**params, 'expected_store_version': response.json()['expected_store_version'], 'reviewer': 'Admin', 'note': 'Apply checked identity'}
    assert client.post(base + '/apply', json=body).status_code == 401
    assert client.post(base + '/apply', json={**body, 'note': ' '}, headers=headers).status_code == 422
    assert client.post(base + '/apply', json={**body, 'expected_store_version': '0' * 64}, headers=headers).status_code == 409
    assert client.post(base + '/apply', json=body, headers=headers).json()['status'] == 'applied'
    assert client.post(base + '/apply', json=body, headers=headers).json()['status'] == 'already_applied'


def test_failed_commit_rolls_back_store_pin_and_audit_together(db, prepared, monkeypatch):
    real_commit = db.commit
    def fail_publication_commit():
        if any(isinstance(row, MapReviewEvent) and row.action == APPLY_ACTION for row in db.new):
            raise IntegrityError('synthetic concurrent conflict', {}, Exception('constraint'))
        return real_commit()
    monkeypatch.setattr(db, 'commit', fail_publication_commit)
    with pytest.raises(ValueError, match='concurrent_conflict'):
        apply(db, prepared)
    db.expire_all()
    assert prepared[1].address.endswith('4층')
    assert db.get(MapLocation, ('store', prepared[1].id)).place_id == '321'
    assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(MapReviewEvent.action == APPLY_ACTION)) == 0
    assert db.scalar(select(func.count()).select_from(MapJobLease)) == 0


def test_target_list_auth_brand_filter_and_pagination(client, db, prepared):
    store = prepared[1]
    headers = {'X-Admin-Token': 'test-admin-token'}
    url = '/admin/map/store-directories/targets'
    assert client.get(url, params={'provider': 'outback'}).status_code == 401
    data = client.get(url, params={'provider': 'outback', 'limit': 1}, headers=headers).json()
    assert data['items'][0]['id'] == store.id and data['has_more'] is False
    assert client.get(url, params={'provider': 'mega'}, headers=headers).json()['items'] == []
    assert client.get(url, params={'provider': 'outback', 'offset': 1}, headers=headers).json()['items'] == []
    assert client.get(url, params={'provider': 'unknown'}, headers=headers).status_code == 422
