"""Synthetic policy completion tests arithmetic; actual policy remains conditional."""
from copy import deepcopy

import pytest
from sqlalchemy import select, func

from app.models.card import Card
from app.models.card_publication import CardProgramPublication
from app.models.map_verification import MapReviewEvent
from app.services.card_benefit_parsers import parse_deep_oil
from app.services.card_usage_cap_calculation import DeepOilScenario, calculate_deep_oil
from tests.test_deep_oil_extraction import text, draft_deep_oil

KEY = 'program:shinhan_deep_oil_living:living_eligible_payment:2026-10'


def policy(completed=True):
    payload = parse_deep_oil(text())
    if completed:
        # Fictional policy solely to exercise integer arithmetic. Never published.
        for rule in payload['rules']:
            rule.update(channel='any', rounding_rule='floor_per_transaction')
        payload['limit_groups'][0]['boundary_policy'] = 'partial_payment'
    return payload


def scenario(**updates):
    return DeepOilScenario.model_validate(dict(amount=10000, brand='CU',
        purchase_at='2026-10-15T03:00:00+00:00', payment_path='card', channel='offline',
        prior_spend_krw=300000, prior_spend_month='2026-09', prior_spend_basis_confirmed=True,
        first_use_registered_on='2026-01-01', registration_date_confirmed=True,
        remaining_eligible_payment_caps={KEY:150000}, family_and_all_living_services_included=True,
        excluded_item_present=False, excluded_branch=False, issuer_classification_confirmed=True,
        other_conditions_confirmed=True) | updates)


@pytest.mark.parametrize('spend,tier,status', [(299999,None,'ineligible'),(300000,'standard','eligible'),
    (699999,'standard','eligible'),(700000,'higher','eligible')])
def test_tier_boundaries(spend,tier,status):
    result = calculate_deep_oil(policy(),scenario(prior_spend_krw=spend))
    assert (result['tier'],result['status']) == (tier,status)


@pytest.mark.parametrize('amount,remaining,saving', [(10000,150000,500),(10000,300,15),
    (200000,150000,7500),(400000,300000,15000),(10019,150000,500)])
def test_principal_cap_applies_before_rate(amount,remaining,saving):
    result = calculate_deep_oil(policy(), scenario(amount=amount, remaining_eligible_payment_caps={KEY:remaining},
        prior_spend_krw=700000 if remaining==300000 else 300000))
    assert result['estimated_saving'] == saving
    assert result['arithmetic']['eligible_payment_krw'] == min(amount,remaining)
    assert result['cap_unit'] == 'eligible_payment_krw'


def test_all_brands_share_one_family_taxi_principal_cap():
    results = [calculate_deep_oil(policy(),scenario(brand=b)) for b in ['CU','GS25','스타벅스','이디야']]
    assert all(r['cap_keys']==[KEY] and r['estimated_saving']==500 for r in results)
    assert calculate_deep_oil(policy(),scenario(brand='택시'))['status']=='ineligible'
    result = calculate_deep_oil(policy(),scenario(family_and_all_living_services_included=False))
    assert result['status']=='conditional' and result['estimated_saving'] is None


def test_actual_policy_never_assumes_channel_rounding_or_partial_cap():
    result = calculate_deep_oil(policy(False),scenario(amount=200000))
    assert result['estimated_saving'] is None and result['status']=='conditional'
    assert any('채널' in reason for reason in result['unknown'])
    assert any('원 단위' in reason for reason in result['unknown'])
    assert any('부분 적용' in reason for reason in result['unknown'])
    assert result['arithmetic']['eligible_payment_krw'] is None
    within = calculate_deep_oil(policy(False),scenario())
    assert within['arithmetic']['eligible_payment_krw']==10000
    assert within['estimated_saving'] is None


def test_boundary_policy_and_rounding_are_independent():
    payload = policy()
    payload['limit_groups'][0]['boundary_policy']='unresolved'
    assert calculate_deep_oil(payload,scenario(amount=150001))['status']=='conditional'
    assert calculate_deep_oil(payload,scenario(amount=150000))['estimated_saving']==7500
    payload['limit_groups'][0]['boundary_policy']='whole_transaction_only'
    assert calculate_deep_oil(payload,scenario(amount=150001))['status']=='ineligible'
    payload['rules'][0]['rounding_rule']='half_up_per_transaction'
    assert calculate_deep_oil(payload,scenario(amount=10010))['estimated_saving']==501


def test_grace_uses_previous_month_not_current_month_and_handles_kst_boundary():
    result = calculate_deep_oil(policy(),scenario(prior_spend_krw=0,first_use_registered_on='2026-09-30',
        purchase_at='2026-10-31T14:59:59+00:00'))
    assert result['tier']=='standard' and result['grace_eligible'] is True
    result = calculate_deep_oil(policy(),scenario(prior_spend_krw=700000,first_use_registered_on='2026-10-01'))
    assert result['tier']=='higher'
    result = calculate_deep_oil(policy(),scenario(prior_spend_krw=0,first_use_registered_on='2026-09-30',
        prior_spend_month='2026-10',purchase_at='2026-10-31T15:00:00+00:00'))
    assert result['status']=='ineligible' and result['grace_eligible'] is False
    assert result['cap_keys'][0].endswith('2026-11')


@pytest.mark.parametrize('updates', [dict(prior_spend_krw=None), dict(prior_spend_month='2026-10'),
    dict(prior_spend_basis_confirmed=False), dict(remaining_eligible_payment_caps={}),
    dict(remaining_eligible_payment_caps={KEY:150001}), dict(excluded_item_present=None),
    dict(excluded_branch=None),dict(issuer_classification_confirmed=False),dict(other_conditions_confirmed=False),
    dict(payment_path='kakao')])
def test_missing_or_inconsistent_context_never_produces_amount(updates):
    result = calculate_deep_oil(policy(),scenario(**updates))
    assert result['status']=='conditional' and result['estimated_saving'] is None


@pytest.mark.parametrize('updates', [dict(remaining_eligible_payment_caps={KEY:0}),
    dict(excluded_item_present=True),dict(excluded_branch=True),dict(first_use_registered_on='2026-11-01')])
def test_failed_conditions_block(updates):
    result = calculate_deep_oil(policy(),scenario(**updates))
    assert result['status']=='ineligible' and result['estimated_saving'] is None


@pytest.mark.parametrize('updates', [dict(amount=True),dict(remaining_eligible_payment_caps={KEY:True}),
    dict(remaining_eligible_payment_caps={KEY:-1}),dict(remaining_caps={KEY:150000}),
    dict(rounding_rule='floor_per_transaction'),dict(current_spend_krw=700000),
    dict(purchase_at='2026-10-15T03:00:00')])
def test_invalid_or_mixed_units_and_policy_override_rejected(updates):
    with pytest.raises(ValueError): scenario(**updates)


@pytest.mark.parametrize('mutation', ['unit','family','taxi','tier','basis','grace','cap','rule'])
def test_invalid_policy_rejected(mutation):
    payload = deepcopy(policy())
    group = payload['limit_groups'][0]
    if mutation=='unit': group['cap_unit']='reward_krw'
    elif mutation=='family': group['shared_with_family_cards']=False
    elif mutation=='taxi': group['includes_unextracted_services']=[]
    elif mutation=='tier': payload['spend_tiers'][0]['max_prior_spend_exclusive_krw']=600000
    elif mutation=='basis': payload['prior_spend_basis']['transport']='previous_month'
    elif mutation=='grace': payload['new_card_grace']['higher_tier_effective']='immediate'
    elif mutation=='cap': group['caps_by_spend_tier_krw']['standard']=-1
    else: payload['rules'][0]['scope']['brands']=[]
    with pytest.raises(ValueError): calculate_deep_oil(payload,scenario())


def test_admin_preview_is_read_only_and_never_changes_public_eligibility(client,db):
    version = draft_deep_oil(db)
    url = f'/admin/map/card-programs/{version.id}/preview-usage-cap'
    body = {'expected_payload_hash':version.payload_hash,'scenario':scenario().model_dump(mode='json')}
    assert client.post(url,json=body).status_code==401
    headers = {'X-Admin-Token':'test-admin-token'}
    response = client.post(url,json=body,headers=headers)
    assert response.status_code==200
    result = response.json()
    assert result['public_estimated_saving'] is None and result['scenario_only'] is True
    assert result['scenario_result']['estimated_saving'] is None
    assert result['publication_readiness']['can_approve'] is False
    assert client.post(url,json=body|{'expected_payload_hash':'0'*64},headers=headers).status_code==409
    assert client.post(url,json=body|{'rounding_rule':'floor_per_transaction'},headers=headers).status_code==422
    assert client.post(url,json=body|{'scenario':body['scenario']|{'remaining_caps':{}}},headers=headers).status_code==422
    assert client.post('/admin/map/card-programs/99999/preview-usage-cap',json=body,headers=headers).status_code==404
    for model in (Card,CardProgramPublication,MapReviewEvent):
        assert db.scalar(select(func.count()).select_from(model))==0
