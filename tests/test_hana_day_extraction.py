"""Real archived Day conditions, shared caps, exclusions and fail-closed posting."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib

import pytest
from sqlalchemy import select, func

from app.models.card import Card
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence, CardBenefitExtraction
from app.models.card_publication import CardProgramPublication
from app.services.card_collection import register_source, register_product
from app.services.card_benefit_parsers import HANA_DAY_URL, parse_hana_day
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_publication import program_review_status

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def text():
    return Path('tests/fixtures/hana-day-benefits.txt').read_text(encoding='utf-8')


def test_offline_rules_spend_tiers_and_all_area_integrated_cap():
    result = parse_hana_day(text())
    coffee, shopping = result['rules']
    assert [rule['rate_bps'] for rule in result['rules']] == [1000, 1000]
    assert coffee['scope']['brands'] == ['스타벅스', '커피빈', '이디야', '투썸플레이스', '할리스']
    assert shopping['scope']['brands'] == ['이마트', '롯데마트', '홈플러스']
    assert shopping['limit_groups'] == ['shopping', 'program_total']
    assert all(rule['reward_method'] == 'statement_discount' and rule['rounding_rule'] == 'unresolved'
        for rule in result['rules'])
    assert result['spend_tiers'] == [
        {'tier_key': 'standard', 'min_prior_spend_krw': 500000, 'max_prior_spend_exclusive_krw': 1000000},
        {'tier_key': 'higher', 'min_prior_spend_krw': 1000000, 'max_prior_spend_exclusive_krw': None}]
    assert result['limit_groups'][0]['caps_by_spend_tier_krw'] == {'standard': 5000, 'higher': 10000}
    total = result['limit_groups'][-1]
    assert total['caps_by_spend_tier_krw'] == {'standard': 30000, 'higher': 50000}
    assert total['includes_unextracted_areas'] is True
    assert len(result['all_area_caps_by_spend_tier_krw']) == 8
    assert '토스쇼핑' not in str(result['rules'])
    assert 'toss_shopping_online' in result['outside_scope']


def test_grace_spend_exclusions_and_indefinite_period_not_fabricated():
    result = parse_hana_day(text())
    assert result['new_card_grace']['until'] == 'end_of_following_month_kst'
    assert result['new_card_grace']['applied_tier'] == 'standard'
    assert '할인받은 매출 전체 제외' in result['spend_exclusions_raw']
    assert '배달앱' in result['rules'][0]['exclusions_raw']
    assert '창고형 할인매장' in result['rules'][1]['exclusions_raw']
    assert result['period_status'] == 'end_not_stated'
    assert result['valid_from'] is result['valid_until_exclusive'] is None
    assert 'period_policy_not_supported' in result['issues']


@pytest.mark.parametrize('before,after', [
    ('8대 생활영역 10% 할인', '8대 생활영역 12% 할인'),
    ('통합 월 할인 한도 3만원 5만원', '통합 월 할인 한도 3천원 5만원'),
    ('50만 이상 100만 이상', '60만 이상 100만 이상'),
    ('이용금액 50만원/100만원 이상', '이용금액 50만원/200만원 이상'),
    ('50만원 미만이어도 50만원 이상 100만원 미만', '50만원 미만이어도 100만원 이상 200만원 미만'),
    ('할인받은 매출 전체 제외', '할인받은 매출 전체 포함'),
    ('공식 앱을 통한 결제 건에 한하여', '오프라인 결제 건에 한하여'),
    ('오프라인 결제건에 한하여', '모든 결제건에 대하여'),
    ('모든 할인 서비스는 하나카드에 등록된', '모든 할인 서비스는 지도에 등록된'),
    ('이마트, 롯데마트, 홈플러스, 토스쇼핑 10%', '이마트, 롯데마트, 홈플러스, 새로운마트, 토스쇼핑 10%'),
    ('병원/의원 업종( 동물병원 제외 ) 10%', '병원/의원 업종( 동물병원 제외 ) 15%'),
    ('토스뱅크 하나카드 Day 청구할인 서비스', '다른 카드 청구할인 서비스'),
])
def test_conflicts_missing_policy_or_changed_scope_fail(before, after):
    assert before in text()
    with pytest.raises(ValueError):
        parse_hana_day(text().replace(before, after))


def test_source_bound_persistence_is_idempotent_and_cannot_publish(client, db):
    source = register_source(db, HANA_DAY_URL, provider_key='hanacard', issuer=('hana', '하나카드'), now=NOW)
    register_product(db, source, namespace='hanacard', code='18669', name='토스뱅크 하나카드 Day', product_type='credit')
    digest = hashlib.sha256(text().encode()).hexdigest()
    snapshot = CardSourceSnapshot(source_id=source.id, fetched_at=NOW, raw_sha256=digest,
        semantic_sha256=digest, raw_object_ref=digest, extracted_text=text(),
        mime_type='text/html', parser_version='fixture', completeness='text_available')
    db.add(snapshot)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id, attempted_at=NOW,
        status='captured', http_status=200))
    first = extract_snapshot(db, source, snapshot, now=NOW)
    assert extract_snapshot(db, source, snapshot, now=NOW)['created'] is False
    db.commit()
    version = db.get(CardBenefitVersion, first['version_id'])
    assert version.extractor_version == 'hana_day_offline_v1'
    assert version.review_state == 'pending' and version.publication_state == 'draft'
    for evidence in db.scalars(select(CardBenefitFieldEvidence)):
        assert text()[evidence.start_offset:evidence.end_offset] == evidence.quote
    readiness = program_review_status(db, version, now=NOW)
    assert not readiness['can_approve']
    assert 'rounding_rule_unresolved' in readiness['unresolved_issues']
    assert 'period_policy_not_supported' in readiness['unresolved_issues']
    headers = {'X-Admin-Token': 'test-admin-token'}
    detail = client.get(f'/admin/map/card-programs/{version.id}', headers=headers).json()
    assert detail['payload']['program_key'] == 'hana_day_offline'
    body = {'action': 'approved', 'expected_payload_hash': version.payload_hash,
        'expected_snapshot_hash': digest, 'reviewer': 'test', 'reason': '미해결 조건을 확인하고 승인을 시도합니다.',
        'policy_quote': version.payload['exclusions_raw'], 'acknowledged_issues': version.payload['issues']}
    assert client.post(f'/admin/map/card-programs/{version.id}/review', json=body, headers=headers).status_code == 409
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
    assert db.scalar(select(func.count()).select_from(Card)) == 0
    assert db.scalar(select(func.count()).select_from(CardBenefitExtraction)) == 1
