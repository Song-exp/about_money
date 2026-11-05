import json

from app.services import llm
from tests.test_writing_workflow import setup_writing, action, collect


def test_explanation_is_not_saved_as_a_field_or_qualification(auth_client, db):
    _, session = setup_writing(auth_client, db)
    before = session['answers']
    session = action(auth_client, session, 'messages', {'message': '이 자격은 무슨 뜻인가요?'})
    assert session['answers'] == before
    assert session['next_action']['kind'] == 'qualification'
    session = action(auth_client, session, 'messages', {'message': '네, 공고 자격을 확인했고 모두 해당합니다.'})
    session = action(auth_client, session, 'messages', {'message': '지원동기가 무슨 뜻인지 설명해 주세요?'})
    assert session['answers'] == {}
    assert session['next_action']['slot_key'] == 'reason'


def test_model_explains_without_advancing_required_state(auth_client, db, monkeypatch):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, session, 'messages', {'message': '네, 공고 자격을 확인했고 모두 해당합니다.'})
    explanation = '이 문항은 장학금이 왜 필요한지 본인의 상황으로 설명하는 항목이에요. 실제 학업 목표와 필요한 지원을 말씀해 주세요.'
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *args, **kwargs: json.dumps({
        'kind': 'question', 'reply': explanation, 'field_id': None, 'slot_key': None, 'fact_quote': None,
    }, ensure_ascii=False))
    session = action(auth_client, session, 'messages', {'message': '지원동기를 어떻게 써야 하나요?'})
    assert session['messages'][-1]['text'] == explanation
    assert session['answers'] == {} and session['next_action']['slot_key'] == 'reason'


def test_natural_correction_invalidates_confirmation_and_drafts(auth_client, db, monkeypatch):
    _, session = setup_writing(auth_client, db)
    session = collect(auth_client, session)
    session = action(auth_client, session, 'draft')
    field_id = session['fields'][0]['id']
    replacement = '이번 학기 전공 교재와 실습 재료 구입 비용이 필요합니다.'
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *args, **kwargs: json.dumps({
        'kind': 'correction', 'reply': '', 'field_id': field_id, 'slot_key': 'reason', 'fact_quote': replacement,
    }, ensure_ascii=False))
    session = action(auth_client, session, 'messages', {'message': '지원동기 내용을 정정할게요. ' + replacement})
    assert session['answers'][field_id]['facts']['reason']['text'] == replacement
    assert session['drafts'] == [] and not session['draft_reviewed']


def test_question_after_draft_preserves_draft(auth_client, db):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, collect(auth_client, session), 'draft')
    drafts = session['drafts']
    session = action(auth_client, session, 'messages', {'message': '이 초안을 어떻게 검토하나요?'})
    assert session['drafts'] == drafts
