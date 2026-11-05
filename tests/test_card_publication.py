"""Latest-source approval, atomic publication, store scope and conservative estimates."""
import hashlib
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.models.card import Card, CardBenefit
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence
from app.models.card_publication import CardProgramPublication
from app.models.map_verification import MapLocation
from app.models.store import Store, StoreCategory
from app.services.card_collection import register_source, register_product
from app.services.card_benefit_parsers import TOSS_URL, JOOPJOOP_URL, KST
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_publication import ProgramReview, review_program, store_programs, estimate_program, publication_status
from app.services.map_verification import PaymentContext, location_hash

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def draft(db):
    text = Path('tests/fixtures/toss-offline-benefits.txt').read_text(encoding='utf-8')
    source = register_source(db, TOSS_URL, provider_key='tossbank', issuer=('tossbank', '토스뱅크'), now=NOW)
    register_product(db, source, namespace='tossbank', code='check-card', name='토스뱅크 체크카드', product_type='check')
    digest = hashlib.sha256(text.encode()).hexdigest()
    snapshot = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256=digest,
        semantic_sha256=digest, raw_object_ref=digest, extracted_text=text,
        mime_type='text/html', parser_version='fixture', completeness='text_available')
    db.add(snapshot)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id,
        attempted_at=NOW, status='captured', http_status=200))
    result = extract_snapshot(db, source, snapshot, now=NOW)
    store = Store(brand='스타벅스', branch='테스트점', address='서울 동대문구 테스트로 1',
                  category=StoreCategory.CAFE, lat=37.59, lng=127.05)
    db.add(store)
    db.flush()
    db.add(MapLocation(kind='store', item_id=store.id, identity_hash=location_hash(store), place_id='10001',
        lat=store.lat, lng=store.lng, source_url='https://place.map.kakao.com/10001', checked_at=NOW, reviewer='fixture'))
    db.commit()
    return db.get(CardBenefitVersion, result['version_id']), source, snapshot, store


def review_body(version, snapshot, **updates):
    return ProgramReview(**{'action': 'approved', 'expected_payload_hash': version.payload_hash,
        'expected_snapshot_hash': snapshot.raw_sha256, 'reviewer': 'fixture-reviewer',
        'reason': '원문 전체 제외 조건 및 금액 구간을 검토했습니다.',
        'policy_quote': version.payload.get('exclusions_raw', ''), 'acknowledged_issues': version.payload['issues'], **updates})


def publish(db, draft):
    version, _, snapshot, store = draft
    review_program(db, version.id, review_body(version, snapshot), now=NOW)
    publication = db.get(CardProgramPublication, version.program_key)
    return publication, store_programs(db, store, now=NOW)[0]


def context(offer, amount=10000, **updates):
    midnight = NOW.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    values = {'amount': amount, 'path': 'card', 'program_modes': {offer['program_key']: 'offline'},
        'program_effective_at': {offer['program_key']: midnight},
        'program_daily_uses': {offer['daily_key']: 0}, 'program_monthly_uses': {offer['monthly_key']: 0},
        'program_first_transaction': {offer['daily_key']: True}, 'remaining_caps': {offer['monthly_key']: 5000},
        'excluded_items_confirmed_absent': True, 'other_conditions_confirmed': True}
    values.update(updates)
    return PaymentContext(**values)


def test_publish_store_mapping_and_integer_reward(db, draft):
    publication, offer = publish(db, draft)
    assert publication_status(db, publication, NOW) == 'approved'
    assert offer['card_name'] == '토스뱅크 체크카드'
    assert estimate_program(offer, context(offer), now=NOW)['estimated_saving'] == 500
    assert db.scalar(select(func.count()).select_from(CardBenefit)) == 0
    assert db.scalar(select(func.count()).select_from(Card)) == 1
    assert draft[0].publication_state == 'live'


@pytest.mark.parametrize('amount,expected', [(2999, None), (3000, 100), (9999, 100), (10000, 500), (500000, 500)])
def test_tier_boundaries(db, draft, amount, expected):
    _, offer = publish(db, draft)
    assert estimate_program(offer, context(offer, amount), now=NOW)['estimated_saving'] == expected


def test_unknown_conditions_never_become_best(db, draft):
    _, offer = publish(db, draft)
    result = estimate_program(offer, PaymentContext(amount=10000, path='card'), now=NOW)
    assert result['status'] == 'conditional' and result['estimated_saving'] is None
    assert len(result['reasons']) >= 6


def test_card_options_count_only_currently_public_program_rules(client, db, draft):
    publication, _ = publish(db, draft)
    def count():
        return next(card['benefit_count'] for card in client.get('/meta/options').json()['cards']
            if card['id'] == publication.card_id)
    assert count() == 6
    publication.recheck_at = NOW - timedelta(seconds=1)
    db.commit()
    assert count() == 0


@pytest.mark.parametrize('field', ['program_daily_uses', 'program_monthly_uses', 'program_first_transaction', 'remaining_caps', 'program_modes', 'program_effective_at'])
def test_explicit_negative_or_exhausted_context_blocks_reward(db, draft, field):
    _, offer = publish(db, draft)
    values = {'program_daily_uses': {offer['daily_key']: 1}, 'program_monthly_uses': {offer['monthly_key']: 10},
        'program_first_transaction': {offer['daily_key']: False}, 'remaining_caps': {offer['monthly_key']: 0},
        'program_modes': {offer['program_key']: 'online'}, 'program_effective_at': {offer['program_key']: NOW + timedelta(days=1)}}
    result = estimate_program(offer, context(offer, **{field: values[field]}), now=NOW)
    assert result['status'] == 'ineligible' and result['estimated_saving'] is None


def test_shared_scope_keys_and_remaining_cap(db, draft):
    _, offer = publish(db, draft)
    store = draft[3]
    store.brand = '맥도날드'
    db.get(MapLocation, ('store', store.id)).identity_hash = location_hash(store)
    db.commit()
    second = store_programs(db, store, now=NOW)[0]
    assert (offer['daily_key'], offer['monthly_key']) == (second['daily_key'], second['monthly_key'])
    assert estimate_program(second, context(second, remaining_caps={second['monthly_key']: 200}), now=NOW)['estimated_saving'] == 200


@pytest.mark.parametrize('mutation', ['stale', 'source_changed', 'payload', 'evidence', 'quote', 'unacknowledged'])
def test_approval_guards(db, draft, mutation):
    version, source, snapshot, _ = draft
    body = review_body(version, snapshot)
    if mutation == 'stale':
        db.scalar(select(CardCollectionAttempt)).attempted_at = NOW - timedelta(days=2)
    elif mutation == 'source_changed':
        changed = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256='a' * 64,
            semantic_sha256='b' * 64, raw_object_ref='changed', extracted_text='changed',
            mime_type='text/html', parser_version='fixture', completeness='text_available')
        db.add(changed)
        db.flush()
        db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=changed.id, status='captured', http_status=200, attempted_at=NOW))
    elif mutation == 'payload':
        version.payload = {**version.payload, 'season_id': 'tampered'}
    elif mutation == 'evidence':
        db.scalar(select(CardBenefitFieldEvidence)).quote = '변조된 구절'
    elif mutation == 'quote':
        body.policy_quote = '확인했다는 메모만 입력'
    else:
        body.acknowledged_issues = []
    db.commit()
    with pytest.raises(HTTPException):
        review_program(db, version.id, body, now=NOW)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
    assert db.scalar(select(func.count()).select_from(Card)) == 0


def test_runtime_expiry_change_location_and_revocation_hide_program(db, draft):
    publication, _ = publish(db, draft)
    version, source, snapshot, store = draft
    assert store_programs(db, store, now=NOW + timedelta(days=2)) == []
    store.address = '다른 주소'
    db.commit()
    assert store_programs(db, store, now=NOW) == []
    store.address = '서울 동대문구 테스트로 1'
    db.commit()
    assert store_programs(db, store, card_ids=[], now=NOW) == []
    review_program(db, version.id, review_body(version, snapshot, action='revoked'), now=NOW)
    assert store_programs(db, store, now=NOW) == []
    assert publication_status(db, publication, NOW) == 'revoked'


def test_api_admin_auth_and_public_store_only_after_approval(client, db, draft):
    version, _, snapshot, store = draft
    route = f'/admin/map/card-programs/{version.id}/review'
    body = review_body(version, snapshot).model_dump(mode='json')
    assert client.get('/admin/map/card-programs').status_code == 401
    assert client.post(route, json=body).status_code == 401
    assert client.get(f'/stores/{store.id}').json()['card_programs'] == []
    response = client.post(route, json=body, headers={'X-Admin-Token': 'test-admin-token'})
    assert response.status_code == 200, response.text
    assert client.get(f'/stores/{store.id}').json()['card_programs'][0]['card_name'] == '토스뱅크 체크카드'


def test_estimate_api_respects_ownership(auth_client, db, draft):
    from app.models.user import User
    publication, offer = publish(db, draft)
    body = {'kind': 'store', 'item_id': draft[3].id, 'context': context(offer).model_dump(mode='json')}
    assert not auth_client.post('/stores/estimate', json=body).json()['candidates']
    user = db.scalar(select(User))
    user.card_ids = [publication.card_id]
    db.commit()
    result = auth_client.post('/stores/estimate', json=body).json()
    assert result['best']['estimated_saving'] == 500


def test_naive_effective_time_and_negative_uses_rejected():
    with pytest.raises(ValueError):
        PaymentContext(amount=1000, path='card', program_effective_at={'x': '2026-10-15T00:00:00'})
    with pytest.raises(ValueError):
        PaymentContext(amount=1000, path='card', program_daily_uses={'x': -1})


def test_not_modified_response_cannot_renew_full_source_validation(db, draft):
    publication, _ = publish(db, draft)
    _, source, snapshot, store = draft
    db.scalar(select(CardCollectionAttempt)).attempted_at = NOW - timedelta(hours=25)
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id,
        attempted_at=NOW, status='not_modified', http_status=304))
    db.commit()
    assert publication_status(db, publication, NOW) == 'source_stale'
    assert store_programs(db, store, now=NOW) == []


def test_new_observed_source_hides_previously_approved_program(db, draft):
    publication, _ = publish(db, draft)
    _, source, snapshot, store = draft
    changed = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256='a' * 64,
        semantic_sha256='b' * 64, raw_object_ref='changed', extracted_text='changed',
        mime_type='text/html', parser_version='fixture', completeness='text_available')
    db.add(changed)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=changed.id,
        attempted_at=NOW, status='captured', http_status=200))
    db.commit()
    assert publication_status(db, publication, NOW) == 'source_changed'
    assert store_programs(db, store, now=NOW) == []
    # Reversion is determined by the latest observation, not the greatest snapshot ID.
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id,
        attempted_at=NOW, status='captured', http_status=200))
    db.commit()
    assert publication_status(db, publication, NOW) == 'approved'


def test_unresolved_joopjoop_policy_cannot_be_approved(db):
    text = Path('tests/fixtures/shinhan-joopjoop-benefits.txt').read_text(encoding='utf-8')
    source = register_source(db, JOOPJOOP_URL, provider_key='shinhan', now=NOW)
    digest = hashlib.sha256(text.encode()).hexdigest()
    snapshot = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256=digest,
        semantic_sha256=digest, raw_object_ref=digest, extracted_text=text,
        mime_type='text/html', parser_version='fixture', completeness='text_available')
    db.add(snapshot)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id,
        attempted_at=NOW, status='captured', http_status=200))
    result = extract_snapshot(db, source, snapshot, now=NOW)
    db.commit()
    version = db.get(CardBenefitVersion, result['version_id'])
    with pytest.raises(HTTPException, match='미해결 조건') as exc:
        review_program(db, version.id, review_body(version, snapshot), now=NOW)
    assert exc.value.status_code == 422
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0


def test_publication_period_boundaries(db, draft):
    publication, _ = publish(db, draft)
    version, source, snapshot, _ = draft
    start = datetime.fromisoformat(version.payload['valid_from'])
    end = datetime.fromisoformat(version.payload['valid_until_exclusive'])
    publication.recheck_at = end + timedelta(hours=1)
    attempt = db.scalar(select(CardCollectionAttempt))
    attempt.attempted_at = start - timedelta(minutes=1)
    db.commit()
    assert publication_status(db, publication, start - timedelta(seconds=1)) == 'not_started'
    assert publication_status(db, publication, start) == 'approved'
    attempt.attempted_at = end - timedelta(minutes=1)
    db.commit()
    assert publication_status(db, publication, end - timedelta(seconds=1)) == 'approved'
    assert publication_status(db, publication, end) == 'expired'


def test_publication_migration_roundtrip(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    migration = importlib.import_module('migrations.versions.2bce40387651_card_publications')
    engine = create_engine('sqlite:///' + (tmp_path / 'publication.db').as_posix())
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert 'card_program_publications' in inspect(connection).get_table_names()
            migration.downgrade()
            assert 'card_program_publications' not in inspect(connection).get_table_names()
    engine.dispose()
