"""CODEF 연동은 내렸지만 이미 저장된 연결은 탈퇴 때 해제해야 한다."""
from unittest.mock import Mock

from app.api import auth
from app.core.config import settings
from app.models.card_connection import CardConnection
from app.models.user import User
from app.services.card_linking import seal
from app.services.codef import CodefError


def test_delete_account_revokes_stored_card_connection(auth_client, db, monkeypatch):
    provider = Mock()
    monkeypatch.setattr(auth, "codef_client", provider)
    user_id = auth_client.get("/auth/me").json()["id"]
    user = next(row for row in db.query(User).all() if str(row.id) == user_id)
    db.add(CardConnection(user_id=user.id, organization="0301", environment=settings.codef_service_type,
                          encrypted_connected_id=seal({"connectedId": "stored-id"}), login_type="1"))
    db.commit()

    provider.disconnect_account.side_effect = CodefError("network_error")
    assert auth_client.post("/auth/delete", json={"password": "test-password-1"}).status_code == 503
    assert auth_client.get("/auth/me").status_code == 200

    provider.disconnect_account.side_effect = None
    assert auth_client.post("/auth/delete", json={"password": "test-password-1"}).status_code == 204
    provider.disconnect_account.assert_called_with("0301", "stored-id", "1")
    assert db.query(CardConnection).count() == 0
