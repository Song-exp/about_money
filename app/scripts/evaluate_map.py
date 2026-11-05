"""Report synthetic calculation accuracy separately from real-world data evidence."""
import json
from pathlib import Path
from app.core.db import SessionLocal
from app.scripts.map_maintenance import audit
from app.services.map_verification import BenefitRules, PaymentContext, estimate


def main():
    reference = json.loads(Path('data/map_reference_cases.json').read_text(encoding='utf-8'))
    outcomes = []
    for case in reference['cases']:
        actual = estimate(BenefitRules(**case['rules']), PaymentContext(**case['context']), case.get('branch', ''), 'reference')
        checked = {key: actual[key] for key in case['expected']}
        outcomes.append({'id': case['id'], 'pass': checked == case['expected'], 'expected': case['expected'], 'actual': checked})
    with SessionLocal() as db: measured = audit(db)
    result = {'scope': reference['scope'], 'cases': outcomes,
              'passed': sum(r['pass'] for r in outcomes), 'total': len(outcomes),
              'human_reviewed_official_benefit_gold_count': 0,
              'real_benefit_accuracy': None, 'measured': measured}
    path = Path('reports/map-improvement-20261013/evaluation.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'total': result['total'], 'real_benefit_accuracy': None}))
    if result['passed'] != result['total']: raise SystemExit(1)


if __name__ == '__main__': main()
