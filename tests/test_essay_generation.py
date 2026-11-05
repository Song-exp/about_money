import json

from app.services import llm
from tests.test_writing_workflow import setup_writing, action, collect


def generator(monkeypatch, *, unsupported=False, number=False, rewrite_id=None):
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        if data.get('task') == 'writing_turn':
            return json.dumps({'kind': 'rewrite', 'reply': '', 'field_id': rewrite_id, 'slot_key': None, 'fact_quote': None})
        if data.get('task') == 'essay_grounding_check':
            return json.dumps({'supported': not unsupported, 'unsupported_sentences': ['새 경험'] if unsupported else [], 'missing_fact_keys': []})
        text = '학업 교재 마련에 필요한 비용을 지원받아 전공 수업에 집중하고자 장학금을 신청합니다.'
        if number:
            text += ' 전국 대회에서 9999등을 수상했습니다.'
        return json.dumps({'paragraphs': [{'text': text, 'fact_keys': list(data['confirmed_facts']),
            'quotes': [{'fact_key': key, 'quote': value} for key, value in data['confirmed_facts'].items()]}]}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)


def test_generation_writes_new_prose_with_mapped_confirmed_sources(auth_client, db, monkeypatch):
    _, session = setup_writing(auth_client, db)
    session = collect(auth_client, session)
    generator(monkeypatch)
    result = action(auth_client, session, 'draft')
    draft = result['drafts'][0]
    assert draft['method'] == 'llm_grounded_prose'
    assert draft['text'] == '학업 교재 마련에 필요한 비용을 지원받아 전공 수업에 집중하고자 장학금을 신청합니다.'
    assert set(draft['paragraph_evidence'][0]['fact_keys']) == {'reason', 'impact'}
    assert result['draft_reviewed'] is False


def test_unsupported_semantics_or_new_number_reject_generated_prose(auth_client, db, monkeypatch):
    _, session = setup_writing(auth_client, db)
    session = collect(auth_client, session)
    generator(monkeypatch, unsupported=True)
    rejected = action(auth_client, session, 'draft')
    assert rejected['drafts'][0]['method'] == 'confirmed_evidence_assembly'
    generator(monkeypatch, number=True)
    rejected = action(auth_client, rejected, 'draft')
    assert '9999' not in rejected['drafts'][0]['text']
    assert rejected['drafts'][0]['method'] == 'confirmed_evidence_assembly'


def test_rewrite_preserves_facts_and_invalidates_review(auth_client, db, monkeypatch):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, collect(auth_client, session), 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True})
    facts = session['answers']
    generator(monkeypatch, rewrite_id=session['fields'][0]['id'])
    session = action(auth_client, session, 'messages', {'message': '지원동기 초안을 더 간결하게 다듬어줘'})
    assert session['answers'] == facts
    assert session['drafts'][0]['method'] == 'llm_grounded_prose'
    assert not session['draft_reviewed']
    action(auth_client, session, 'save', expected=409)
