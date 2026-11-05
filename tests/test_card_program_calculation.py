"""Tier boundaries, KST grace windows, two shared caps and unpublished diagnostics."""
from datetime import date
from pathlib import Path

import pytest

from app.services.card_benefit_parsers import parse_hana_day
from app.services.card_program_calculation import DayScenario, calculate_day, grace_end
from tests.test_card_document_crosscheck import documents


def policy(rounding='floor_per_transaction'):
    payload = parse_hana_day(Path('tests/fixtures/hana-day-benefits.txt').read_text(encoding='utf-8'))
    for rule in payload['rules']:
        rule['rounding_rule'] = rounding  # Synthetic policy only. Never persisted or accepted from API clients.
    return payload


def scenario(**updates):
    values = {'amount': 10000, 'brand': '스타벅스', 'purchase_at': '2026-10-15T03:00:00+00:00',
        'payment_path': 'card', 'channel': 'offline', 'prior_spend_krw': 500000,
        'prior_spend_month': '2026-09', 'prior_spend_exclusions_confirmed': True,
        'first_use_registered_on': '2025-01-01', 'registration_date_confirmed': True,
        'remaining_caps': {'program:hana_day_offline:coffee:2026-10': 5000,
            'program:hana_day_offline:program_total:2026-10': 30000},
        'excluded_item_present': False, 'excluded_branch': False,
        'issuer_classification_confirmed': True, 'other_conditions_confirmed': True}
    values.update(updates)
    return DayScenario(**values)


@pytest.mark.parametrize('spend,tier,status', [(499999,None,'ineligible'), (500000,'standard','eligible'),
    (999999,'standard','eligible'), (1000000,'higher','eligible')])
def test_spend_tier_boundaries(spend, tier, status):
    result = calculate_day(policy(), scenario(prior_spend_krw=spend))
    assert result['tier'] == tier and result['status'] == status
    assert result['estimated_saving'] == (1000 if status == 'eligible' else None)


@pytest.mark.parametrize('area,total,expected,status', [(5000,30000,1000,'eligible'), (800,300,300,'eligible'),
    (0,30000,None,'ineligible'), (5000,0,None,'ineligible'), (5001,30000,None,'conditional')])
def test_area_and_integrated_caps_apply_together(area,total,expected,status):
    context = scenario(remaining_caps={'program:hana_day_offline:coffee:2026-10':area,
        'program:hana_day_offline:program_total:2026-10':total})
    result = calculate_day(policy(), context)
    assert result['estimated_saving'] == expected and result['status'] == status


def test_higher_tier_has_higher_caps_and_shared_keys_across_brands():
    values = {'program:hana_day_offline:coffee:2026-10':10000, 'program:hana_day_offline:program_total:2026-10':50000}
    result = calculate_day(policy(), scenario(amount=200000, prior_spend_krw=1000000, remaining_caps=values))
    assert result['estimated_saving'] == 10000
    other = calculate_day(policy(), scenario(brand='커피빈'))
    assert result['cap_keys'] == other['cap_keys']
    shopping = calculate_day(policy(), scenario(brand='이마트', remaining_caps={
        'program:hana_day_offline:shopping:2026-10':5000, 'program:hana_day_offline:program_total:2026-10':300}))
    assert shopping['cap_keys'][1] == result['cap_keys'][1] and shopping['cap_keys'][0] != result['cap_keys'][0]
    assert shopping['estimated_saving'] == 300


@pytest.mark.parametrize('registered,end', [('2026-01-31','2026-02-28'), ('2028-01-31','2028-02-29'), ('2026-12-31','2027-01-31')])
def test_calendar_grace_end(registered,end):
    assert grace_end(date.fromisoformat(registered)) == date.fromisoformat(end)


def test_grace_last_second_and_kst_month_change():
    context = scenario(prior_spend_krw=0, first_use_registered_on='2026-09-30', purchase_at='2026-10-31T14:59:59+00:00')
    result = calculate_day(policy(), context)
    assert result['grace_eligible'] is True and result['tier'] == 'standard' and result['estimated_saving'] == 1000
    context = scenario(prior_spend_krw=0, prior_spend_month='2026-10', first_use_registered_on='2026-09-30',
        purchase_at='2026-10-31T15:00:00+00:00', remaining_caps={
            'program:hana_day_offline:coffee:2026-11':5000,'program:hana_day_offline:program_total:2026-11':30000})
    result = calculate_day(policy(), context)
    assert result['status'] == 'ineligible' and result['grace_eligible'] is False
    assert all(key.endswith('2026-11') for key in result['cap_keys'])


def test_grace_does_not_reduce_earned_higher_tier_and_unknown_not_assumed():
    result = calculate_day(policy(), scenario(prior_spend_krw=1000000, first_use_registered_on='2026-10-01'))
    assert result['tier'] == 'higher'
    result = calculate_day(policy(), scenario(prior_spend_krw=0, first_use_registered_on=None, registration_date_confirmed=False))
    assert result['status'] == 'conditional' and result['estimated_saving'] is None
    result = calculate_day(policy(), scenario(prior_spend_krw=None, first_use_registered_on='2026-10-01'))
    assert result['status'] == 'conditional' and result['tier'] is None


@pytest.mark.parametrize('updates', [{'prior_spend_krw':None}, {'prior_spend_month':'2026-08'},
    {'prior_spend_exclusions_confirmed':False}, {'remaining_caps':{}}, {'excluded_item_present':None},
    {'excluded_branch':None}, {'issuer_classification_confirmed':False}, {'other_conditions_confirmed':False},
    {'payment_path':'kakao'}])
def test_unknown_conditions_never_produce_amount(updates):
    result = calculate_day(policy(), scenario(**updates))
    assert result['status'] == 'conditional' and result['estimated_saving'] is None


@pytest.mark.parametrize('updates', [{'brand':'토스쇼핑'}, {'channel':'online'}, {'excluded_item_present':True},
    {'excluded_branch':True}, {'first_use_registered_on':'2026-11-01'}])
def test_unsupported_or_failed_conditions(updates):
    result = calculate_day(policy(), scenario(**updates))
    assert result['status'] == 'ineligible' and result['estimated_saving'] is None


def test_unresolved_rounding_is_not_silently_floor_even_for_whole_won():
    result = calculate_day(policy('unresolved'), scenario())
    assert result['status'] == 'conditional' and result['estimated_saving'] is None
    assert result['arithmetic']['numerator'] == 10000000
    assert calculate_day(policy(), scenario(amount=10005))['estimated_saving'] == 1000
    assert calculate_day(policy('half_up_per_transaction'), scenario(amount=10005))['estimated_saving'] == 1001


@pytest.mark.parametrize('mutation', ['group', 'tier', 'rate', 'scope', 'cap'])
def test_malformed_policy_is_rejected(mutation):
    payload = policy()
    if mutation == 'group': payload['rules'][0]['limit_groups'] = ['coffee']
    elif mutation == 'tier': payload['spend_tiers'][0]['max_prior_spend_exclusive_krw'] = 900000
    elif mutation == 'rate': payload['rules'][0]['rate_bps'] = 10001
    elif mutation == 'scope': payload['rules'][0]['scope']['brands'] = []
    else: payload['limit_groups'][0]['caps_by_spend_tier_krw']['standard'] = 999999
    with pytest.raises(ValueError): calculate_day(payload, scenario())


@pytest.mark.parametrize('updates', [{'amount':-1}, {'amount':True}, {'remaining_caps':{'x':-1}},
    {'remaining_caps':{'x':True}}, {'purchase_at':'2026-10-15T00:00:00'}, {'prior_spend_krw':-1}])
def test_invalid_context_rejected(updates):
    with pytest.raises(ValueError): scenario(**updates)


def test_preview_is_admin_only_read_only_and_cannot_override_policy(client, db, documents):
    from sqlalchemy import select, func
    from app.models.card_publication import CardProgramPublication
    from app.models.map_verification import MapReviewEvent
    version = documents[0]
    url = f'/admin/map/card-programs/{version.id}/preview'
    body = {'expected_payload_hash':version.payload_hash, 'scenario':scenario().model_dump(mode='json')}
    assert client.post(url,json=body).status_code == 401
    headers = {'X-Admin-Token':'test-admin-token'}
    response = client.post(url,json=body,headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result['scenario_only'] is True and result['public_estimated_saving'] is None
    assert result['scenario_result']['estimated_saving'] is None
    assert result['publication_readiness']['supporting_document']['status'] == 'conflict'
    assert result['publication_readiness']['can_approve'] is False
    assert client.post(url,json={**body,'rounding_rule':'floor_per_transaction'},headers=headers).status_code == 422
    assert client.post(url,json={**body,'expected_payload_hash':'0'*64},headers=headers).status_code == 409
    for model in (CardProgramPublication,MapReviewEvent):
        assert db.scalar(select(func.count()).select_from(model)) == 0
