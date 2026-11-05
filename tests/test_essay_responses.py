"""No acknowledgements as facts, consistent edits, and explicit source/answer review."""
from copy import deepcopy
import uuid

import pytest

from app.core.config import settings
from app.models.writing_session import WritingSession
from app.services import llm
from app.services.essay_collection import detail_slots
from app.services.essay_responses import response_issue
from app.services.writing_requirements import requirements
from tests.test_essay_reference import essay_session
from tests.test_essay_collection import completed, values_for
from tests.test_submission_consent_reference import finish_collection, exported_texts
from tests.test_writing_workflow import action


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    monkeypatch.setattr(llm,'has_llm',lambda:False)
    monkeypatch.setattr(settings,'chat_rate_limit_per_min',1000)


@pytest.mark.parametrize('text', ['네','확인했습니다.','알겠습니다!','좋아요','감사합니다.',
                                  '아직 확인하지 않았어요.','미확인입니다.','질문 설명해주세요','장학금 추천해주세요','?'])
def test_chat_followup_and_edit_rejection_do_not_store_nonfact(auth_client,db,text):
    session,fields,_=essay_session(auth_client,db)
    field=fields[0]
    key=session['next_action']['slot_key']
    before=deepcopy(session['answers'])
    session=action(auth_client,session,'messages',{'message':text})
    assert session['next_action']['slot_key']==key and session['answers']==before
    assert field['id'] not in session['answers']
    assert len(session['messages'])>=3
    action(auth_client,session,f"fields/{field['id']}/{key}",{'message':text},method='put',expected=422)
    action(auth_client,session,f"fields/{field['id']}/confirm",{'confirmed':True},expected=409)
    action(auth_client,session,'draft',expected=409)
    loaded=auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded['answers']==before and loaded['version']==session['version']


@pytest.mark.parametrize('label,text', [('돌봄 빈도','매주 2회'),('돌봄 빈도','필요할 때'),
    ('이전 지원 기관','우양재단'),('학업에 미치는 영향','해당 없음'),('병원 방문 시 이동 방법','버스 이용')])
def test_short_original_facts_and_explicit_absence_are_allowed(label,text):
    from tests.test_essay_reference import fixture
    _,fields,_=fixture()
    field=next(f for f in fields if any(r.get('collection_label')==label for r in detail_slots(f)))
    rule=next(r for r in detail_slots(field) if r.get('collection_label')==label)
    assert response_issue(field,rule['key'],text) is None


def test_short_fact_is_accepted_through_chat_and_edit(auth_client,db):
    session,fields,_=essay_session(auth_client,db)
    field=fields[0]
    values=values_for(fields)
    frequency=next(r for r in detail_slots(field) if r.get('collection_label')=='돌봄 빈도')
    # Fill only predecessors through normal conversation, then reach the actual frequency question.
    while session['next_action']['slot_key']!=frequency['key']:
        step=session['next_action']
        assert step['kind']=='field'
        session=action(auth_client,session,'messages',{'message':values[(field['id'],step['slot_key'])]})
    session=action(auth_client,session,'messages',{'message':'매주 2회'})
    assert session['answers'][field['id']]['facts'][frequency['key']]['text']=='매주 2회'
    session=action(auth_client,session,f"fields/{field['id']}/{frequency['key']}",{'message':'필요할 때'},method='put')
    assert session['answers'][field['id']]['facts'][frequency['key']]['text']=='필요할 때'


def test_confirmation_lists_each_original_question_with_its_answer(auth_client,db):
    session,fields,_=essay_session(auth_client,db)
    field=fields[0]
    values=values_for(fields)
    while session['next_action']['kind']=='field':
        step=session['next_action']
        session=action(auth_client,session,'messages',{'message':values[(field['id'],step['slot_key'])]})
    assert session['next_action']['kind']=='confirm'
    question=session['next_action']['question']
    for rule in detail_slots(field):
        assert f"{rule.get('collection_label',rule['evidence'])}: {values[(field['id'],rule['key'])]}" in question
    assert '각 답변이 해당 질문을 설명' in question
    assert all(not fact['confirmed'] for fact in session['answers'][field['id']]['facts'].values())
    action(auth_client,session,'draft',expected=409)
    session=action(auth_client,session,f"fields/{field['id']}/confirm",{'confirmed':True})
    assert all(fact['confirmed'] for fact in session['answers'][field['id']]['facts'].values())


def test_rejected_edit_keeps_existing_confirmed_draft_and_version(auth_client,db):
    session,fields,_=completed(auth_client,db)
    field=fields[0]
    key=requirements(field)[0]['key']
    before=deepcopy(session)
    action(auth_client,session,f"fields/{field['id']}/{key}",{'message':'확인했습니다.'},method='put',expected=422)
    loaded=auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded['version']==before['version'] and loaded['answers']==before['answers']
    assert loaded['drafts']==before['drafts'] and loaded['draft_reviewed']
    exported_texts(auth_client,loaded)


@pytest.mark.parametrize('text',['확인했습니다.','아직 확인하지 않았습니다.','장학금 추천해주세요'])
def test_old_confirmed_nonfact_is_reasked_and_old_export_blocked(auth_client,db,text):
    session,fields,values=completed(auth_client,db)
    field=fields[0]
    key=requirements(field)[0]['key']
    stored=db.get(WritingSession,uuid.UUID(session['id']))
    old=deepcopy(stored.answers)
    old[field['id']]['facts'][key]['text']=text
    old[field['id']]['facts'][key]['confirmed']=True
    stored.answers=old
    db.commit()
    loaded=auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded['next_action']['slot_key']==key
    assert not loaded['drafts'] and not loaded['draft_reviewed']
    assert key not in loaded['answers'][field['id']]['facts']
    archived=loaded['answers'][field['id']]['previous_details'][key]
    assert archived['text']==text and not archived['confirmed'] and archived['response_issue']
    assert all(loaded['answers'][f['id']]==old[f['id']] for f in fields[1:])
    assert stored.answers==old
    for fmt in ('txt','docx'):
        assert auth_client.get(f"/writing-sessions/{session['id']}/export?format={fmt}").status_code==409
    action(auth_client,loaded,'review',{'confirmed':True,'manual_checks_confirmed':True},expected=409)
    action(auth_client,loaded,'save',{'allow_reuse':False},expected=409)
    session=finish_collection(auth_client,loaded,values)
    session=action(auth_client,session,'draft')
    session=action(auth_client,session,'review',{'confirmed':True,'manual_checks_confirmed':True})
    for output in exported_texts(auth_client,session):
        assert text not in output and values[(field['id'],key)] in output


def test_check_is_not_an_automatic_semantic_or_truth_verdict():
    field={'field_type':'essay'}
    # Arbitrary prose cannot be proved relevant by these deterministic checks.
    assert response_issue(field,'original_detail_example','오늘 날씨가 맑습니다.') is None
    assert response_issue({'field_type':'text'},'value','확인했습니다.') is None
