"""Store card registration is additive, idempotent and account-scoped."""
from app.models.card import Card


def card(db, name):
    row = Card(card_key=name, card_name=name, issuer='테스트 발급사')
    db.add(row)
    db.commit()
    return row


def test_card_registration_requires_login(client, db):
    target = card(db, '인증 테스트 카드')
    assert client.post(f'/auth/cards/{target.id}').status_code == 401


def test_add_card_keeps_existing_cards_and_other_profile_fields(auth_client, db):
    first, second = card(db, '기존 카드'), card(db, '매장에서 찾은 카드')
    profile = {'card_ids': [first.id], 'gender': 'female', 'telecom': 'KT',
        'preferred_pay_methods': ['kakao'], 'student_credentials': ['student_card']}
    assert auth_client.put('/auth/profile', json=profile).status_code == 200
    response = auth_client.post(f'/auth/cards/{second.id}')
    assert response.status_code == 200
    data = response.json()
    assert data['card_ids'] == [first.id, second.id]
    for key, value in profile.items():
        if key != 'card_ids':
            assert data[key] == value
    assert auth_client.get('/auth/me').json()['card_ids'] == [first.id, second.id]
    assert auth_client.post(f'/auth/cards/{second.id}').json()['card_ids'] == [first.id, second.id]


def test_unknown_card_never_changes_account(auth_client):
    before = auth_client.get('/auth/me').json()['card_ids']
    assert auth_client.post('/auth/cards/999999').status_code == 404
    assert auth_client.get('/auth/me').json()['card_ids'] == before


def test_card_addition_does_not_modify_another_account(auth_client, client, db):
    from app.models.user import User
    from app.core.security import hash_password
    from sqlalchemy import select
    other = User(email='other-card-owner@khu.ac.kr', password_hash=hash_password('test-password-2'), card_ids=[])
    db.add(other)
    db.commit()
    target = card(db, '계정별 카드')
    assert auth_client.post(f'/auth/cards/{target.id}').status_code == 200
    db.expire_all()
    assert db.scalar(select(User).where(User.id == other.id)).card_ids == []
