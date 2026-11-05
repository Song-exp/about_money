"""Compound source details, explicit support branches, and legacy-session recovery."""
from copy import deepcopy
import uuid

import pytest

from app.core.config import settings
from app.models.writing_session import WritingSession
from app.services import llm
from app.services.essay_collection import detail_slots, normalize_details
from app.services.writing_requirements import requirements
from tests.test_essay_reference import fixture, essay_session
from tests.test_submission_consent_reference import finish_collection, exported_texts
from tests.test_writing_workflow import action


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)


def values_for(fields):
    return {(f['id'], r['key']): ('지원 있음' if r.get('response_options') else f"본인확인고유응답{i}_{j}입니다.")
            for i, f in enumerate(fields) for j, r in enumerate(detail_slots(f) or requirements(f))}


def completed(client, db):
    session, fields, _ = essay_session(client, db)
    values = values_for(fields)
    session = finish_collection(client, session, values)
    session = action(client, session, 'draft')
    session = action(client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    return session, fields, values


@pytest.mark.parametrize('label', ['돌봄 내용','돌봄 빈도','학업에 미치는 영향','생계에 미치는 영향',
                                 '병원 방문 시 이동 방법','병원 방문 시 동행 방법','이전 지원 내용','이전 지원 기관'])
def test_each_component_has_unique_exact_original_proof(label):
    _, fields, _ = fixture()
    rules = [r for f in fields for r in detail_slots(f)]
    child = next(r for r in rules if r.get('collection_label') == label)
    parent = next(r for f in fields for r in f.get('content_requirements', []) if r['key'] == child['parent_key'])
    assert child['key'] != parent['key']
    assert len({r['key'] for r in rules}) == len(rules)
    relative = child['source']['text_start'] - parent['source']['text_start']
    assert parent['evidence'][relative:relative+len(child['evidence'])] == child['evidence']
    assert child['source']['text_end'] - child['source']['text_start'] == len(child['evidence'])
    assert child['context_source'] == parent['context_source']
    assert child['context'] == parent['context']
    assert child['source']['sha256'] == parent['source']['sha256']


def test_alternatives_and_unrecognized_sentences_are_not_split():
    _, fields, _ = fixture()
    field = fields[0]
    treatment = next(r for r in field['content_requirements'] if '통원·입원' in r['evidence'])
    assert next(r for r in detail_slots(field) if r['key'] == treatment['key']) == treatment
    original = deepcopy(field['content_requirements'][0])
    original['evidence'] = '어떤 도움을 어디서 받았는지 아닌 다른 문맥'
    custom = {**field, 'content_requirements': [original]}
    assert detail_slots(custom) == [original]


@pytest.mark.parametrize('value', ['네','없습니다','해당 없음','기관에서 지원을 받았습니다.'])
def test_support_status_requires_explicit_branch(auth_client, db, value):
    session, fields, _ = essay_session(auth_client, db)
    field = fields[1]
    status = next(r for r in detail_slots(field) if r.get('response_options'))
    action(auth_client, session, f"fields/{field['id']}/{status['key']}", {'message': value}, method='put', expected=422)


def test_no_support_still_requires_current_need_then_reselection_resets_details(auth_client, db):
    session, fields, values = completed(auth_client, db)
    field = fields[1]
    status = next(r for r in detail_slots(field) if r.get('response_options'))
    children = [r for r in detail_slots(field) if r.get('depends_on')]
    current = next(r for r in detail_slots(field) if not r.get('response_options') and not r.get('depends_on'))
    before = deepcopy(session['answers'])
    old = session
    session = action(auth_client, session, f"fields/{field['id']}/{status['key']}", {'message': '지원 없음'}, method='put')
    facts = session['answers'][field['id']]['facts']
    assert all(r['key'] not in facts for r in children)
    assert facts[current['key']]['text'] == before[field['id']]['facts'][current['key']]['text']
    assert len(session['requirements'][field['id']]) == 2
    assert all(session['answers'][f['id']] == before[f['id']] for f in fields if f != field)
    for child in children:
        action(auth_client, session, f"fields/{field['id']}/{child['key']}", {'message': '비활성지원답변입니다.'}, method='put', expected=422)
    assert not session['drafts'] and not session['draft_reviewed']
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    action(auth_client, old, f"fields/{field['id']}/confirm", {'confirmed': True}, expected=409)
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    for text in exported_texts(auth_client, session):
        assert '지원 없음' in text and values[(field['id'],current['key'])] in text
        assert all(values[(field['id'],r['key'])] not in text for r in children)
    session = action(auth_client, session, f"fields/{field['id']}/{status['key']}", {'message': '지원 있음'}, method='put')
    assert session['next_action']['slot_key'] == children[0]['key']
    action(auth_client, session, f"fields/{field['id']}/confirm", {'confirmed': True}, expected=409)
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    for text in exported_texts(auth_client, session):
        assert all(values[(field['id'],r['key'])] in text for r in children)
        assert '지원 없음' not in text


def test_old_compound_answers_are_archived_and_old_drafts_blocked(auth_client, db):
    session, fields, values = completed(auth_client, db)
    stored = db.get(WritingSession, uuid.UUID(session['id']))
    answers = deepcopy(stored.answers)
    for field in fields[:2]:
        rules = detail_slots(field)
        for rule in rules:
            if rule.get('parent_key'):
                answers[field['id']]['facts'].pop(rule['key'], None)
                answers[field['id']]['facts'][rule['parent_key']] = {'text':'예전복합답변만있습니다.', 'confirmed':True, 'source':{'kind':'conversation'}}
            if rule.get('response_options'):
                answers[field['id']]['facts'][rule['key']]['text'] = '과거 기관의 지원을 받았어요.'
    stored.answers = answers
    db.commit()
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert not loaded['drafts'] and not loaded['draft_reviewed'] and loaded['next_action']['kind'] == 'field'
    for field in fields[:2]:
        item = loaded['answers'][field['id']]
        assert item['previous_details'] and all(not fact['confirmed'] for fact in item['previous_details'].values())
        assert all(rule['key'] not in item['facts'] for rule in detail_slots(field) if rule.get('parent_key'))
    # GET does not rewrite stored data. Old confirmed paragraphs never become child facts.
    assert stored.answers == answers
    for suffix in ('export?format=txt','export?format=docx'):
        assert auth_client.get(f"/writing-sessions/{session['id']}/{suffix}").status_code == 409
    action(auth_client, loaded, 'draft', expected=409)
    action(auth_client, loaded, 'review', {'confirmed':True, 'manual_checks_confirmed':True}, expected=409)
    action(auth_client, loaded, 'save', {'allow_reuse':False}, expected=409)
    session = finish_collection(auth_client, loaded, values)
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed':True,'manual_checks_confirmed':True})
    for text in exported_texts(auth_client, session):
        assert '예전복합답변만있습니다.' not in text and '과거 기관의 지원을 받았어요.' not in text
    reloaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert reloaded['answers'] == session['answers'] and reloaded['draft_reviewed']


def test_legacy_normalization_is_repeatable_without_mutating_field():
    _, fields, _ = fixture()
    field = fields[0]
    before = deepcopy(field)
    child = next(r for r in detail_slots(field) if r.get('parent_key'))
    item = {'facts':{child['parent_key']:{'text':'이전 답변', 'confirmed':True}}}
    normalize_details(field, item)
    first = deepcopy(item)
    normalize_details(field, item)
    assert item == first and field == before and not item['facts']
