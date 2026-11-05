from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction

import pytest
from pydantic import ValidationError
from sqlalchemy import select, func

from app.models.card_benefit_version import CardBenefitVersion
from app.models.card_collection import CardCollectionAttempt
from app.models.card_publication import CardProgramPublication
from app.services.card_collection import capture_document, register_source
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_mr_life import parse_mr_life, MR_LIFE_URL
from app.services.card_time_calculation import MrLifeScenario, calculate_mr_life
from tests.test_mr_life_extraction import text

PREFIX = 'program:shinhan_mr_life_time'
CAP = PREFIX+':time_shared:2026-10'
DAY = PREFIX+':night_food:day:2026-10-17'
MONTH = PREFIX+':night_food:month:2026-10'


def policy():
    payload = parse_mr_life(text())
    payload.pop('evidence')
    return payload


def scenario(**changes):
    return MrLifeScenario.model_validate(dict(amount=10000, issuer_category='커피전문점',
        issuer_classification_confirmed=True, purchase_at='2026-10-17T22:00:00+09:00', payment_path='card',
        prior_spend_krw=300000, prior_spend_month='2026-09', prior_spend_basis_confirmed=True,
        first_use_registered_on='2026-01-01', registration_date_confirmed=True,
        remaining_reward_caps={CAP: 10000}, remaining_counts={DAY: 1, MONTH: 10},
        all_time_services_included=True, count_periods_confirmed=True, excluded_transaction=False) | changes)


@pytest.mark.parametrize('at,failed,boundary', [
    ('2026-10-17T20:59:59+09:00', True, False),
    ('2026-10-17T21:00:00+09:00', False, True),
    ('2026-10-17T21:00:00.000001+09:00', False, False),
    ('2026-10-17T23:59:59+09:00', False, False),
    ('2026-10-17T00:00:00+09:00', False, False),
    ('2026-10-17T08:59:59+09:00', False, False),
    ('2026-10-17T09:00:00+09:00', False, True),
    ('2026-10-17T09:00:00.000001+09:00', True, False),
    ('2026-10-17T13:00:00+00:00', False, False),
])
def test_overnight_window_uses_kst_and_does_not_invent_inclusive_boundaries(at, failed, boundary):
    result = calculate_mr_life(policy(), scenario(purchase_at=at))
    assert any('야간 할인 시간 밖' in v for v in result['failed']) is failed
    assert any('시간 경계' in v for v in result['unknown']) is boundary
    assert result['estimated_saving'] is None


@pytest.mark.parametrize('spend,tier', [(299999,None),(300000,'standard'),(499999,'standard'),
    (500000,'middle'),(999999,'middle'),(1000000,'higher')])
def test_spend_tier_boundaries(spend, tier):
    result = calculate_mr_life(policy(), scenario(prior_spend_krw=spend))
    assert result['tier'] == tier
    if tier is None: assert result['status'] == 'ineligible'


@pytest.mark.parametrize('amount,remaining', [(10000,10000),(30000,10000),(7000,300),(3,10000),(1,1)])
def test_shared_reward_cap_is_applied_after_transaction_cap_with_integer_arithmetic(amount, remaining):
    result = calculate_mr_life(policy(), scenario(amount=amount, remaining_reward_caps={CAP: remaining}))
    expected = min(Fraction(min(amount,10000),10), 1000, remaining)
    assert Fraction(result['arithmetic']['conditional_reward_numerator'],10000) == expected
    assert result['arithmetic']['eligible_payment_krw'] == min(amount,10000)
    assert result['status'] == 'conditional' and result['estimated_saving'] is None


def test_convenience_and_all_food_categories_share_one_cap_and_food_counts():
    coffee = calculate_mr_life(policy(), scenario())
    food = calculate_mr_life(policy(), scenario(issuer_category='한식'))
    convenience = calculate_mr_life(policy(), scenario(issuer_category='편의점', purchase_at='2026-10-17T12:00:00+09:00'))
    assert coffee['cap_keys'] == food['cap_keys'] == convenience['cap_keys'] == [CAP]
    assert coffee['count_keys'] == food['count_keys']
    assert coffee['count_keys'] != convenience['count_keys']
    assert not any('야간' in v for v in convenience['failed']+convenience['unknown'])
    exhausted = calculate_mr_life(policy(), scenario(remaining_reward_caps={CAP:0}))
    assert exhausted['status'] == 'ineligible'


@pytest.mark.parametrize('changes,word', [
    (dict(issuer_classification_confirmed=False), '카드사 업종'),
    (dict(remaining_reward_caps={}), '잔여 할인액 누락'),
    (dict(remaining_reward_caps={CAP:10001}), '최대 한도'),
    (dict(remaining_counts={}), '잔여 횟수 누락'),
    (dict(remaining_counts={DAY:2, MONTH:10}), '최대 횟수'),
    (dict(all_time_services_included=False), '통합 잔여'),
    (dict(count_periods_confirmed=False), 'KST 일·월'),
    (dict(prior_spend_month='2026-08'), '전월 실적'),
    (dict(payment_path='kakao'), '간편결제'),
])
def test_unknowns_cannot_become_confirmed_savings(changes, word):
    result = calculate_mr_life(policy(), scenario(**changes))
    assert result['status'] == 'conditional' and result['estimated_saving'] is None
    assert any(word in v for v in result['unknown'])


@pytest.mark.parametrize('changes', [dict(payment_path='cash'), dict(issuer_category='스타벅스'),
    dict(excluded_transaction=True), dict(remaining_counts={DAY:0,MONTH:10}),
    dict(first_use_registered_on='2026-11-01')])
def test_excluded_and_unsupported_scenarios_fail(changes):
    assert calculate_mr_life(policy(), scenario(**changes))['status'] == 'ineligible'


def test_new_card_grace_and_kst_month_rollover():
    result = calculate_mr_life(policy(), scenario(prior_spend_krw=0,first_use_registered_on='2026-09-30'))
    assert result['tier'] == 'standard' and result['grace_eligible']
    ended = calculate_mr_life(policy(), scenario(prior_spend_krw=0,first_use_registered_on='2026-08-31'))
    assert ended['status'] == 'ineligible' and not ended['grace_eligible']
    month = calculate_mr_life(policy(), scenario(purchase_at='2026-10-31T15:30:00+00:00'))
    assert month['cap_keys'] == [PREFIX+':time_shared:2026-11']
    assert month['count_keys'][0].endswith(':2026-11-01')
    assert month['tier'] is None


@pytest.mark.parametrize('changes', [dict(amount=True),dict(amount='10000'),dict(issuer_classification_confirmed='yes'),
    dict(remaining_reward_caps={CAP:-1}),dict(remaining_counts={DAY:True}),dict(purchase_at='2026-10-17T22:00:00'),
    dict(rounding_rule='floor_per_transaction'),dict(time_boundary_policy='inclusive')])
def test_request_cannot_override_missing_official_policy_or_coerce_inputs(changes):
    with pytest.raises(ValidationError): scenario(**changes)


def test_mutated_policy_is_rejected():
    for key, value in [('rate_bps',2000),('monthly_count_cap',99),('rounding_rule','floor_per_transaction')]:
        payload = deepcopy(policy())
        payload['rules'][0][key] = value
        with pytest.raises(ValueError, match='time_policy_contract_changed'):
            calculate_mr_life(payload, scenario())


def test_api_is_admin_only_hash_bound_and_read_only(client, db, tmp_path):
    now = datetime.now(timezone.utc)
    source = register_source(db, MR_LIFE_URL, provider_key='shinhan', issuer=('shinhan','신한카드'),now=now)
    snapshot = capture_document(db, source, ('<main>'+text()+'</main>').encode(),now=now,archive_root=tmp_path)
    db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=snapshot.id,status='captured',http_status=200,attempted_at=now))
    result = extract_snapshot(db,source,snapshot,now=now)
    db.commit()
    version = db.get(CardBenefitVersion,result['version_id'])
    path = f'/admin/map/card-programs/{version.id}/preview-time'
    body = {'expected_payload_hash':version.payload_hash, 'scenario':scenario().model_dump(mode='json')}
    assert client.post(path,json=body).status_code in (401,403)
    headers = {'X-Admin-Token':'test-admin-token'}
    response = client.post(path,json=body,headers=headers)
    assert response.status_code == 200
    assert response.json()['public_estimated_saving'] is None
    assert response.json()['scenario_result']['estimated_saving'] is None
    assert not response.json()['publication_readiness']['can_approve']
    assert client.post(path,json=body|{'expected_payload_hash':'0'*64},headers=headers).status_code == 409
    version.payload = version.payload|{'tampered':True}
    db.commit()
    assert client.post(path,json=body,headers=headers).status_code == 409
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
