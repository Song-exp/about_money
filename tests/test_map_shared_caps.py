import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.services.map_verification import BenefitRules, PaymentContext, estimate


def reference_rule():
    rule = json.loads(Path('data/map_kb_nori_reference.json').read_text(encoding='utf-8'))['rules']
    # Synthetic scenario confirms scope/items/exceptions; real records stay pending.
    return BenefitRules(**{**rule, 'scope_confirmed': True, 'excluded_items': [], 'other_conditions': []})


@pytest.mark.parametrize('spend,expected_cap', [(200000,10000),(299999,10000),(300000,20000),(500000,30000),(1000000,50000)])
def test_official_reference_spend_thresholds(spend, expected_cap):
    rule = reference_rule()
    rule.per_transaction_cap = None
    context = PaymentContext(amount=300000, path='card', prior_spend_groups={'kb_nori':spend}, remaining_caps={'group:kb_nori':50000})
    result = estimate(rule, context, '일반점', 'card:1', 1)
    assert result['estimated_saving'] == expected_cap
    assert result['status'] == 'eligible'


def test_shared_cap_does_not_become_separate_benefit_caps():
    context = PaymentContext(amount=20000, path='card', prior_spend_groups={'kb_nori':300000}, remaining_caps={'group:kb_nori':500})
    first = estimate(reference_rule(), context, '카페', 'card:1', 1)
    second = estimate(reference_rule(), context, '다른 카페', 'card:2', 2)
    assert first['cap_key'] == second['cap_key'] == 'group:kb_nori'
    assert first['estimated_saving'] == second['estimated_saving'] == 500


def test_per_card_spend_cannot_substitute_unknown_family_total():
    context = PaymentContext(amount=20000, path='card', prior_spend={1:300000}, remaining_caps={'group:kb_nori':20000})
    result = estimate(reference_rule(), context, '', 'card:1', 1)
    assert result['status'] == 'conditional'
    assert '동일계열 합산 전월 실적 확인 필요' in result['reasons']


def test_below_first_tier_and_per_transaction_limit():
    context = PaymentContext(amount=50000, path='card', prior_spend_groups={'kb_nori':199999}, remaining_caps={'group:kb_nori':20000})
    assert estimate(reference_rule(), context, '', 'card:1', 1)['status'] == 'ineligible'
    context.prior_spend_groups['kb_nori'] = 300000
    assert estimate(reference_rule(), context, '', 'card:1', 1)['estimated_saving'] == 4000


def test_ambiguous_or_unsorted_tiers_are_rejected():
    raw = reference_rule().model_dump(mode='json')
    with pytest.raises(ValidationError): BenefitRules(**{**raw, 'monthly_cap':10000})
    with pytest.raises(ValidationError): BenefitRules(**{**raw, 'monthly_cap_tiers':list(reversed(raw['monthly_cap_tiers']))})
