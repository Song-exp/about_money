from copy import deepcopy
from app.services.release_evaluation import evaluate_release, wilson


def case():
    field = {'label': '성명', 'role': 'student', 'field_type': 'text', 'required': None, 'min_length': None,
             'max_length': None, 'length_unit': None, 'includes_spaces': None, 'source': {'sha256': 'sha', 'location': 'cell:1'}}
    return {'id': '1', 'source_hashes': ['sha'], 'fetch_complete': True, 'prediction_complete': True,
            'prediction': {'fields': [field]}, 'gold': {'created_by': 'first-reviewer', 'reviewed_by': 'second-reviewer',
            'whole_document_reviewed': True, 'source_hashes': ['sha'], 'form_source_hashes': ['sha'], 'fields': [deepcopy(field)], 'facts': [], 'schedules': []}}


def test_unreviewed_and_self_reviewed_gold_cannot_release():
    c = case()
    c['gold']['reviewed_by'] = c['gold']['created_by']
    r = evaluate_release([c], 1, 1)
    assert not r['release_ready'] and r['metrics'] is None
    c['gold']['reviewed_by'] = 'second'
    c['source_hashes'] = ['different']
    assert not evaluate_release([c], 1, 1)['release_ready']


def test_missing_extra_role_constraint_and_failed_cases_are_counted():
    c = case()
    assert evaluate_release([c], 1, 1)['release_ready']
    assert not evaluate_release([c])['release_ready']
    c['prediction']['fields'][0]['role'] = 'guardian'
    r = evaluate_release([c], 1, 1)
    assert r['metrics']['field_recall']['correct'] == r['metrics']['whole_form_match']['correct'] == 0
    c = case()
    c['prediction']['fields'].append(deepcopy(c['prediction']['fields'][0]))
    assert evaluate_release([c], 1, 1)['metrics']['field_precision']['rate'] == .5
    c = case()
    c.update(fetch_complete=False, prediction_complete=False, prediction={'fields': []})
    r = evaluate_release([c], 1, 1)
    assert r['metrics']['field_recall']['total'] == 1 and r['metrics']['notice_retrieval']['total'] == 1
    assert r['metrics']['whole_form_match']['rate'] == 0


def test_duplicate_samples_and_small_sample_uncertainty():
    assert not evaluate_release([case(), case()], 1, 1)['release_ready']
    low, high = wilson(50, 50)
    assert low < .99 and high <= 1


def test_fact_schedule_or_component_mismatch_prevents_whole_match():
    c = case()
    c['gold']['facts'] = [{'key': 'gpa', 'value': 3.5}]
    c['gold']['schedules'] = [{'kind': 'student_deadline', 'value': '2026-10-12T18:00:00+09:00'}]
    r = evaluate_release([c], 1, 1)
    assert r['metrics']['facts_recall']['rate'] == 0 and r['metrics']['schedules_recall']['rate'] == 0
    assert not r['release_ready']
    c = case()
    c['prediction']['fields'][0]['input_spec'] = {'slots': [{'key': 'wrong'}]}
    assert evaluate_release([c], 1, 1)['metrics']['whole_form_match']['rate'] == 0
