"""Independent failure scenarios for evidence, identity, eligibility and caps."""
from datetime import datetime, timedelta, timezone, date
import json
import pytest
from sqlalchemy import select
from app.models.card import CardBenefit
from app.models.map_verification import MapEvidence, MapLocation, MapJobLease
from app.models.local_benefit import LocalBenefitMerchant
from app.models.store import StoreCategory
from app.services.map_verification import BenefitRules, PaymentContext, estimate, item_hash, location_hash, evidence_status
from app.services.map_sync import capture_official, acquire_lease

RULES = {'type': 'percent', 'value': 20, 'min_payment': 5000, 'per_transaction_cap': 1000,
         'monthly_cap': 3000, 'prior_spend_required': 200000, 'payment_paths': ['card'], 'scope_confirmed': True}

QUOTES = {'product': '테스트 체크카드', 'value': '20% 할인', 'minimum': '최소 금액 조건',
          'prior_spend': '전월 실적 조건', 'scope': '대상 지점 조건', 'period': '상시 제공',
          'transaction_cap': '건별 한도 조건', 'monthly_cap': '월간 한도 조건'}


@pytest.mark.parametrize('amount,spend,cap,status,saving', [
    (4000, 300000, 3000, 'ineligible', 0),
    (10000, 100000, 3000, 'ineligible', 0),
    (10000, None, 3000, 'conditional', 1000),
    (10000, 300000, None, 'conditional', 1000),
    (10000, 300000, 200, 'eligible', 200),
    (10000, 300000, 0, 'eligible', 0),
    (10000, 300000, 3000, 'eligible', 1000),
])
def test_conditions_and_caps(amount, spend, cap, status, saving):
    context = PaymentContext(amount=amount, path='card', prior_spend={} if spend is None else {7: spend},
                             remaining_caps={} if cap is None else {'card:1': cap})
    result = estimate(BenefitRules(**RULES), context, '경희대점', 'card:1', 7)
    assert (result['status'], result['estimated_saving']) == (status, saving)


def test_fixed_can_outperform_high_rate():
    context = PaymentContext(amount=10000, path='card')
    base = {'min_payment': 0, 'prior_spend_required': 0, 'payment_paths': ['card'], 'scope_confirmed': True}
    percent = estimate(BenefitRules(**base, type='percent', value=50, per_transaction_cap=500), context, '', 'a')
    fixed = estimate(BenefitRules(**base, type='amount', value=2000), context, '', 'b')
    assert fixed['estimated_saving'] > percent['estimated_saving']


def test_branches_path_and_unverified_credentials():
    rule = BenefitRules(**{**RULES, 'excluded_branches': ['제외점'], 'credentials': ['student_card']})
    result = estimate(rule, PaymentContext(amount=10000, path='naver'), '제외점', 'x')
    assert result['status'] == 'ineligible'
    result = estimate(rule, PaymentContext(amount=10000, path='card', credentials=['student_card']), '일반점', 'x')
    assert result['status'] == 'conditional'


def captured(db, seed_stores):
    item = db.scalar(select(CardBenefit))
    source = {'target_kind': 'card', 'item_id': item.id, 'url': 'https://www.shinhancard.com/example'}
    capture_official(db, source, ('<html>공식 원문 카드 조건 ' + ' '.join(QUOTES.values()) + '내용 ' * 100 + '</html>').encode())
    db.commit()
    return item, db.get(MapEvidence, ('card', item.id)), source


def test_capture_never_approves_and_change_invalidates(db, seed_stores):
    item, row, source = captured(db, seed_stores)
    assert evidence_status(row, item) == 'pending'
    row.status, row.verified_at, row.recheck_at, row.indefinite = 'approved', datetime.now(timezone.utc), datetime.now(timezone.utc)+timedelta(days=1), True
    db.commit()
    assert evidence_status(row, item) == 'approved'
    capture_official(db, source, ('<html>공식 원문 카드 조건 ' + ' '.join(QUOTES.values()) + '내용 ' * 100 + '</html>').encode())
    assert evidence_status(row, item) == 'approved'  # unchanged retrieval is not a new approval
    verified = row.verified_at
    assert row.verified_at == verified
    item.cap_text = '변경된 한도'
    assert evidence_status(row, item) == 'changed'
    capture_official(db, source, ('<html>변경된 원문 ' + '내용 ' * 100 + '</html>').encode())
    assert evidence_status(row, item) == 'pending'
    assert row.verified_at is None


def test_parent_card_identity_change_invalidates_approval(db, seed_stores):
    item, row, _ = captured(db, seed_stores)
    row.status, row.verified_at, row.recheck_at, row.indefinite = 'approved', datetime.now(timezone.utc), datetime.now(timezone.utc)+timedelta(days=1), True
    assert evidence_status(row, item) == 'approved'
    item.card.issuer = '다른 발급사'
    assert evidence_status(row, item) == 'changed'


def test_expiry_uses_korean_calendar_day(db, seed_stores):
    item, row, _ = captured(db, seed_stores)
    row.status, row.verified_at, row.recheck_at = 'approved', datetime.now(timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc)
    row.valid_to = date(2026, 10, 13)
    # UTC Oct 13 16:00 is already Oct 14 in Korea.
    assert evidence_status(row, item, datetime(2026, 10, 13, 16, tzinfo=timezone.utc)) == 'expired'


def test_review_requires_admin_and_current_version(client, db, seed_stores):
    item, row, _ = captured(db, seed_stores)
    body = {'expected_hash': row.content_hash, 'action': 'approved', 'reviewer': '검토자',
            'evidence_quotes': QUOTES,
            'reason': '원문에 따른 모든 조건 확인 완료', 'rules': RULES, 'indefinite': True}
    url = f'/admin/map/review/card/{item.id}'
    assert client.post(url, json=body).status_code == 401
    headers = {'X-Admin-Token': 'test-admin-token'}
    assert client.post(url, json={**body, 'expected_hash': '0'*64}, headers=headers).status_code == 409
    assert client.post(url, json=body, headers=headers).json()['status'] == 'approved'
    db.refresh(row)
    row.recheck_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    assert evidence_status(row, item) == 'recheck_required'


def test_server_distance_before_pagination_and_changed_identity(client, db):
    for n in range(65):
        row = LocalBenefitMerchant(source_key=f'local:{n}', merchant_name=f'가맹점{n}', category=StoreCategory.CAFE,
            address=f'서울 주소{n}', search_query=f'검색{n}', source_type='student', programs=['khu_alliance'],
            credentials=[], benefit_text='조건 확인', is_active=True)
        db.add(row); db.flush()
        db.add(MapLocation(kind='local', item_id=row.id, identity_hash=location_hash(row), place_id=str(n),
            lat=37.5938 if n==64 else 35, lng=127.0564, source_url=f'https://place.map.kakao.com/{n}',
            checked_at=datetime.now(timezone.utc), reviewer='fixture'))
    db.commit()
    params = {'program': 'khu_alliance', 'lat': 37.5938, 'lng': 127.0564, 'radius_m': 3000, 'limit': 1}
    page = client.get('/stores/local-benefits', params=params).json()
    assert page['total'] == 1 and page['items'][0]['merchant_name'] == '가맹점64'
    row.address = '주소 변경'
    db.commit()
    page = client.get('/stores/local-benefits', params=params).json()
    assert page['total'] == 0 and page['unlocated_count'] == 1
    assert client.get('/stores/local-benefits', params={'lat': 37}).status_code == 422


def test_database_lease_prevents_concurrent_worker(db):
    first = acquire_lease(db)
    assert first and acquire_lease(db) is None
    row = db.get(MapJobLease, 'map_sync')
    row.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    db.commit()
    assert acquire_lease(db) != first


def test_estimate_uses_owned_cards_only(auth_client, db, seed_stores):
    from app.models.store import Store
    store = db.scalar(select(Store).where(Store.brand == 'GS25'))
    response = auth_client.post('/stores/estimate', json={'kind': 'store', 'item_id': store.id,
        'context': {'amount': 10000, 'path': 'card'}})
    assert response.status_code == 200
    assert all(not r['key'].startswith('card:') for r in response.json()['candidates'])
    assert response.json()['best'] is None


def test_approved_owned_card_is_estimated_and_revocation_excludes(auth_client, db, seed_stores):
    from app.models.store import Store
    item, row, _ = captured(db, seed_stores)
    simple = {**RULES, 'prior_spend_required': 0, 'monthly_cap': None}
    body = {'expected_hash': row.content_hash, 'action': 'approved', 'reviewer': '검토자',
            'evidence_quotes': QUOTES,
            'reason': '원문 조건과 대상 지점 확인 완료', 'rules': simple, 'indefinite': True}
    headers = {'X-Admin-Token': 'test-admin-token'}
    assert auth_client.post(f'/admin/map/review/card/{item.id}', json=body, headers=headers).status_code == 200
    assert auth_client.put('/auth/profile', json={'card_ids': [item.card_id]}).status_code == 200
    store = db.scalar(select(Store).where(Store.brand == 'GS25'))
    request = {'kind': 'store', 'item_id': store.id, 'context': {'amount': 10000, 'path': 'card'}}
    result = auth_client.post('/stores/estimate', json=request).json()
    assert result['best']['estimated_saving'] == 1000
    assert auth_client.post(f'/admin/map/review/card/{item.id}', json={**body, 'action': 'revoked'}, headers=headers).status_code == 200
    assert auth_client.post('/stores/estimate', json=request).json()['best'] is None


def test_location_review_cannot_accept_wrong_address(client, db):
    row = LocalBenefitMerchant(source_key='test', merchant_name='식당', address='서울 주소', category=StoreCategory.RESTAURANT,
        search_query='검색', source_type='student', programs=[], credentials=[], benefit_text='정보')
    db.add(row); db.commit()
    payload = {'expected_identity': location_hash(row), 'place_id': '1', 'matched_name': '식당', 'matched_address': '다른 주소',
               'lat': 37, 'lng': 127, 'source_url': 'https://place.map.kakao.com/1', 'reviewer': '검토자'}
    assert client.post(f'/admin/map/locations/local/{row.id}', json=payload, headers={'X-Admin-Token':'test-admin-token'}).status_code == 422
    assert db.get(MapLocation, ('local', row.id)) is None


def test_additive_migration_and_rollback_in_isolated_database(monkeypatch):
    import importlib.util
    from sqlalchemy import create_engine, inspect
    spec = importlib.util.spec_from_file_location('map_migration', 'migrations/versions/b08d3f276549_map_verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scratch = create_engine('sqlite:///:memory:')
    with scratch.begin() as connection:
        monkeypatch.setattr(module.op, 'get_bind', lambda: connection)
        module.upgrade()
        assert set(inspect(connection).get_table_names()) == {'map_evidence', 'map_locations', 'map_job_leases', 'map_review_events'}
        module.downgrade()
        assert inspect(connection).get_table_names() == []
