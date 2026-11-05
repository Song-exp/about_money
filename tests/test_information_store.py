from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.information_session import InformationSession
from app.services import chat
from tests.test_information_search import send


def test_restart_restores_dialogue_profile_and_search_without_reinterpreting(auth_client):
    first = send(auth_client, '서울 거주 소득 3분위 학점 3.8 생활비 장학금')
    send(auth_client, '서울 조건 빼고 찾아줘', first['session_id'])
    with chat._SESSIONS_LOCK:
        chat._SESSIONS.clear()
    restored = auth_client.get('/chat/session').json()
    assert restored['session_id'] == first['session_id']
    assert restored['profile']['region'] == '서울'
    assert restored['profile']['income_bracket'] == 3
    assert restored['search']['ignored_fields'] == ['region']
    assert len(restored['messages']) == 4
    continued = send(auth_client, '계속', restored['session_id'])
    assert continued['search']['ignored_fields'] == ['region']


def test_other_user_cannot_restore_or_continue_saved_dialogue(auth_client, client):
    first = send(auth_client, '소득 3분위 서울 거주')
    auth_client.post('/auth/logout')
    assert client.post('/auth/signup', json={'email': 'other-info@khu.ac.kr', 'password': 'other-pass-1', 'privacy_consent': True}).status_code == 201
    with chat._SESSIONS_LOCK:
        chat._SESSIONS.clear()
    assert client.get('/chat/session', params={'session_id': first['session_id']}).status_code == 404
    assert client.post('/chat/message', json={'session_id': first['session_id'], 'message': '계속'}).status_code == 404


def test_new_conversation_is_durable_but_does_not_erase_account_facts(auth_client):
    first = send(auth_client, '소득 3분위 학점 3.8 서울 거주')
    fresh = auth_client.post('/chat/session').json()
    assert fresh['session_id'] != first['session_id'] and fresh['messages'] == []
    assert fresh['profile']['gpa'] == 3.8
    assert auth_client.get('/chat/session').json()['session_id'] == fresh['session_id']


def test_expired_conversation_is_not_resumed(auth_client, db):
    first = send(auth_client, '서울 거주')
    row = db.get(InformationSession, first['session_id'])
    row.updated_at = datetime.now(timezone.utc) - timedelta(days=40)
    db.commit()
    assert auth_client.get('/chat/session').json() is None
    assert auth_client.get('/chat/session', params={'session_id': first['session_id']}).status_code == 409


def test_deleting_account_removes_saved_and_cached_conversations(auth_client, db):
    first = send(auth_client, '서울 거주')
    assert db.scalar(select(InformationSession)) is not None
    assert auth_client.post('/auth/delete', json={'password': 'test-password-1'}).status_code == 204
    db.expire_all()
    assert db.scalar(select(InformationSession)) is None
    assert first['session_id'] not in chat._SESSIONS
