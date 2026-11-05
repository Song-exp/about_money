"""Freeze original essay contracts before loading saved real model predictions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.hwp_source_inventory import source_inventory
from app.services.writing_drafts import compose, validate_groups
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact
from reports.atomic_input_reference import record_of
from reports.essay_reference import build_essay_gold, audit_essays
from reports.measure_single_inputs import original_bytes
from reports.measure_whole_form_inventory import read, save

BASE = Path(__file__).resolve().parent
ROOT = BASE / 'scholarship-improvement-20261010-essays-verified'
PREDICTIONS = BASE / 'scholarship-improvement-20261010-grid-audit-verified'


def measure_collection(fields, groups):
    answers, drafts, collected = {}, [], []
    for field in fields:
        assert requirements(field) and field['id'] not in answers
        index = 0
        while True:
            slots = requirements(field, answers.get(field['id']))
            slot = next((s for s in slots if s['key'] not in answers.get(field['id'], {}).get('facts', {})), None)
            if slot is None:
                break
            index += 1
            from app.services.essay_collection import detail_slots
            rule = next((r for r in detail_slots(field) if r['key'] == slot['key']), {})
            text = '지원 있음' if rule.get('response_options') else f"이번 신청 문항{record_of(field['source'])} 사실{index}: 본인이 확인한 실제 응답입니다."
            _set_fact(answers, field, slot['key'], text, {'kind': 'conversation'}, confirmed=True)
        draft = compose(field, answers[field['id']], SimpleNamespace(title='원문 자기소개서'))
        assert not draft['validation']['errors']
        assert all(fact['text'] in draft['text'] for fact in answers[field['id']]['facts'].values())
        assert {p['slot_key'] for p in draft['sources']} == {s['key'] for s in slots}
        drafts.append(draft)
        collected.append({'record': record_of(field['source']), 'slots': len(slots), 'all_supplied_facts_preserved': True})
    validation = validate_groups(groups, fields, drafts, {})
    assert len(validation) == 1 and not validation[0]['errors']
    assert set(validation[0]['warnings']) == {'original_layout_page_check_required', 'original_group_format_check_required'}
    return {'collected': collected, 'group_validation': validation,
            'fact_preservation_only_not_semantic_completeness': True, 'failures': 0}


def main():
    ROOT.mkdir(exist_ok=True)
    gold = build_essay_gold(source_inventory(original_bytes()))
    path = ROOT / 'gold.json'
    if path.exists():
        assert read(path) == json.loads(json.dumps(gold)), 'frozen original contract changed'
    else:
        save(path, gold)
    runs = []
    for index in range(1, 4):
        original_bytes(index)
        prediction = read(PREDICTIONS / f'r{index}-prediction.json')
        fields = [f for f in prediction['application_fields'] if record_of(f.get('source')) in (1070,1083,1111,1120)]
        audit = audit_essays(gold, fields, prediction['form_groups'])
        assert not audit['unresolved_contracts'], audit
        save(ROOT / f'r{index}-audit.json', audit)
        with patch('app.services.llm.has_llm', return_value=False):
            collection = measure_collection(fields, prediction['form_groups'])
        save(ROOT / f'r{index}-collection.json', collection)
        runs.append({'run': index, 'prompts': 4, 'explicit_original_clauses': 13,
                     'service_collection_slots': sum(c['slots'] for c in collection['collected']), 'contract_failures': 0})
    summary = {'gold_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'runs': runs,
               'measurement': 'saved_v28_predictions_not_new_model_runs', 'new_llm_calls': 0,
               'database_written': False, 'compound_response_completeness_verified': False,
               'complete_atomic_gold': False, 'whole_form_complete': False, 'independent_human_review_complete': False}
    save(ROOT / 'summary.json', summary)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
