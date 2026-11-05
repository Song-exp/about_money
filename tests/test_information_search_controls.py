import json

from app.services import chat, llm
from tests.conftest import make_scholarship


def send(client, text, sid=None):
    response = client.post('/chat/message', json={'message': text, 'session_id': sid})
    assert response.status_code == 200, response.text
    return response.json()


def seed(db):
    rows = [make_scholarship(content_key='living-fit', title='지역 생활비 장학',
                eligibility={'region': ['전남'], 'income_bracket': [0, 1], 'gpa_min': 3.5},
                benefit={'type': 'living', 'amount_desc': '생활비 100만원'}),
            make_scholarship(content_key='wrong-income', title='다른 소득 장학',
                eligibility={'region': ['전남'], 'income_bracket': [9]}, benefit={'type': 'living'}),
            make_scholarship(content_key='tuition', title='등록금 장학', benefit={'type': 'tuition'})]
    db.add_all(rows)
    db.commit()
    return rows


def test_remove_only_region_requeries_and_keeps_income_gpa_type_and_account_facts(client, db):
    rows = seed(db)
    info = send(client, '서울 거주 소득 0분위 학점 3.8 생활비 장학금')
    assert not info['candidates']
    body = send(client, '서울 조건을 빼고 소득분위 0분위와 학점 3.8은 그대로 유지해서 찾아줘', info['session_id'])
    assert [c['id'] for c in body['candidates']] == [rows[0].id]
    assert body['candidates'][0]['title'] == rows[0].title
    assert body['candidates'][0]['benefit']['amount_desc'] == '생활비 100만원'
    assert body['candidates'][0]['source_url'] == rows[0].source_url
    assert body['candidates'][0]['eligibility']['region'] == ['전남']
    assert body['candidates'][0]['eligibility_status'] == 'not_eligible'
    assert body['profile'] == info['profile']
    session = chat._SESSIONS[info['session_id']]
    assert session.ignored_profile_fields == {'region'}
    restored = send(client, '서울 조건 다시 적용해줘', info['session_id'])
    assert not restored['candidates']
    assert session.ignored_profile_fields == set()


def test_field_relaxation_does_not_cross_into_preserved_neighbor_field(client, db):
    seed(db)
    info = send(client, '서울 거주 소득 0분위 학점 3.8 생활비 장학금')
    send(client, '학점은 유지하고 지역만 해제해줘', info['session_id'])
    assert chat._SESSIONS[info['session_id']].ignored_profile_fields == {'region'}
    send(client, '다시 내 조건 전부 적용해줘', info['session_id'])
    assert chat._SESSIONS[info['session_id']].ignored_profile_fields == set()
    send(client, '학점 빼지 말고 소득도 제외하지 마', info['session_id'])
    assert chat._SESSIONS[info['session_id']].ignored_profile_fields == set()


def test_semantic_search_plan_runs_before_generation_and_supplies_actual_candidate(client, db, monkeypatch):
    rows = seed(db)
    info = send(client, '서울 거주 소득 0분위 학점 3.8 생활비 장학금')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    observed = []
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        observed.append(data)
        if data.get('task') == 'search_plan':
            return json.dumps({'actions': [{'field': 'region', 'operation': 'ignore', 'evidence': '거주지 제약 없이'}], 'mode': 'personal'})
        assert [c['id'] for c in data['evidence']['candidates']] == [rows[0].id]
        assert data['evidence']['search']['effective_profile']['region'] is None
        assert data['evidence']['search']['effective_profile']['income_bracket'] == 0
        assert data['evidence']['search']['effective_profile']['gpa'] == 3.8
        assert data['evidence']['profile']['region'] == '서울'
        return json.dumps({'message': '지역 생활비 장학은 생활비 100만원 지원 공고예요. 실제 지역 자격은 별도로 확인해야 해요.', 'citations': [rows[0].id]}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    body = send(client, '거주지 제약 없이 찾아줘', info['session_id'])
    assert body['answer_source'] == 'llm'
    assert body['candidates'] and '100만원' in body['message']
    assert observed[0]['task'] == 'search_plan'
    assert observed[-1]['evidence']['catalog']['returned'] == 1


def test_ungrounded_plan_cannot_drop_unrequested_conditions(client, db, monkeypatch):
    seed(db)
    info = send(client, '서울 거주 소득 0분위 학점 3.8 생활비 장학금')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        if json.loads(prompt).get('task') == 'search_plan':
            return json.dumps({'actions': [{'field': 'income_bracket', 'operation': 'ignore', 'evidence': '소득 빼고'}], 'mode': None})
        return json.dumps({'message': '조건에 맞는 공고가 아직 없어요.', 'citations': []}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    body = send(client, '현재 조건 그대로 다시 확인해줘', info['session_id'])
    assert body['candidates'] == []
    assert chat._SESSIONS[info['session_id']].ignored_profile_fields == set()
