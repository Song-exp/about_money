import json

from app.models.scholarship import Category
from app.services import chat, llm
from tests.conftest import make_scholarship, verified_policy_fixture


def post(client, scope, text, sid=None):
    url = '/policy-chat/message' if scope == 'youth_policy' else '/chat/message'
    response = client.post(url, json={'message': text, 'session_id': sid})
    assert response.status_code == 200, response.text
    return response.json()


def seed(db):
    rows = [make_scholarship(content_key='sch', title='재단 장학금'),
            make_scholarship(content_key='policy', title='주거 지원', category=Category.GOV_BENEFIT, source_platform='onjungchoungnyeon'),
            make_scholarship(content_key='legacy', title='광양시 생활비 장학금', category=Category.SCHOLARSHIP, source_platform='onjungchoungnyeon')]
    for row in rows[1:]:
        verified_policy_fixture(row)
    db.add_all(rows)
    db.commit()
    return rows


def test_scholarship_and_policy_api_query_disjoint_sets_including_legacy_rows(client, db):
    rows = seed(db)
    scholarship = post(client, 'scholarship', '장학금 찾아줘')
    policy = post(client, 'youth_policy', '청년정책 찾아줘')
    assert [c['id'] for c in scholarship['candidates']] == [rows[0].id]
    assert {c['id'] for c in policy['candidates']} == {rows[1].id, rows[2].id}
    assert policy['selection'] is None
    assert all(c['application_period']['kind'] == 'unknown' for c in policy['candidates'])
    assert '학년을 알려' not in policy['message'] and '작성 페이지' not in policy['message']


def test_conversation_ids_cannot_be_reused_across_scopes(auth_client, db):
    seed(db)
    scholarship = post(auth_client, 'scholarship', '장학금 찾아줘')
    assert auth_client.post('/policy-chat/message', json={'session_id': scholarship['session_id'], 'message': '계속'}).status_code == 409
    policy = post(auth_client, 'youth_policy', '정책 찾아줘')
    assert auth_client.post('/chat/message', json={'session_id': policy['session_id'], 'message': '계속'}).status_code == 409


def test_latest_conversation_and_new_conversation_are_scoped(auth_client, db):
    seed(db)
    sch = post(auth_client, 'scholarship', '장학금 찾아줘')
    pol = post(auth_client, 'youth_policy', '청년정책 찾아줘')
    with chat._SESSIONS_LOCK:
        chat._SESSIONS.clear()
    assert auth_client.get('/chat/session').json()['session_id'] == sch['session_id']
    assert auth_client.get('/policy-chat/session').json()['session_id'] == pol['session_id']
    auth_client.post('/policy-chat/session')
    initial = auth_client.get('/policy-chat/session').json()['messages']
    assert len(initial) == 1 and initial[0]['role'] == 'bot'
    assert auth_client.get('/chat/session').json()['session_id'] == sch['session_id']


def test_policy_cannot_enter_scholarship_writing_even_if_client_calls_api(auth_client, db):
    rows = seed(db)
    pol = post(auth_client, 'youth_policy', '정책 찾아줘')
    result = auth_client.post('/writing-sessions', json={'information_session_id': pol['session_id'], 'scholarship_id': rows[1].id})
    assert result.status_code == 409


def test_policy_selection_is_detail_not_writing(auth_client, db, monkeypatch):
    rows = seed(db)
    pol = post(auth_client, 'youth_policy', '정책 찾아줘')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        assert data['task'] == 'policy_intake'
        assert '자기소개서 작성 페이지로 유도하지 않는다' in system
        return json.dumps({'questions': [], 'shortlist': [rows[1].id]})
    monkeypatch.setattr(llm, 'chat_complete', complete)
    result = post(auth_client, 'youth_policy', '이 정책 선택할게', pol['session_id'])
    assert result['selection'] is None and result['answer_source'] == 'llm'


def test_normalizer_preserves_ids_and_only_changes_youth_api_records(db):
    from app.scripts.separate_consultation_sources import run
    rows = seed(db)
    result = run(apply=True)
    assert result['policy_record_ids'] == [rows[2].id]
    db.expire_all()
    assert db.get(type(rows[0]), rows[0].id).category == Category.SCHOLARSHIP
    assert db.get(type(rows[2]), rows[2].id).category == Category.GOV_BENEFIT


def test_next_collection_repairs_legacy_category_and_preserves_application_period(db):
    from app.crawlers.public import OnjungchoungnyeonCrawler
    from app.crawlers.registry import upsert_posting
    from tests.test_youthcenter import _item
    from sqlalchemy import select
    from app.models.scholarship import Scholarship
    raw = OnjungchoungnyeonCrawler()._to_raw(_item(plcyNm='정책형 장학금'))
    upsert_posting(db, raw)
    db.commit()
    row = db.scalar(select(Scholarship))
    original_id = row.id
    row.category = Category.SCHOLARSHIP
    db.commit()
    upsert_posting(db, raw)
    db.commit()
    db.refresh(row)
    assert row.id == original_id and row.category == Category.GOV_BENEFIT
    assert row.eligibility['application_period']['kind'] == 'fixed'


def test_policy_search_words_do_not_require_adjacent_phrase(db):
    from app.schemas.schemas import UserProfile
    from app.services.matching import match_scholarships, matches_query
    row = make_scholarship(content_key='housing-query', title='청년 주거비 지원 사업',
                           category=Category.GOV_BENEFIT, source_platform='onjungchoungnyeon')
    verified_policy_fixture(row)
    db.add(row)
    db.commit()
    found = match_scholarships(db, UserProfile(), query='주거 지원', consultation='youth_policy')
    assert [r.scholarship.id for r in found] == [row.id]
    assert matches_query(row, '주거 지원')


def test_policy_does_not_become_writable_even_if_its_form_was_reviewed(auth_client, db):
    from tests.test_scholarship_validation import stored_form
    from tests.test_writing_flow import approve
    row, attempt = stored_form(db)
    approve(auth_client, row, attempt)
    db.refresh(row)
    assert row.readiness['writing_ready']
    row.source_platform = 'onjungchoungnyeon'
    db.commit()
    from app.services.writing_chat import start_session
    from app.schemas.schemas import UserProfile
    from fastapi import HTTPException
    from app.models.user import User
    from sqlalchemy import select
    import pytest
    with pytest.raises(HTTPException) as error:
        start_session(db, db.scalar(select(User)).id, row, UserProfile())
    assert error.value.status_code == 409
