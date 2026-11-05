import json

from app.services import llm
from tests.test_information_search import send


def test_hypothetical_question_never_overwrites_account(auth_client):
    first = send(auth_client, '부산 거주 소득 3분위 학점 3.8')
    question = send(auth_client, '서울 장학금도 있나요? 0분위 학생은 얼마나 받나요?', first['session_id'])
    assert question['profile'] == first['profile']
    user = auth_client.get('/auth/me').json()
    assert user['region'] == '부산' and user['income_bracket'] == 3


def test_unified_fallback_handles_negation_and_latest_count(client):
    from app.services import chat
    result = send(client, '생활비 말고 등록금 장학금 2개 말고 10개 보여줘')
    session = chat._SESSIONS[result['session_id']]
    assert session.benefit_type == 'tuition' and session.search_limit == 10


def test_llm_search_filter_is_not_a_personal_fact(auth_client, monkeypatch):
    first = send(auth_client, '부산 거주 소득 3분위 학점 3.8')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        payload = json.loads(prompt)
        if payload.get('task') == 'search_plan':
            return json.dumps({'intent': 'search', 'filters': [{'field': 'region', 'value': '서울', 'evidence': '서울 장학금'}],
                               'mode': 'personal', 'tool': {'name': 'search', 'ids': []}}, ensure_ascii=False)
        assert payload['evidence']['profile']['region'] == '부산'
        assert payload['evidence']['search']['effective_profile']['region'] == '서울'
        return json.dumps({'message': '서울 지역 공고를 검색했어요.', 'citations': []}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    result = send(auth_client, '서울 장학금도 찾아줘', first['session_id'])
    assert result['profile']['region'] == '부산'
    assert auth_client.get('/auth/me').json()['region'] == '부산'


def test_even_model_cannot_promote_quoted_question_to_fact(auth_client, monkeypatch):
    first = send(auth_client, '부산 거주 소득 3분위')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        if json.loads(prompt).get('task') == 'search_plan':
            return json.dumps({'profile_updates': [{'field': 'income_bracket', 'value': 0, 'evidence': '0분위면?'}]}, ensure_ascii=False)
        return json.dumps({'message': '가정한 조건에 대한 질문으로 이해했어요.', 'citations': []}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    result = send(auth_client, '0분위면?', first['session_id'])
    assert result['profile']['income_bracket'] == 3
    assert auth_client.get('/auth/me').json()['income_bracket'] == 3
