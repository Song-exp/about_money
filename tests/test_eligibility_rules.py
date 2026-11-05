from copy import deepcopy
from types import SimpleNamespace
import pytest

from app.schemas.schemas import UserProfile
from app.services.eligibility_rules import review_tree, evaluate_rules
from app.services.matching import _passes_hard_filter
from app.services.writing_requirements import next_action

TEXT = '유형 A: 학점 3.5 이상 또는 유형 B: 학점 3.0 이상. 기존 수혜자 제외'
PROOF = {'sha256': 'sha', 'evidence': TEXT, 'text_start': 0, 'text_end': len(TEXT)}


def atom(key, attribute, value, operator='eq'):
    return {'kind': 'atom', 'key': key, 'attribute': attribute, 'operator': operator, 'value': value,
            'question': key + ' 조건을 확인하세요.', 'source': PROOF}


def scholarship():
    tree = {'kind': 'all', 'source': PROOF, 'children': [
        {'kind': 'any', 'source': PROOF, 'children': [
            {'kind': 'all', 'source': PROOF, 'children': [atom('type_a', 'reported', True), atom('a_gpa', 'gpa', 3.5, 'ge')]},
            {'kind': 'all', 'source': PROOF, 'children': [atom('type_b', 'reported', True), atom('b_gpa', 'gpa', 3.0, 'ge')]}]},
        {'kind': 'not', 'source': PROOF, 'child': atom('previous_benefit', 'reported', True)}]}
    rules = review_tree(tree, [{'sha256': 'sha', 'extracted_text': TEXT}], 'hash', True)
    return SimpleNamespace(content_hash='hash', eligibility={'condition_rules': rules, 'gpa_min': 3.5})


def test_type_scoped_or_and_exclusion_unknown_and_correction():
    row = scholarship()
    profile = UserProfile(gpa=3.2)
    assert evaluate_rules(row, profile)['status'] == 'unknown'
    assert _passes_hard_filter(row, profile)[0]  # the unrelated type A 3.5 must not filter type B
    facts = {'type_a': False, 'type_b': True, 'previous_benefit': False}
    assert evaluate_rules(row, profile, facts)['status'] == 'satisfied'
    facts['previous_benefit'] = True
    failed = evaluate_rules(row, profile, facts)
    assert failed['status'] == 'not_eligible' and failed['pending']
    assert failed['pending'][0]['key'] == 'previous_benefit'
    facts['previous_benefit'] = False
    assert evaluate_rules(row, UserProfile(gpa=2.9), facts)['status'] == 'not_eligible'
    assert evaluate_rules(row, UserProfile(), facts)['status'] == 'unknown'
    row.content_hash = 'changed'
    assert evaluate_rules(row, profile, facts)['status'] == 'unknown'


@pytest.mark.parametrize('problem', ['unreviewed', 'wrong_hash', 'wrong_quote', 'wrong_position', 'unknown_operator', 'duplicate_key', 'missing_scope', 'depth'])
def test_unverified_or_malformed_tree_rejected(problem):
    row = scholarship()
    tree = deepcopy(row.eligibility['condition_rules']['tree'])
    docs = [{'sha256': 'sha', 'extracted_text': TEXT}]
    reviewed = True
    if problem == 'unreviewed':
        reviewed = False
    elif problem == 'wrong_hash':
        docs[0]['sha256'] = 'wrong'
    elif problem == 'wrong_quote':
        tree['source'] = {**PROOF, 'evidence': '의역'}
    elif problem == 'wrong_position':
        tree['source'] = {**PROOF, 'text_start': 1}
    elif problem == 'unknown_operator':
        tree['children'][1]['child']['operator'] = 'python'
    elif problem == 'duplicate_key':
        tree['children'][1]['child']['key'] = 'type_a'
    elif problem == 'missing_scope':
        tree['children'][0].pop('source')
    else:
        for _ in range(10):
            tree = {'kind': 'not', 'source': PROOF, 'child': tree}
    with pytest.raises(ValueError):
        review_tree(tree, docs, 'hash', reviewed)


def test_writing_asks_specific_rule_before_form_and_respects_or():
    row = scholarship()
    session = SimpleNamespace(profile=UserProfile(gpa=3.2).model_dump(), fields=[])
    assert next_action(session, row, {}, {})['key'] == 'rule:type_b'
    workflow = {'qualification_answers': {'type_a': False, 'type_b': True, 'previous_benefit': False}}
    assert next_action(session, row, {}, workflow) is None


def test_api_review_source_validation_specific_questions_and_staleness(auth_client, db):
    from tests.test_writing_workflow import setup_writing, action
    from app.models.user import User
    from sqlalchemy import select
    from app.services.writing_chat import start_session
    row, old = setup_writing(auth_client, db)
    from app.models.source_document import SourceFetchAttempt
    attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == row.content_key).order_by(SourceFetchAttempt.id.desc()))
    attempt.documents = [{**attempt.documents[0], 'extracted_text': TEXT, 'sha256': 'sha'}]
    db.commit()
    tree = scholarship().eligibility['condition_rules']['tree']
    payload = {'attempt_id': attempt.id, 'content_hash': row.content_hash, 'tree': tree, 'reviewed_logic': True}
    path = f'/admin/crawl/scholarships/{row.id}/condition-rules'
    assert auth_client.post(path, json=payload).status_code in (401, 403)
    headers = {'X-Admin-Token': 'test-admin-token'}
    assert auth_client.post(path, json={**payload, 'reviewed_logic': False}, headers=headers).status_code == 422
    assert auth_client.post(path, json=payload, headers=headers).status_code == 200
    assert auth_client.get(f"/writing-sessions/{old['id']}").json()['state'] == 'stale'
    db.refresh(row)
    session = start_session(db, db.scalar(select(User)).id, row, UserProfile(gpa=3.2))
    assert session['next_action']['key'] == 'rule:type_b'
    session = action(auth_client, session, 'messages', {'message': '예'})
    assert session['next_action']['key'] == 'rule:previous_benefit'
    session = action(auth_client, session, 'messages', {'message': '예'})
    assert session['state'] == 'ineligible'
    action(auth_client, session, 'draft', expected=409)
    assert session['next_action']['key'] == 'rule:previous_benefit'
    session = action(auth_client, session, 'messages', {'message': '아니요'})
    assert session['next_action']['kind'] == 'field'
