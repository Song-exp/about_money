"""Search intent and empty-result explanations must reflect the real catalog."""
from datetime import datetime, timedelta, timezone

from app.models.scholarship import PostingStatus
from tests.conftest import make_scholarship


def send(client, text, sid=None):
    response = client.post('/chat/message', json={'message': text, 'session_id': sid})
    assert response.status_code == 200, response.text
    return response.json()


def test_region_mismatch_explains_without_repeating_missing_profile(client, db):
    db.add(make_scholarship(eligibility={'region': ['전남']}, benefit={'type': 'living'}))
    db.commit()
    body = send(client, '학점 3.6 제주 출신, 서울 거주, 소득분위 1분위')
    assert body['profile']['gpa'] == 3.6
    assert body['profile']['income_bracket'] == 1
    assert body['profile']['region'] == '서울'
    assert body['candidates'] == []
    assert '입력한 조건과 일치하는 공고는 없습니다' in body['message']
    assert '학년을 알려' not in body['message']
    assert '소득분위, 학점' not in body['message']


def test_browse_living_honors_count_and_does_not_erase_profile(client, db):
    for i in range(12):
        db.add(make_scholarship(content_key=f'living-{i}', title=f'생활비 장학 {i}',
            eligibility={'region': ['전남']}, benefit={'type': 'living'}))
    db.add(make_scholarship(content_key='tuition', title='등록금 장학', benefit={'type': 'tuition'}))
    db.commit()
    info = send(client, '학점 3.6 제주 출신, 서울 거주, 소득분위 1분위')
    body = send(client, '지금 모집중인 장학금 10개 가져와봐, 생활비성 장학만', info['session_id'])
    assert len(body['candidates']) == 10
    assert all(c['benefit']['type'] == 'living' for c in body['candidates'])
    assert all(c['eligibility_status'] == 'not_eligible' for c in body['candidates'])
    assert body['profile'] == info['profile']
    assert '개인 조건을 적용하지 않고' in body['message']
    personal = send(client, '내 조건에 맞는 생활비 장학금', info['session_id'])
    assert personal['candidates'] == []


def test_living_never_invents_unknown_or_tuition_benefits(client, db):
    for key, title, benefit in [('known', '생활비 지원', {}), ('unknown', '장학금', {}),
                                ('negative', '등록금 지원 (생활비 지원 제외)', {}),
                                ('tuition', '등록금 장학', {'type': 'tuition'})]:
        db.add(make_scholarship(content_key=key, title=title, benefit=benefit))
    db.commit()
    result = send(client, '생활비 장학금 10개 보여줘')
    assert [c['title'] for c in result['candidates']] == ['생활비 지원']
    assert '10건을 채우기 위해' in result['message']


def test_no_catalog_and_no_publishable_catalog_have_different_explanations(client, db):
    assert '등록된 장학금 공고가 없어요' in send(client, '장학금 찾아줘')['message']
    db.add(make_scholarship(status=PostingStatus.NEEDS_REVIEW))
    db.commit()
    result = send(client, '장학금 찾아줘')
    assert '모집 중 장학금이 없어요' in result['message']
    assert 'DB' not in result['message'] and '검토 필요' not in result['message']
    assert result['candidates'] == []


def test_living_filter_persists_until_cleared_and_never_includes_expired(client, db):
    db.add(make_scholarship(content_key='expired', title='생활비 마감', benefit={'type': 'living'},
        deadline_at=datetime.now(timezone.utc) - timedelta(days=2)))
    db.add(make_scholarship(content_key='tuition', title='등록금 장학', benefit={'type': 'tuition'}))
    db.commit()
    result = send(client, '생활비 장학금 10개 보여줘')
    assert result['candidates'] == []
    assert '지급 유형이 미확인' in result['message']
    assert send(client, '더 보여줘', result['session_id'])['candidates'] == []
    assert len(send(client, '모든 유형의 장학금 보여줘', result['session_id'])['candidates']) == 1


def test_browse_mismatched_candidate_cannot_enter_writing(auth_client, db):
    row = make_scholarship(eligibility={'region': ['전남']}, benefit={'type': 'living'})
    db.add(row)
    db.commit()
    info = send(auth_client, '서울 거주')
    body = send(auth_client, '전체 모집 공고 10개 보여줘', info['session_id'])
    assert len(body['candidates']) == 1
    assert auth_client.post('/writing-sessions', json={
        'information_session_id': info['session_id'], 'scholarship_id': row.id,
    }).status_code == 409
