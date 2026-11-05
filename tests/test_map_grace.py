import pytest
from app.services.map_verification import BenefitRules, PaymentContext, estimate


def rule():
    return BenefitRules(type='percent',value=20,min_payment=10000,per_transaction_cap=4000,
        prior_spend_required=200000,monthly_cap_tiers=[{'min_spend':200000,'cap':10000},{'min_spend':300000,'cap':20000}],
        monthly_cap_group='kb_nori',prior_spend_group='kb_nori',grace_monthly_cap=10000,
        payment_paths=['card'],scope_confirmed=True)


@pytest.mark.parametrize('grace,spend,status,saving', [
    (None,0,'conditional',4000),
    ('eligible',0,'eligible',4000),
    ('not_eligible',0,'ineligible',0),
    ('not_eligible',300000,'eligible',4000),
    ('eligible',300000,'conditional',4000),
])
def test_grace_never_assumed_from_missing_information(grace,spend,status,saving):
    context=PaymentContext(amount=30000,path='card',prior_spend_groups={'kb_nori':spend},
        grace_status_groups={} if grace is None else {'kb_nori':grace},remaining_caps={'group:kb_nori':20000})
    result=estimate(rule(),context,'일반점','card:1',1)
    assert result['status']==status
    assert result['estimated_saving']==saving


def test_grace_waives_unknown_prior_spend_but_still_requires_remaining_cap():
    context=PaymentContext(amount=30000,path='card',grace_status_groups={'kb_nori':'eligible'})
    result=estimate(rule(),context,'일반점','card:1',1)
    assert result['status']=='conditional'
    assert result['reasons']==['이번 달 잔여 한도 확인 필요']
