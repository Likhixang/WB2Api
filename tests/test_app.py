import json
import time

import pytest
from fastapi.testclient import TestClient

from wb2api.main import create_app
from wb2api.store import Store

ADMIN = 'test-management-key-1234567890'
API = 'test-inference-key-1234567890'
AH = {'Authorization': f'Bearer {ADMIN}'}
IH = {'Authorization': f'Bearer {API}'}


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': ADMIN, 'api_key': API})
    with TestClient(app) as client:
        yield client


def test_health_static_and_auth_boundaries(client):
    assert client.get('/healthz').json()['status'] == 'ok'
    page = client.get('/')
    assert page.status_code == 200 and 'WB2Api' in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert client.get('/admin/api/status').status_code == 401
    assert client.get('/admin/api/status', headers=IH).status_code == 401
    assert client.get('/v1/models', headers=AH).status_code == 401
    assert client.get('/v1/models', headers=IH).json()['data'][0]['id'] == 'wabi'


def test_account_encryption_and_no_secret_echo(client):
    secret = 'not-a-real-refresh-token-for-ci'
    response = client.post('/admin/api/accounts', headers=AH, json={
        'label': 'CI fixture', 'refresh_token': secret, 'firebase_api_key': 'test-project-identifier',
    })
    assert response.status_code == 200
    account = response.json()
    assert account['ready'] and account['has_refresh_token']
    assert secret not in response.text
    data_dir = client.app.state.store
    assert secret not in data_dir.db.execute('SELECT refresh_token FROM accounts').fetchone()[0]
    assert data_dir.get_account(account['id'])['refresh_token'] == secret
    toggled = client.patch('/admin/api/accounts/' + account['id'], headers=AH, json={'enabled': False})
    assert not toggled.json()['enabled']
    reauth = client.put('/admin/api/accounts/' + account['id'] + '/reauth', headers=AH, json={
        'label': 'CI fixture', 'refresh_token': 'new-secret', 'firebase_api_key': 'test-project-identifier'
    })
    assert reauth.status_code == 200
    assert data_dir.get_account(account['id'])['refresh_token'] == 'new-secret'
    deleted = client.delete('/admin/api/accounts/' + account['id'], headers=AH)
    assert deleted.status_code == 200
    assert secret not in client.get('/admin/api/status', headers=AH).text


def test_validation_never_echoes_tokens(client):
    marker = 'THIS_SECRET_MUST_NOT_LEAK'
    response = client.post('/admin/api/accounts', headers=AH, json={'label': 'test', 'unexpected': marker})
    assert response.status_code == 422 and marker not in response.text


def test_fail_closed_without_protocol(client):
    response = client.post('/v1/chat/completions', headers=IH,
                           json={'messages': [{'role': 'user', 'content': 'test'}]})
    assert response.status_code == 501
    assert response.json()['error']['code'] == 'protocol_not_configured'


def test_protocol_validation_and_no_usable_account(client):
    mapping = {'delta_events': ['fixture_delta'], 'done_events': ['fixture_done']}
    assert client.put('/admin/api/protocol', headers=AH, json=mapping).status_code == 200
    assert client.get('/admin/api/status', headers=AH).json()['protocol_configured']
    response = client.post('/v1/chat/completions', headers=IH,
                           json={'messages': [{'role': 'user', 'content': 'test'}]})
    assert response.status_code == 503
    assert client.put('/admin/api/protocol', headers=AH,
                      json={'delta_events': ['overlap'], 'done_events': ['overlap']}).status_code == 422


@pytest.mark.parametrize('stream', [False, True])
def test_openai_output_conversion_with_explicit_fixture(client, stream):
    # This fixture tests conversion only; it is not a captured Wabi response.
    client.post('/admin/api/accounts', headers=AH, json={
        'label': 'Fixture account', 'refresh_token': 'fixture', 'firebase_api_key': 'fixture',
    })
    client.put('/admin/api/protocol', headers=AH,
               json={'delta_events': ['fixture_delta'], 'done_events': ['fixture_done']})

    async def fixture_stream(account_id, prompt, protocol):
        assert prompt == 'hello'
        yield 'Hello '
        yield 'world'

    client.app.state.wabi.stream_chat = fixture_stream
    response = client.post('/v1/chat/completions', headers=IH, json={
        'model': 'wabi', 'stream': stream, 'messages': [{'role': 'user', 'content': 'hello'}],
    })
    assert response.status_code == 200
    if stream:
        frames = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
        assert frames[-1] == '[DONE]'
        chunks = [json.loads(frame) for frame in frames[:-1]]
        assert ''.join(c['choices'][0]['delta'].get('content', '') for c in chunks) == 'Hello world'
        assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'
    else:
        assert response.json()['choices'][0]['message']['content'] == 'Hello world'
        assert 'usage' not in response.json()


def test_unsupported_parameters_are_not_silently_ignored(client):
    response = client.post('/v1/chat/completions', headers=IH, json={
        'messages': [{'role': 'user', 'content': 'hello'}], 'tools': [],
    })
    assert response.status_code == 422


def test_store_persists_and_reuses_device_id(tmp_path):
    store = Store(str(tmp_path))
    account = store.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
    device = store.get_account(account['id'])['device_id']
    store.close()
    reopened = Store(str(tmp_path))
    assert reopened.get_account(account['id'])['device_id'] == device
    reopened.close()


def test_google_verify_session_handling(client):
    response = client.post('/admin/api/auth/google-verify', headers=AH, json={
        'phone_number': '+861****0000', 'code': 'test-code', 'state': 'test-state'
    })
    assert response.status_code == 400


def test_api_key_management_and_inference_auth(client):
    res = client.post('/admin/api/keys', headers=AH, json={'name': 'Test Token', 'key': 'sk-wb-test-123456'})
    assert res.status_code == 200
    key_id = res.json()['id']
    key_val = res.json()['key']
    assert key_val == 'sk-wb-test-123456'

    list_res = client.get('/admin/api/keys', headers=AH)
    assert any(k['id'] == key_id for k in list_res.json())

    models_res = client.get('/v1/models', headers={'Authorization': f'Bearer {key_val}'})
    assert models_res.status_code == 200

    client.patch(f'/admin/api/keys/{key_id}', headers=AH, json={'enabled': False})
    fail_res = client.get('/v1/models', headers={'Authorization': f'Bearer {key_val}'})
    assert fail_res.status_code == 401

    del_res = client.delete(f'/admin/api/keys/{key_id}', headers=AH)
    assert del_res.status_code == 200
