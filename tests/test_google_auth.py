import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from wb2api.auth import google_callback
from wb2api.main import create_app
from wb2api.wabi import UpstreamError, Wabi


CALLBACK = 'https://app.getwabi.ai/gmail-auth-result?state=fixture-state&code=4%2Ffixture-code&scope=email'
PHONE = '+155****0100'


@pytest.mark.parametrize('value', [CALLBACK, CALLBACK.replace('&', '\\&'),
                                  CALLBACK.replace('&', '&amp;'), '<' + CALLBACK + '>',
                                  '[callback](' + CALLBACK.replace('&', '\\&') + ')',
                                  '?state=fixture-state&code=4%2Ffixture-code'])
def test_pasted_callback_formats(value):
    assert google_callback(value) == ('4/fixture-code', 'fixture-state')


@pytest.mark.parametrize('query', ['code=fixture&code=another', 'state=one&state=two&code=fixture',
                                  'state=fixture', 'code=', 'error=access_denied'])
def test_invalid_callback_does_not_echo_values(query):
    with pytest.raises(ValueError) as exc:
        google_callback('https://app.getwabi.ai/gmail-auth-result?' + query)
    assert query not in str(exc.value)


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'google-test-admin',
                      'api_key': 'google-test-client'})
    with TestClient(app) as client:
        app.state.logins[PHONE] = {'expires': time.time() + 600, 'google_state': 'fixture-state',
                                  'verifier': 'fixture-verifier', 'label': 'Fixture'}
        async def verify(state, code, verifier):
            assert (state, code, verifier) == ('fixture-state', '4/fixture-code', 'fixture-verifier')
            return {'firebase_custom_token': 'fixture-custom-token'}
        async def exchange(*args):
            return {'id_token': 'fixture-id', 'refresh_token': 'fixture-refresh',
                    'user_id': 'fixture-user', 'expires_at': time.time() + 3600}
        app.state.wabi.google_verify = verify
        app.state.wabi.exchange_custom = exchange
        yield client


def submit(client, **body):
    return client.post('/admin/api/auth/google-verify', headers={'Authorization': 'Bearer google-test-admin'},
                       json={'code': CALLBACK, **body})


def test_callback_alone_resolves_and_removes_session(client):
    response = submit(client)
    assert response.status_code == 200
    assert response.json()['account']['phone_number'] == PHONE
    assert not client.app.state.logins
    assert 'fixture-refresh' not in response.text


@pytest.mark.parametrize('body', [{'state': 'different'},
                                {'phone_number': PHONE, 'code': CALLBACK.replace('fixture-state', 'different')}])
def test_state_mismatch_rejected_before_upstream(client, body):
    async def forbidden(*args):
        raise AssertionError('Mismatched callbacks must not reach upstream')
    client.app.state.wabi.google_verify = forbidden
    response = submit(client, **body)
    assert response.status_code == 400
    assert PHONE in client.app.state.logins


def test_expired_session_rejected_before_upstream(client):
    client.app.state.logins[PHONE]['expires'] = time.time() - 1
    assert submit(client).status_code == 400


def test_raw_code_uses_pending_state(client):
    assert submit(client, code='4/fixture-code', phone_number=PHONE).status_code == 200


def test_manual_retry_after_firebase_failure_does_not_redeem_google_code_again(client):
    calls = []
    exchanges = []
    async def verify(*args):
        calls.append(args)
        return {'firebase_custom_token': 'fixture-custom-token'}
    async def exchange(token, key):
        exchanges.append(token)
        if len(exchanges) == 1:
            raise UpstreamError('Firebase connection failed', code='upstream_transport_error')
        return {'id_token': 'fixture-id', 'refresh_token': 'fixture-refresh',
                'expires_at': time.time() + 3600}
    client.app.state.wabi.google_verify = verify
    client.app.state.wabi.exchange_custom = exchange
    failed = submit(client)
    assert failed.status_code == 502
    assert 'fixture-custom-token' not in failed.text
    assert submit(client).status_code == 200
    assert len(calls) == 1 and len(exchanges) == 2
    assert not client.app.state.logins


@pytest.mark.asyncio
@pytest.mark.parametrize('error, code', [\
    ({'error_code': 'invalid_request'}, 'google_request_invalid'),
    ({'message': 'invalid_grant'}, 'google_authorization_expired'),
    ({'message': 'PKCE rejected'}, 'google_pkce_failed'),
    ({'message': 'Invalid state'}, 'google_state_invalid'),
    ({'message': 'other'}, 'google_verification_rejected'),
])
async def test_google_error_classification_and_single_post(error, code):
    calls = []
    def response(request):
        calls.append(request)
        assert json.loads(request.content) == {'state': 'fixture-state', 'code': 'fixture-code',
                                               'code_verifier': 'fixture-verifier'}
        return httpx.Response(400, json={**error, 'private': 'SECRET_DO_NOT_ECHO'})
    wabi = Wabi(None, {})
    await wabi.client.aclose()
    wabi.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    try:
        with pytest.raises(UpstreamError) as exc:
            await wabi.google_verify('fixture-state', 'fixture-code', 'fixture-verifier')
        assert exc.value.code == code
        assert 'SECRET_DO_NOT_ECHO' not in str(exc.value)
        assert len(calls) == 1
    finally:
        await wabi.close()
