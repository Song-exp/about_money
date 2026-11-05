import json
from datetime import datetime, timedelta, timezone

from app.models.scholarship import Category, PostingStatus
from app.services import llm, chat
from app.services.policy_intake import period_status
from tests.conftest import make_scholarship, verified_policy_fixture


def policies(db):
    housing = make_scholarship(content_key='rent-intake', category=Category.GOV_BENEFIT,
        title='주거 지원', body_text='독립 거주 중인 무주택 청년의 전세 또는 월세 계약을 지원합니다.',
        eligibility={'gpa_min': 4.5})
    study = make_scholarship(content_key='grad-intake', category=Category.GOV_BENEFIT,
        title='대학원 진학 지원', body_text='대학원 진학 예정자를 대상으로 교육비를 지원합니다.')
    verified_policy_fixture(housing)
    verified_policy_fixture(study)
    db.add_all([housing, study, make_scholarship(content_key='not-policy', title='장학금 전용')])
    db.commit()
    return housing, study


def test_first_questions_use_actual_conditions_and_answers_survive_restore(auth_client, db, monkeypatch):
    housing, study = policies(db)
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    calls = []
    def complete(system, payload, **kwargs):
        data = json.loads(payload)
        calls.append(data)
        assert len(data['policies']) == 2
        if not data['user_message']:
            return json.dumps({'questions': [
                {'text': '독립 거주 중이신가요? 전세인가요, 월세인가요?', 'policy_id': housing.id,
                 'quote': housing.body_text},
                {'text': '대학원 진학 예정이신가요?', 'policy_id': study.id, 'quote': study.body_text}],
                'shortlist': []}, ensure_ascii=False)
        assert data['previous_questions'][0]['policy_id'] == housing.id
        assert data['user_message'] == '자취 중이고 월세예요. 대학원 진학 예정은 없어요.'
        return json.dumps({'questions': [], 'shortlist': [housing.id]})
    monkeypatch.setattr(llm, 'chat_complete', complete)
    initial = auth_client.post('/policy-chat/session').json()
    assert '대학원 진학 예정이신가요?' in initial['messages'][0]['text']
    assert '현재 접수 중임을 확인한 공고는 없습니다' in initial['messages'][0]['text']
    with chat._SESSIONS_LOCK:
        chat._SESSIONS.clear()
    result = auth_client.post('/policy-chat/message', json={'session_id': initial['session_id'],
        'message': '자취 중이고 월세예요. 대학원 진학 예정은 없어요.'}).json()
    assert result['answer_source'] == 'llm'
    assert [c['id'] for c in result['candidates']] == [housing.id]
    assert result['candidates'][0]['eligibility_status'] == 'unknown'
    assert result['selection'] is None
    assert len(calls) == 2


def test_question_without_real_quote_rejected(client, db, monkeypatch):
    housing, _ = policies(db)
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **kw: json.dumps({
        'questions': [{'text': '학점이 어떻게 되나요?', 'policy_id': housing.id, 'quote': '없는 조건'}],
        'shortlist': [housing.id]}))
    response = client.post('/policy-chat/message', json={'message': '주거 지원'}).json()
    assert response['answer_source'] == 'fallback'
    assert response['fallback_reason'] == 'intake_unavailable'
    assert '학점이 어떻게' not in response['message']


def test_period_dates_and_freshness_are_code_owned():
    now = datetime.now(timezone.utc)
    row = make_scholarship(crawled_at=now, eligibility={'application_period': {
        'kind': 'fixed', 'start_at': (now - timedelta(days=1)).isoformat(),
        'end_at': (now + timedelta(days=1)).isoformat()}})
    assert period_status(row, now) == 'current'
    row.crawled_at = now - timedelta(days=30)
    assert period_status(row, now) == 'unconfirmed'
    row.eligibility['application_period']['end_at'] = (now - timedelta(days=1)).isoformat()
    assert period_status(row, now) == 'closed'
    row.eligibility = {'application_period': {'kind': 'continuous'}}
    assert period_status(row, now) == 'unconfirmed'
    row.crawled_at = now
    assert period_status(row, now) == 'current'
    row.eligibility = {}
    assert period_status(row, now) == 'unconfirmed'


def test_expired_policy_excluded_before_model_and_profile_gpa_not_applied(client, db, monkeypatch):
    housing, study = policies(db)
    study.eligibility = {'application_period': {'kind': 'fixed',
        'end_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}}
    db.commit()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, payload, **kwargs):
        assert [p['id'] for p in json.loads(payload)['policies']] == [housing.id]
        return json.dumps({'questions': [], 'shortlist': [housing.id]})
    monkeypatch.setattr(llm, 'chat_complete', complete)
    result = client.post('/policy-chat/message', json={'message': '학점 2.0, 주거 지원'}).json()
    assert [c['id'] for c in result['candidates']] == [housing.id]
    assert result['profile']['gpa'] is None


def test_scholarship_empty_result_hides_internal_status_counts(client, db):
    db.add(make_scholarship(status=PostingStatus.NEEDS_REVIEW,
        deadline_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.commit()
    result = client.post('/chat/message', json={'message': '서울 장학금'}).json()
    assert '모집 중 장학금이 없어요' in result['message']
    assert 'DB' not in result['message'] and '검토 필요' not in result['message']
    assert result['answer_source'] == 'system'
