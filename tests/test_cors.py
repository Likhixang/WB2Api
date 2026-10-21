import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app

ORIGIN = 'https://browser.example.invalid'
HEADERS = {'Origin': ORIGIN}
AUTH = {**HEADERS, 'Authorization': 'Bearer cors-client-key'}


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'cors-admin-key', 'api_key': 'cors-client-key'})
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize('path,method', [('/v1/models', 'GET'), ('/v1/chat/completions', 'POST'),
    ('/v1/responses', 'POST'), ('/v1/messages', 'POST'), ('/v1/images/generations', 'POST'),
    ('/v1/videos/generations', 'POST'), ('/v1/responses/resp_fixture', 'GET')])
def test_preflight(client, path, method):
    r = client.options(path, headers={**HEADERS, 'Access-Control-Request-Method': method,
        'Access-Control-Request-Headers': 'authorization,content-type,x-api-key,anthropic-version,anthropic-dangerous-direct-browser-access,x-stainless-lang'})
    assert r.status_code == 200
    assert r.headers['access-control-allow-origin'] == '*'
    assert method in r.headers['access-control-allow-methods']
    assert 'access-control-allow-credentials' not in r.headers


@pytest.mark.parametrize('path', ['/admin/api/status', '/admin/api/keys', '/admin/api/accounts/export', '/', '/healthz', '/v10/models'])
def test_no_management_cors(client, path):
    for r in (client.get(path, headers=HEADERS), client.options(path, headers={**HEADERS, 'Access-Control-Request-Method': 'GET'})):
        assert 'access-control-allow-origin' not in r.headers


def test_auth_and_errors_preserved(client):
    r = client.get('/v1/models', headers=HEADERS)
    assert r.status_code == 401 and r.headers['access-control-allow-origin'] == '*'
    r = client.get('/v1/models', headers=AUTH)
    assert r.status_code == 200 and r.headers['access-control-allow-origin'] == '*'
    r = client.post('/v1/chat/completions', headers=AUTH, json={'messages': []})
    assert r.status_code == 400 and r.headers['access-control-allow-origin'] == '*'
    r = client.options('/v1/models', headers={**HEADERS, 'Access-Control-Request-Method': 'DELETE'})
    assert r.status_code == 400


@pytest.mark.parametrize('stream', [False, True])
def test_success_and_stream_diagnostic_headers(client, stream):
    client.app.state.store.add_account(label='fixture', id_token='fixture', expires_at=9999999999)
    async def upstream(*args):
        yield 'cross-origin answer'
    client.app.state.wabi.stream_chat = upstream
    r = client.post('/v1/chat/completions', headers=AUTH, json={
        'messages': [{'role': 'user', 'content': 'hello'}], 'stream': stream,
        'reasoning_effort': 'low', 'temperature': 0.7})
    assert r.status_code == 200 and 'cross-origin answer' in r.text
    assert r.headers['access-control-allow-origin'] == '*'
    assert r.headers['x-wb2api-ignored-parameters'] == 'temperature'
    exposed = {x.strip().lower() for x in r.headers['access-control-expose-headers'].split(',')}
    assert {'x-request-id', 'x-wb2api-ignored-parameters', 'x-wb2api-reasoning-effort'} <= exposed
    assert 'access-control-allow-credentials' not in r.headers


def test_non_browser_requests_unchanged(client):
    r = client.get('/v1/models', headers={'Authorization': AUTH['Authorization']})
    assert r.status_code == 200 and 'access-control-allow-origin' not in r.headers
