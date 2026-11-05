"""Actual archived text, numeric conflicts, user-condition separation and immutability."""
import hashlib
import importlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.models.card import Card, CardBenefit
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence, CardBenefitExtraction
from app.services.card_benefit_parsers import parse_toss, parse_joopjoop, TOSS_URL, JOOPJOOP_URL
from app.services.card_benefit_extraction import extract_snapshot, extract_stored_sources
from app.services.card_collection import register_source, collect_due

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def fixture(name):
    return Path('tests/fixtures/' + name + '-benefits.txt').read_text(encoding='utf-8')


def snapshot(db, text, url=TOSS_URL):
    source = register_source(db, url, provider_key='test', now=NOW)
    digest = hashlib.sha256(text.encode()).hexdigest()
    row = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256=digest,
        semantic_sha256=digest, raw_object_ref=digest[:2] + '/' + digest, extracted_text=text,
        mime_type='text/html', parser_version='fixture', completeness='text_available')
    db.add(row)
    db.flush()
    return source, row


def test_toss_dates_tiers_and_shared_area_limits():
    result = parse_toss(fixture('toss-offline'))
    assert result['valid_from'] == '2026-03-31T15:00:00+00:00'
    assert result['valid_until_exclusive'] == '2027-01-20T15:00:00+00:00'
    assert result['season_id'] == '6'
    assert result['early_end_possible'] is True
    assert result['stacking']['other_switch_modes'] is False
    assert result['selection'] == {'mode': 'offline', 'effective': 'next_day_00_kst'}
    assert len(result['rules']) == len(result['limit_groups']) == 6
    food = next(r for r in result['rules'] if r['rule_key'] == 'food_drink')
    assert '맥도날드' in food['scope']['brands'] and '스타벅스' in food['scope']['brands']
    assert food['tiers'] == [
        {'min_payment_krw': 3000, 'max_payment_exclusive_krw': 10000, 'amount_krw': 100},
        {'min_payment_krw': 10000, 'max_payment_exclusive_krw': None, 'amount_krw': 500}]
    assert all(g['daily_count_cap'] == 1 and g['monthly_count_cap'] == 10 and g['monthly_amount_cap_krw'] == 5000
               and g['shared_across_brands'] for g in result['limit_groups'])
    assert 'transport' in result['outside_scope']
    assert food['simple_pay'] == 'may_be_excluded'


@pytest.mark.parametrize('old,new', [
    ('7개 영역별 1만원 이상 결제 시 500원', '7개 영역별 1만원 이상 결제 시 700원'),
    ('영역별 최대 월 5,000원', '영역별 최대 월 9,000원'),
    ('신청 익일 00시부터', '신청 즉시부터'),
    ('2026.04.01.~2027.01.20.', '2028.04.01.~2027.01.20.'),
    ('27년 1월 20일까지', '27년 1월 21일까지'),
    ('최소 결제 금액(3,000원)', '최소 결제 금액(5,000원)'),
    ('영화관 공식 홈페이지 및 앱 또는 현장', '영화관 현장'),
])
def test_toss_conflicts_and_unknown_changes_fail_closed(old, new):
    text = fixture('toss-offline')
    assert old in text
    with pytest.raises(ValueError):
        parse_toss(text.replace(old, new))


def test_joopjoop_basis_and_batch_minimum_separated():
    result = parse_joopjoop(fixture('shinhan-joopjoop'))
    assert result['period_status'] == 'end_not_stated'
    assert result['valid_until_exclusive'] is None
    assert len(result['rules']) == 2
    base, early = result['rules']
    assert base['rate_bps'] == early['rate_bps'] == 100
    assert [g['aggregation_basis'] for g in result['limit_groups']] == ['approval_date', 'early_payment_date']
    assert [g['monthly_amount_cap_krw'] for g in result['limit_groups']] == [20000, 20000]
    assert early['additional_conditions']['min_early_payment_batch_krw'] == 10000
    assert early['additional_conditions']['within_days_from_acquisition_inclusive'] == 5
    assert 'min_payment_krw' not in early  # Batch minimum is not a purchase minimum.
    assert 'rounding_rule_unresolved' in result['issues']


def test_joopjoop_table_and_detail_cap_conflict():
    text = fixture('shinhan-joopjoop')
    with pytest.raises(ValueError, match='cap_conflict'):
        parse_joopjoop(text.replace('(승인일자 기준) 2만원', '(승인일자 기준) 3만원'))


def test_pending_version_evidence_idempotency_and_changed_policy(db):
    text = fixture('toss-offline')
    source, original = snapshot(db, text)
    first = extract_snapshot(db, source, original)
    again = extract_snapshot(db, source, original)
    db.commit()
    assert first['created'] and again['created'] is False
    version = db.get(CardBenefitVersion, first['version_id'])
    assert version.review_state == 'pending' and version.publication_state == 'draft'
    original_hash = version.payload_hash
    for evidence in db.scalars(select(CardBenefitFieldEvidence)):
        assert text[evidence.start_offset:evidence.end_offset] == evidence.quote
    changed = text.replace('이 프로모션은 토스뱅크 및 제휴업체의 사정으로 변경 또는 중단될 수 있어요.',
                           '이 프로모션은 조기 종료될 수 있어요.')
    _, new_snapshot = snapshot(db, changed)
    second = extract_snapshot(db, source, new_snapshot)
    db.commit()
    assert second['version_id'] != first['version_id']
    assert db.get(CardBenefitVersion, first['version_id']).payload_hash == original_hash
    assert db.scalar(select(func.count()).select_from(CardBenefitExtraction)) == 2
    assert db.scalar(select(func.count()).select_from(Card)) == db.scalar(select(func.count()).select_from(CardBenefit)) == 0


def test_failed_extraction_retains_reason_without_version(db):
    source, row = snapshot(db, fixture('toss-offline').replace('신청 익일 00시부터', '신청 즉시부터'))
    result = extract_snapshot(db, source, row)
    db.commit()
    assert result['status'] == 'failed'
    assert db.scalar(select(func.count()).select_from(CardBenefitVersion)) == 0
    assert db.scalar(select(CardBenefitExtraction)).issues == ['benefit_required_evidence_missing:selection']
    assert extract_snapshot(db, source, row)['created'] is False


def test_reparse_uses_latest_observed_snapshot_when_content_reverts(db):
    source, first = snapshot(db, fixture('toss-offline'))
    _, newer_id = snapshot(db, fixture('toss-offline') + ' 추가 고지문')
    for index, row in enumerate((newer_id, first)):
        db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=row.id,
            attempted_at=NOW + timedelta(hours=index), status='captured', http_status=200))
    db.commit()
    results = extract_stored_sources(db)
    assert db.get(CardBenefitVersion, results[0]['version_id']).snapshot_id == first.id


def test_collection_extracts_before_commit_without_publishing(db, tmp_path):
    register_source(db, TOSS_URL, provider_key='tossbank', now=NOW)
    db.commit()
    body = ('<html><meta charset="utf-8"><main>' + fixture('toss-offline') + '</main></html>').encode()
    def handle(request):
        return httpx.Response(404) if request.url.path == '/robots.txt' else httpx.Response(200, content=body)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['captured'] == result['parsed_versions'] == 1
    assert result['extraction_failures'] == result['auto_approved'] == 0
    assert db.scalar(select(CardBenefitVersion)).publication_state == 'draft'


def test_collection_reports_extraction_failure_separately_from_capture(db, tmp_path):
    register_source(db, TOSS_URL, provider_key='tossbank', now=NOW)
    db.commit()
    body = ('<meta charset="utf-8"><main>' + fixture('toss-offline').replace('신청 익일 00시부터', '신청 즉시부터') + '</main>').encode()
    def handle(request):
        return httpx.Response(404) if request.url.path == '/robots.txt' else httpx.Response(200, content=body)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['status'] == 'degraded'
    assert result['captured'] == result['extraction_failures'] == 1
    assert result['failed'] == result['parsed_versions'] == result['auto_approved'] == 0
    assert db.scalar(select(CardSourceSnapshot)) is not None
    assert db.scalar(select(CardBenefitExtraction)).status == 'failed'


def test_version_migration_roundtrip(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    from app.models.card_collection import COLLECTION_MODELS
    migration = importlib.import_module('migrations.versions.1abd3f276540_card_benefit_versions')
    engine = create_engine('sqlite:///' + (tmp_path / 'benefit-migration.db').as_posix())
    with engine.begin() as connection:
        Card.__table__.create(connection)
        for model in COLLECTION_MODELS:
            model.__table__.create(connection)
        before = set(inspect(connection).get_table_names())
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
        assert set(inspect(connection).get_table_names()) == before
    engine.dispose()
