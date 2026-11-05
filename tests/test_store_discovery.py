import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.map_verification import MapLocation, MapReviewEvent
from app.models.store import Store, StoreCategory, StoreOffer
from app.scripts.discover_starbucks_store import official_candidate, register_match


NOW = datetime.now(timezone.utc)


def evidence():
    raw = json.dumps({'list': [{'s_biz_code': '9967', 's_name': '회기역사거리',
        'doro_address': '서울특별시 동대문구 이문로 37 (회기동)', 'lat': '37.590784', 'lot': '127.056098'}]})
    receipt = {'source_url': 'https://www.starbucks.co.kr/store/store_map.do',
        'captured_at': NOW.isoformat(), 'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}
    return raw, receipt


def candidate():
    return official_candidate(*evidence(), '9967', '회기역사거리', NOW)


def place():
    return {'id': '123', 'place_name': '스타벅스 회기역사거리점',
        'road_address_name': '서울 동대문구 이문로 37', 'address_name': '서울 동대문구 회기동',
        'place_url': 'http://place.map.kakao.com/123', 'y': '37.590784', 'x': '127.056098'}


@pytest.mark.parametrize('change', ['hash', 'old', 'future', 'naive', 'source'])
def test_capture_must_be_recent_and_bind_retained_response(change):
    raw, receipt = evidence()
    if change == 'hash': receipt['response_sha256'] = '0' * 64
    if change == 'old': receipt['captured_at'] = (NOW - timedelta(days=8)).isoformat()
    if change == 'future': receipt['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'naive': receipt['captured_at'] = NOW.replace(tzinfo=None).isoformat()
    if change == 'source': receipt['source_url'] = 'https://example.com/'
    with pytest.raises(ValueError):
        official_candidate(raw, receipt, '9967', '회기역사거리', NOW)


def test_official_id_and_name_must_both_match():
    with pytest.raises(ValueError):
        official_candidate(*evidence(), '9967', '회기역', NOW)
    with pytest.raises(ValueError):
        official_candidate(*evidence(), '9551', '회기역사거리', NOW)


def test_cross_checked_store_is_separate_from_legacy_candidate_and_idempotent(db):
    old = Store(brand='스타벅스', branch='회기역점', category=StoreCategory.CAFE,
        address='회기로 118', lat=37.589655, lng=127.057882, is_active=True)
    db.add(old)
    db.commit()
    result = register_match(db, candidate(), [place()], 'OK', NOW)
    db.commit()
    new = db.get(Store, result['store_id'])
    assert new.id != old.id and old.address == '회기로 118' and old.branch == '회기역점'
    assert db.get(MapLocation, ('store', new.id)).place_id == '123'
    assert list(db.scalars(select(StoreOffer))) == []
    assert len(list(db.scalars(select(MapReviewEvent)))) == 2
    assert register_match(db, candidate(), [place()], 'OK', NOW + timedelta(hours=1))['status'] == 'unchanged'
    db.commit()
    assert len(list(db.scalars(select(Store)))) == 2
    assert len(list(db.scalars(select(MapReviewEvent)))) == 2


@pytest.mark.parametrize('change', ['name', 'address', 'duplicate', 'error', 'url', 'coords', 'nan'])
def test_mismatched_or_ambiguous_place_does_not_register(db, change):
    p = place()
    if change == 'name': p['place_name'] = '스타벅스 회기역점'
    if change == 'address': p['road_address_name'] = '서울 동대문구 이문로 38'
    if change == 'url': p['place_url'] = 'https://example.com/123'
    if change == 'coords': p['y'] = '37.7'
    if change == 'nan': p['x'] = 'nan'
    docs = [p, p] if change == 'duplicate' else [p]
    with pytest.raises(ValueError):
        register_match(db, candidate(), docs, 'ERROR' if change == 'error' else 'OK', NOW)
    assert list(db.scalars(select(Store))) == []


def test_existing_same_branch_with_different_address_requires_review(db):
    row = Store(brand='스타벅스', branch='회기역사거리점', category=StoreCategory.CAFE,
        address='옛 주소', lat=37.59, lng=127.05)
    db.add(row)
    db.commit()
    with pytest.raises(ValueError, match='separate_review'):
        register_match(db, candidate(), [place()], 'OK', NOW)
    assert row.address == '옛 주소'


def test_place_cannot_be_bound_to_another_store(db):
    db.add(MapLocation(kind='store', item_id=999, identity_hash='x' * 64, place_id='123',
        lat=37.59, lng=127.05, source_url='https://place.map.kakao.com/123', checked_at=NOW, reviewer='reviewer'))
    db.commit()
    with pytest.raises(ValueError, match='place_already_bound'):
        register_match(db, candidate(), [place()], 'OK', NOW)
    assert list(db.scalars(select(Store))) == []


def test_official_code_cannot_be_rebound_to_another_store(db):
    db.add(MapReviewEvent(kind='store', item_id=999, action='official_store_discovery', at=NOW,
        payload={'provider': 'starbucks', 'official_id': '9967'}))
    db.commit()
    with pytest.raises(ValueError, match='official_id_already_bound'):
        register_match(db, candidate(), [place()], 'OK', NOW)
    assert list(db.scalars(select(Store))) == []


def test_mega_registration_records_provider_without_approving_benefits(db):
    c = {**candidate(), 'provider': 'mega', 'brand': '메가MGC커피', 'branch': '경희대점',
        'name': '메가MGC커피 경희대점', 'official_name': '경희대점', 'official_id': '732'}
    p = {**place(), 'place_name': c['name']}
    result = register_match(db, c, [p], 'OK', NOW)
    db.commit()
    row = db.get(Store, result['store_id'])
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'official_store_discovery'))
    assert row.brand == '메가MGC커피' and row.mark == 'M'
    assert event.payload['provider'] == 'mega' and event.payload['official_id'] == '732'
    assert event.payload['benefit_approved'] is False
    assert list(db.scalars(select(StoreOffer))) == []


def test_sdk_outage_is_reported_without_database_mutation(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app.scripts import discover_starbucks_store as discovery
    from app.scripts.resolve_map_locations import SDKLoadError

    browser = SimpleNamespace(close=lambda: None)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setattr(discovery, 'sync_playwright', lambda: nullcontext(playwright))

    def unavailable(*args, **kwargs):
        raise SDKLoadError([{'host': 'dapi.kakao.com', 'status': 'network_error'}])

    def unexpected_db_access():
        raise AssertionError('SDK outage must not attempt a DB mutation')

    monkeypatch.setattr(discovery, 'open_sdk_page', unavailable)
    monkeypatch.setattr(discovery, 'SessionLocal', unexpected_db_access)
    output = tmp_path / 'lookup.json'
    discovery.review_candidate(candidate(), output, apply=True)
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['lookup_status'] == 'SDK_UNAVAILABLE'
    assert result['reason'] == 'source_lookup_failed'
    assert result['status'] == 'pending' and result['applied'] is False
    assert result['benefit_approved'] is False


def test_paris_store_uses_bakery_category_and_cannot_spoof_other_brand(db):
    c = {**candidate(), 'provider': 'paris', 'brand': '파리바게뜨', 'branch': '검증용점',
        'name': '파리바게뜨 검증용점', 'official_name': '검증용', 'official_id': '999'}
    p = {**place(), 'place_name': c['name']}
    with pytest.raises(ValueError, match='provider_brand_mismatch'):
        register_match(db, {**c, 'brand': '스타벅스'}, [p], 'OK', NOW)
    result = register_match(db, c, [p], 'OK', NOW)
    db.commit()
    row = db.get(Store, result['store_id'])
    assert row.category == StoreCategory.BAKERY and row.mark == 'P'
    assert list(db.scalars(select(StoreOffer))) == []


def test_outback_registration_is_restaurant_without_new_benefits(db):
    c = {**candidate(), 'provider': 'outback', 'brand': '아웃백', 'branch': '청량리역사점',
        'name': '아웃백스테이크하우스 청량리역사점', 'official_name': '청량리역사점', 'official_id': '279'}
    p = {**place(), 'place_name': c['name']}
    result = register_match(db, c, [p], 'OK', NOW)
    db.commit()
    row = db.get(Store, result['store_id'])
    assert row.category == StoreCategory.RESTAURANT and row.brand == '아웃백'
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'official_store_discovery'))
    assert event.payload['provider'] == 'outback' and event.payload['benefit_approved'] is False
    assert list(db.scalars(select(StoreOffer))) == []
