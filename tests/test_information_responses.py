import json

import pytest

from app.services import llm
from tests.conftest import make_scholarship


def send(client, message, sid=None):
    response = client.post('/chat/message', json={'message': message, 'session_id': sid})
    assert response.status_code == 200, response.text
    return response.json()


def mock_llm(monkeypatch, response, plan=None):
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    calls = []
    def complete(system, prompt, **kwargs):
        if json.loads(prompt).get('task') == 'search_plan':
            return json.dumps(plan or {'actions': [], 'mode': None}, ensure_ascii=False)
        calls.append({'system': system, 'payload': json.loads(prompt)})
        return json.dumps(response, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    return calls


def test_zero_matches_still_use_real_llm_reply(client, db, monkeypatch):
    db.add(make_scholarship(eligibility={'region': ['부산']}))
    db.commit()
    expected = '서울에 거주 중이시군요. 현재 수집된 공고는 다른 지역 자격이 필요해 맞춤 후보가 없어요. 지역 조건을 적용하지 않은 공고 목록을 함께 살펴볼까요?'
    calls = mock_llm(monkeypatch, {'message': expected, 'citations': []}, plan={
        'profile_updates': [{'field': key, 'value': value, 'evidence': quote} for key, value, quote in
                            [('income_bracket', 3, '소득분위 3분위'), ('gpa', 3.8, '학점 3.8'), ('region', '서울', '서울거주')]],
        'actions': [], 'mode': 'personal'})
    result = send(client, '소득분위 3분위, 학점 3.8, 서울거주')
    assert result['message'] == expected
    assert result['answer_source'] == 'llm'
    assert result['fallback_reason'] is None
    assert result['candidates'] == []
    evidence = calls[0]['payload']['evidence']
    assert evidence['catalog']['no_result_reason'] == 'profile_mismatch'
    assert evidence['catalog']['region_mismatch_count'] == 1
    assert evidence['profile']['gpa'] == 3.8
    assert 'ask_field' not in calls[0]['system']


def test_conversation_and_candidates_are_passed_and_response_not_replaced(client, db, monkeypatch):
    row = make_scholarship(title='실제 공고', benefit={'amount_desc': '생활비 지원'})
    db.add(row)
    db.commit()
    calls = mock_llm(monkeypatch, {'message': '생활비 지원 공고를 찾았어요. 상세 자격은 함께 확인해 봐요.', 'citations': [row.id]})
    first = send(client, '서울 거주, 소득 3분위, 학점 3.8')
    second = send(client, '아까 말한 공고는 어떤 지원이야?', first['session_id'])
    assert second['message'] == '생활비 지원 공고를 찾았어요. 상세 자격은 함께 확인해 봐요.'
    assert second['answer_source'] == 'llm'
    payload = calls[-1]['payload']
    assert payload['conversation'][-2]['content'] == '서울 거주, 소득 3분위, 학점 3.8'
    assert payload['conversation'][-1]['content'] == first['message']
    assert payload['user_message'] == '아까 말한 공고는 어떤 지원이야?'
    assert payload['evidence']['candidates'][0]['id'] == row.id


@pytest.mark.parametrize('response', [
    {'message': '없는 공고 안내', 'citations': [99999]},
    {'message': '장학금 9999만원을 받을 수 있어요.', 'citations': []},
    {'message': 'https://invented.invalid/scholarship', 'citations': []},
    {'message': '   ', 'citations': []},
    {'ask_field': 'gpa'},
])
def test_ungrounded_or_invalid_responses_are_identified_as_fallback(client, monkeypatch, response):
    mock_llm(monkeypatch, response)
    body = send(client, '장학금 찾아줘')
    assert body['answer_source'] == 'fallback'
    assert body['fallback_reason'] == 'invalid_response'
    assert '없는 공고 안내' not in body['message']
    assert '9999' not in body['message']
    assert 'invented.invalid' not in body['message']


def test_provider_failure_does_not_pretend_to_be_ai_response(client, monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def fail(*args, **kwargs):
        raise TimeoutError('upstream unavailable')
    monkeypatch.setattr(llm, 'chat_complete', fail)
    body = send(client, '장학금 찾아줘')
    assert body['answer_source'] == 'fallback'
    assert body['fallback_reason'] == 'provider_unavailable'
    assert body['message']


def test_no_key_returns_explicit_fallback(client):
    body = send(client, '장학금 찾아줘')
    assert body['answer_source'] == 'fallback'
    assert body['fallback_reason'] == 'not_configured'


def test_no_candidate_catalog_still_allows_natural_conversation(client, monkeypatch):
    expected = '안녕하세요. 장학금 조건이나 신청 준비에 대해 궁금한 점을 말씀해 주세요. 아직 수집된 공고가 없어 특정 장학금 추천은 어렵지만, 확인할 항목은 함께 정리할 수 있어요.'
    mock_llm(monkeypatch, {'message': expected, 'citations': []})
    result = send(client, '안녕하세요')
    assert result['message'] == expected and result['answer_source'] == 'llm'


def test_claim_is_checked_against_its_own_posting_field(client, db, monkeypatch):
    row = make_scholarship(benefit={'amount_desc': '생활비 100만원'})
    db.add(row)
    db.commit()
    mock_llm(monkeypatch, {'message': '생활비 100만원 공고입니다.', 'citations': [row.id],
                          'claims': [{'scholarship_id': row.id, 'field': 'benefit.amount_desc', 'value': '생활비 100만원'}]})
    good = send(client, '장학금 찾아줘')
    assert good['answer_source'] == 'llm' and good['citations'] == [row.id]
    assert good['tool_result']['tool'] == 'search'
    mock_llm(monkeypatch, {'message': '생활비 100만원 공고입니다.', 'citations': [row.id],
                          'claims': [{'scholarship_id': row.id, 'field': 'deadline', 'value': '생활비 100만원'}]})
    assert send(client, '장학금 찾아줘')['fallback_reason'] == 'invalid_response'
