"""Measure real approved source records with explicit simulated purchase inputs."""
import json
from pathlib import Path
from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.card import Card, CardBenefit
from app.models.map_verification import MapEvidence
from app.services.map_verification import BenefitRules, PaymentContext, estimate, evidence_status


def main():
    reference=json.loads(Path('data/map_pengsoo_cases.json').read_text(encoding='utf-8'))
    cases=[]
    with SessionLocal() as db:
        items=db.scalars(select(CardBenefit).join(Card).where(Card.card_key=='kb_pengsoo_nori_check',CardBenefit.brand_key.is_not(None))).all()
        by_brand={item.brand_key:item for item in items if evidence_status(db.get(MapEvidence,('card',item.id)),item)=='approved'}
        for case in reference['cases']:
            item=by_brand[case['brand']]
            proof=db.get(MapEvidence,('card',item.id))
            context=PaymentContext(amount=case['amount'],path='card',prior_spend_groups={'kb_nori':case.get('spend',300000)},
                remaining_caps={'group:kb_nori':case.get('remaining',20000)},
                grace_status_groups={} if case.get('grace','not_eligible') is None else {'kb_nori':case.get('grace','not_eligible')},
                excluded_items_confirmed_absent=case.get('exclusions_confirmed',True),other_conditions_confirmed=True)
            actual=estimate(BenefitRules(**proof.rules),context,'조건 충족 가정 지점',f'card:{item.id}',item.card_id)
            passed=(actual['status'],actual['estimated_saving'])==(case['expected_status'],case['expected_saving'])
            cases.append({'id':case['id'],'pass':passed,'actual':actual,'source_hash':proof.content_hash,'source_url':proof.source_url})
    root=Path('reports/map-improvement-20261013-pengsoo')
    root.mkdir(parents=True,exist_ok=True)
    result={'scope':reference['scope'],'cases':cases,'passed':sum(c['pass'] for c in cases),'total':len(cases),
            'real_user_payment_verified':False,'real_financial_balance_verified':False}
    (root/'calculation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'total':result['total'],'real_user_payment_verified':False}))
    if result['passed']!=result['total']: raise SystemExit(1)


if __name__=='__main__': main()
