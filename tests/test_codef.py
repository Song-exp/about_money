import base64
import json
from urllib.parse import quote, unquote

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import Settings
from app.services.codef import CodefClient, CodefError


def config(**overrides):
    return Settings(_env_file=None, codef_client_id='test-id', codef_client_secret='test-secret', **overrides)


def test_token_cached_and_credentials_sent_only_to_oauth():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'access_token': 'private-token', 'expires_in': 3600})
    client = CodefClient(config(), httpx.MockTransport(handler))
    assert client.access_token() == client.access_token() == 'private-token'
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://oauth.codef.io/oauth/token'
    assert b'grant_type=client_credentials' in calls[0].content


@pytest.mark.parametrize('encoded', [False, True])
@pytest.mark.parametrize('single', [False, True])
def test_card_output_never_exposes_number_and_handles_provider_formats(encoded, single):
    row = {'resCardNo': '1234567890123456', 'resCardName': '테스트+카드', 'resValidPeriod': '20290101'}
    def handler(request):
        if request.url.host == 'oauth.codef.io':
            return httpx.Response(200, json={'access_token': 'token', 'expires_in': 3600})
        assert request.url.host == 'development.codef.io'
        payload = json.loads(unquote(request.content.decode()))
        assert payload['connectedId'] == 'private-account'
        assert payload['inquiryType'] == '0'
        body = {'result': {'code': 'CF-00000'}, 'data': row if single else [row]}
        return httpx.Response(200, text=quote(json.dumps(body), safe='')) if encoded else httpx.Response(200, json=body)
    result = CodefClient(config(), httpx.MockTransport(handler)).card_list('0020', 'private-account')
    assert result[0]['name'] == '테스트+카드'
    assert '1234567890123456' not in json.dumps(result)
    assert '20290101' not in json.dumps(result)


def test_rsa_password_uses_codef_padding():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    client = CodefClient(config(codef_public_key=base64.b64encode(public).decode()))
    encrypted = client.encrypt_password('test-password')
    assert private.decrypt(base64.b64decode(encrypted), padding.PKCS1v15()) == b'test-password'


def test_provider_errors_sanitized_and_no_password_retry():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == 'oauth.codef.io':
            return httpx.Response(200, json={'access_token': 'token', 'expires_in': 3600})
        return httpx.Response(200, json={'result': {'code': 'CF-12834', 'message': 'SECRET_ACCOUNT'}, 'data': {}})
    client = CodefClient(config(), httpx.MockTransport(handler))
    with pytest.raises(CodefError, match='^CF-12834$'):
        client.card_list('0020', 'private-account')
    assert len(calls) == 2


def test_invalid_environment_does_not_send_credentials():
    def handler(request):
        pytest.fail('credentials must not leave on an invalid environment')
    client = CodefClient(config(codef_service_type='typo'), httpx.MockTransport(handler))
    with pytest.raises(CodefError, match='invalid_service_type'):
        client.access_token()


def test_rejected_keys_do_not_leak_body():
    client = CodefClient(config(), httpx.MockTransport(lambda request:
        httpx.Response(401, json={'error': 'test-secret'})))
    with pytest.raises(CodefError, match='^credentials_rejected$'):
        client.access_token()


def test_credentials_not_in_settings_repr():
    assert 'test-secret' not in repr(config())
    assert 'test-id' not in repr(config())


@pytest.mark.parametrize('existing', ['', 'existing-connection'])
def test_connect_account_encrypts_password_and_requires_success(existing):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    def handler(request):
        if request.url.host == 'oauth.codef.io':
            return httpx.Response(200, json={'access_token': 'token', 'expires_in': 3600})
        assert request.url.path == ('/v1/account/update' if existing else '/v1/account/create')
        body = json.loads(unquote(request.content.decode()))
        account = body['accountList'][0]
        assert account['businessType'] == 'CD' and account['clientType'] == 'P'
        assert account['id'] == 'user-login'
        assert private.decrypt(base64.b64decode(account['password']), padding.PKCS1v15()) == b'private-password'
        return httpx.Response(200, json={'result': {'code': 'CF-00000'}, 'data': {
            'connectedId': 'new-connection', 'successList': [{'organization': '0301', 'code': 'CF-00000'}], 'errorList': []}})
    client = CodefClient(config(codef_public_key=base64.b64encode(public).decode()), httpx.MockTransport(handler))
    assert client.connect_account('0301', '1', 'private-password', login_id='user-login', connected_id=existing) == 'new-connection'
