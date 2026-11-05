"""Purchase-amount caps must never become cash-reward caps or per-brand quotas."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app.models.card import Card
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence
from app.models.card_publication import CardProgramPublication
from app.services.card_collection import register_source, register_product
from app.services.card_benefit_parsers import DEEP_OIL_URL, parse_deep_oil
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_publication import program_review_status
from app.services.card_program_calculation import DayPolicy

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def text():
    return Path('tests/fixtures/shinhan-deep-oil-benefits.txt').read_text(encoding='utf-8')


def test_living_rules_and_cap_currency_are_distinct_from_discount_amount():
    payload = parse_deep_oil(text())
    convenience, coffee = payload['rules']
    assert convenience['scope']['brands'] == ['GS25', 'CU']
    assert coffee['scope']['brands'] == ['스타벅스', '이디야']
    assert all(rule['rate_bps'] == 500 and rule['reward_method'] == 'statement_discount' for rule in payload['rules'])
    assert convenience['limit_group'] == coffee['limit_group'] == 'living_eligible_payment'
    group = payload['limit_groups'][0]
    assert group['cap_unit'] == 'eligible_payment_krw'
    assert group['caps_by_spend_tier_krw'] == {'standard':150000, 'higher':300000}
    assert group['shared_with_family_cards'] is True
    assert group['includes_unextracted_services'] == ['taxi']
    assert 'monthly_amount_cap_krw' not in group
    assert 'monthly_reward_cap_krw' not in group
    assert '상품권 구입' in coffee['additional_exclusions_raw']
    assert 'additional_exclusions_raw' not in convenience
    assert all(rule['channel'] == 'unresolved' for rule in payload['rules'])


def test_spend_grace_basis_and_no_fabricated_expiry_or_default_eligibility():
    payload = parse_deep_oil(text())
    assert payload['spend_tiers'] == [
        {'tier_key':'standard','min_prior_spend_krw':300000,'max_prior_spend_exclusive_krw':700000},
        {'tier_key':'higher','min_prior_spend_krw':700000,'max_prior_spend_exclusive_krw':None}]
    assert payload['new_card_grace']['higher_tier_effective'] == 'following_month'
    assert payload['new_card_grace']['higher_spend_threshold_krw'] == 700000
    assert payload['prior_spend_basis']['transport'] == 'two_months_before'
    assert payload['prior_spend_basis']['family_cards_included'] is True
    assert '모든 주유소 주유 및 LPG' in payload['spend_exclusions_raw']
    assert payload['period_status'] == 'end_not_stated'
    assert payload['valid_from'] is payload['valid_until_exclusive'] is None
    assert 'cinema' in payload['outside_scope'] and 'fuel' in payload['outside_scope']
    with pytest.raises(ValueError):
        DayPolicy.model_validate(payload)  # Do not route a purchase-cap policy through a reward-cap engine.


@pytest.mark.parametrize('before,after', [
    ('생활서비스 5% 할인','생활서비스 10% 할인'),
    ('편의점(GS25, CU) 5%','편의점(GS25, CU) 10%'),
    ('커피 스타벅스, 이디야 5%','커피 스타벅스, 이디야 7%'),
    ('생활서비스 월 이용금액 한도','생활서비스 월 할인금액 한도'),
    ('30만원 이상 70만원 미만 15만원 15만원 15만원 1회','30만원 이상 80만원 미만 15만원 15만원 15만원 1회'),
    ('’30만원 이상~70만원 미만’','’30만원 이상~80만원 미만’'),
    ('해당 기간 중 70만원 이상 이용','해당 기간 중 80만원 이상 이용'),
    ('본인+가족카드 통합 적용','본인카드 별도 적용'),
    ('할인대상 편의점+커피+택시 통합','할인대상 편의점+커피 별도'),
    ('모든 주유소 주유 및 LPG 이용금액','일부 주유소 이용금액'),
    ('상품권 구입, 선불카드 충전은 할인 제외','상품권 구입, 선불카드 충전은 할인 제공'),
    ('전월 이용금액 유의 사항','다른 이용금액 유의 사항'),
])
def test_changed_unit_scope_or_conflicting_terms_fail(before, after):
    assert before in text()
    with pytest.raises(ValueError):
        parse_deep_oil(text().replace(before,after))


def draft_deep_oil(db):
    source = register_source(db, DEEP_OIL_URL, provider_key='shinhan', issuer=('shinhan','신한카드'), now=NOW)
    register_product(db,source,namespace='shinhan',code='1188274_2207',name='신한카드 Deep Oil',product_type='credit')
    digest = hashlib.sha256(text().encode()).hexdigest()
    snapshot = CardSourceSnapshot(source_id=source.id,fetched_at=NOW,raw_sha256=digest,semantic_sha256=digest,
        raw_object_ref=digest,extracted_text=text(),mime_type='text/html',parser_version='fixture',completeness='text_available')
    db.add(snapshot)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=snapshot.id,status='captured',http_status=200,attempted_at=NOW))
    result = extract_snapshot(db,source,snapshot,now=NOW)
    assert extract_snapshot(db,source,snapshot,now=NOW)['created'] is False
    db.commit()
    version = db.get(CardBenefitVersion,result['version_id'])
    return version


def test_persistence_admin_review_and_publication_remain_fail_closed(client,db):
    version = draft_deep_oil(db)
    digest = db.get(CardSourceSnapshot, version.snapshot_id).raw_sha256
    assert version.review_state == 'pending' and version.publication_state == 'draft'
    for evidence in db.scalars(select(CardBenefitFieldEvidence)):
        assert text()[evidence.start_offset:evidence.end_offset] == evidence.quote
    readiness = program_review_status(db,version,now=NOW)
    assert not readiness['can_approve']
    assert 'eligible_payment_cap_engine_not_supported' in readiness['unresolved_issues']
    headers = {'X-Admin-Token':'test-admin-token'}
    assert client.get(f'/admin/map/card-programs/{version.id}',headers=headers).json()['readiness']['can_approve'] is False
    body = {'action':'approved','expected_payload_hash':version.payload_hash,'expected_snapshot_hash':digest,
        'reviewer':'test','reason':'미지원 계산 정책의 승인 차단을 검증합니다.',
        'policy_quote':version.payload['exclusions_raw'],'acknowledged_issues':version.payload['issues']}
    blocked = client.post(f'/admin/map/card-programs/{version.id}/review',headers=headers,json=body)
    assert blocked.status_code == 409 and blocked.json()['detail'] == 'supporting_document_source_unavailable'
    preview = {'expected_payload_hash':version.payload_hash,'scenario':{'amount':10000,'brand':'CU',
        'purchase_at':NOW.isoformat(),'payment_path':'card','channel':'offline'}}
    assert client.post(f'/admin/map/card-programs/{version.id}/preview',headers=headers,json=preview).status_code == 422
    assert db.scalar(select(func.count()).select_from(Card)) == 0
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
