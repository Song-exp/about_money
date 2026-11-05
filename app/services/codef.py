"""CODEF transport. Credentials and raw financial responses must not be logged.

Protocol reference: https://github.com/codef-io/codef-python
"""
from __future__ import annotations

import base64
import json
import re
import time
from threading import Lock
from urllib.parse import quote, unquote_plus

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import Settings, settings

HOSTS = {
    "sandbox": "https://sandbox.codef.io",
    "demo": "https://development.codef.io",
    "production": "https://api.codef.io",
}
TOKEN_URL = "https://oauth.codef.io/oauth/token"


class CodefError(Exception):
    """Only sanitized classifications escape the transport, never provider payloads."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CodefChallenge(CodefError):
    def __init__(self, info):
        super().__init__("CF-03002")
        self.info = info


class CodefClient:
    def __init__(self, config: Settings = settings, transport=None):
        self.config = config
        self.transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = Lock()

    @property
    def host(self):
        try:
            return HOSTS[self.config.codef_service_type]
        except KeyError:
            raise CodefError("invalid_service_type") from None

    def public_key(self):
        value = self.config.codef_public_key.strip().replace('\\n', '\n')
        try:
            key = (serialization.load_pem_public_key(value.encode()) if "-----BEGIN" in value
                   else serialization.load_der_public_key(base64.b64decode(value, validate=True)))
        except (ValueError, TypeError):
            raise CodefError("invalid_public_key") from None
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise CodefError("invalid_public_key")
        return key

    def encrypt_password(self, value: str):
        try:
            encrypted = self.public_key().encrypt(value.encode(), padding.PKCS1v15())
        except ValueError:
            raise CodefError("invalid_password_length") from None
        return base64.b64encode(encrypted).decode()

    def access_token(self):
        # A single client per process reuses the token until expiry.
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            self.host  # Reject an invalid environment before sending credentials.
            if not self.config.codef_client_id or not self.config.codef_client_secret:
                raise CodefError("credentials_missing")
            try:
                with httpx.Client(transport=self.transport, timeout=20, follow_redirects=False) as client:
                    response = client.post(TOKEN_URL,
                        auth=(self.config.codef_client_id, self.config.codef_client_secret),
                        data={"grant_type": "client_credentials", "scope": "read"})
            except httpx.HTTPError:
                raise CodefError("network_error") from None
            if response.status_code in (400, 401, 403):
                raise CodefError("credentials_rejected")
            if response.status_code != 200:
                raise CodefError("token_http_error")
            try:
                body = response.json()
                token = body.get("access_token")
                expires = int(body.get("expires_in", 0))
                if not isinstance(token, str) or not token or expires <= 0:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise CodefError("invalid_token_response") from None
            self._token = token
            self._expires_at = time.monotonic() + max(0, expires - 60)
            return self._token

    def request(self, path: str, payload: dict):
        if path not in ("/v1/account/create", "/v1/account/update", "/v1/account/delete", "/v1/kr/card/p/account/card-list"):
            raise CodefError("unsupported_api")
        token = self.access_token()
        try:
            with httpx.Client(transport=self.transport, timeout=httpx.Timeout(300, connect=15),
                              follow_redirects=False) as client:
                response = client.post(self.host + path,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                    content=quote(json.dumps(payload, ensure_ascii=False), safe="").encode())
        except httpx.HTTPError:
            # Do not retry: card passwords can lock an account after repeated failures.
            raise CodefError("network_error") from None
        if response.status_code == 401:
            with self._lock:
                self._token = ""
            raise CodefError("token_expired")
        if response.status_code != 200:
            raise CodefError("api_http_error")
        try:
            # Some environments send JSON directly, others URL-encoded JSON.
            try:
                body = response.json()
            except ValueError:
                body = json.loads(unquote_plus(response.text))
            code = body["result"]["code"]
            if not isinstance(code, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise CodefError("invalid_api_response") from None
        if code == "CF-03002" and isinstance(body.get("data"), dict) and body["data"].get("continue2Way") is True:
            raise CodefChallenge(body["data"])
        if code != "CF-00000":
            raise CodefError(code if re.fullmatch(r"CF-\d{5}", code) else "provider_error")
        return body.get("data")

    def connect_account(self, organization, login_type, password, *, login_id="", der_file="", key_file="", connected_id=""):
        account = {"countryCode": "KR", "businessType": "CD", "clientType": "P",
                   "organization": organization, "loginType": login_type,
                   "password": self.encrypt_password(password)}
        if login_type == "1":
            account["id"] = login_id
        else:
            account.update(derFile=der_file, keyFile=key_file)
        payload = {"accountList": [account]}
        if connected_id:
            payload["connectedId"] = connected_id
        data = self.request("/v1/account/update" if connected_id else "/v1/account/create", payload)
        if not isinstance(data, dict) or data.get("errorList"):
            raise CodefError("account_connection_failed")
        successes = data.get("successList", [])
        if not any(isinstance(item, dict) and item.get("organization") == organization
                   and item.get("code") == "CF-00000" for item in successes):
            raise CodefError("account_connection_failed")
        result = data.get("connectedId") or connected_id
        if not isinstance(result, str) or not result:
            raise CodefError("invalid_account_response")
        return result

    def disconnect_account(self, organization, connected_id, login_type):
        data = self.request("/v1/account/delete", {"connectedId": connected_id,
            "accountList": [{"countryCode": "KR", "businessType": "CD", "clientType": "P",
                "organization": organization, "loginType": login_type}]})
        if not isinstance(data, dict) or data.get("errorList"):
            raise CodefError("account_disconnection_failed")

    def card_list(self, organization: str, connected_id: str, *, card_no="", card_password="", birth_date="", two_way=None, sms_code=""):
        payload = {
            "organization": organization, "connectedId": connected_id, "inquiryType": "0",
            "cardNo": card_no, "cardPassword": self.encrypt_password(card_password) if card_password else "",
            "birthDate": birth_date,
        }
        if two_way:
            payload.update(is2Way="true", twoWayInfo=two_way, smsAuthNo=sms_code)
        data = self.request("/v1/kr/card/p/account/card-list", payload)
        rows = [data] if isinstance(data, dict) else data
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CodefError("invalid_card_response")
        # Never return card numbers, expiry dates, or raw account identifiers to the UI.
        return [{"name": row.get("resCardName", ""), "type": row.get("resCardType", ""),
                 "dormant": row.get("resSleepYN") == "Y", "state": row.get("resState", "")}
                for row in rows]


codef_client = CodefClient()
