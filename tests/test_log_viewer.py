from fastapi.testclient import TestClient
from wb2api.main import create_app
from wb2api.observability import RequestLogMiddleware
import pytest


def test_logs_admin_only_bounded_and_filterable(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'viewer-admin', 'api_key': 'viewer-client'})
    h = {'Authorization': 'Bearer viewer-admin'}
    with TestClient(app) as c:
        r = c.get('/v1/models', headers={'Authorization': 'Bearer viewer-client'})
        rid = r.headers['x-request-id']
        assert c.get('/admin/api/logs').status_code == 401
        assert c.get('/admin/api/logs', headers={'Authorization': 'Bearer viewer-client'}).status_code == 401
        data = c.get('/admin/api/logs', headers=h, params={'request_id': rid}).json()
        assert len(data['entries']) == 1 and data['entries'][0]['request_id'] == rid
        assert data['storage']['max_bytes'] == 20971520
        assert data['storage']['used_bytes'] > 0
        assert c.get('/admin/api/logs', headers=h, params={'errors_only': True, 'request_id': rid}).json()['entries'] == []
        before = (tmp_path / 'logs/requests.log').stat().st_size
        c.get('/admin/api/logs', headers=h)
        assert (tmp_path / 'logs/requests.log').stat().st_size == before
        assert c.get('/admin/api/logs', headers=h, params={'limit': 999}).status_code == 422
        assert c.get('/admin/api/logs', headers=h, params={'request_id': '../../private'}).status_code == 422


@pytest.mark.asyncio
async def test_inflight_request_snapshot(tmp_path):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b': connected\n\n', 'more_body': True})
        entered.set()
        await release.wait()
        await send({'type': 'http.response.body', 'body': b'complete'})
    m = RequestLogMiddleware(app, tmp_path)
    async def recv():
        return {'type': 'http.request', 'body': b''}
    async def send(message):
        pass
    t = asyncio.create_task(m({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, recv, send))
    await entered.wait()
    snap = m.snapshot()
    assert snap['active_count'] == 1 and snap['entries'][0]['in_flight']
    assert snap['entries'][0]['first_byte_ms'] is not None
    release.set()
    await t
    assert m.snapshot()['active_count'] == 0 and m.snapshot()['entries'][0]['completed']
    m.close()


def test_malformed_log_types_are_skipped(tmp_path):
    import json
    async def app(*args):
        pass
    m = RequestLogMiddleware(app, tmp_path)
    p = tmp_path / 'logs/requests.log'
    good = {'request_id': 'ok', 'timestamp': '2026-10-21T00:00:00Z', 'status': 200, 'completed': True}
    with p.open('a') as f:
        for row in ({**good, 'status': '200'}, {**good, 'timestamp': None}, {**good, 'extra_private': 'must-not-return'}):
            f.write(json.dumps(row) + '\n')
    result = m.snapshot()
    assert len(result['entries']) == 1 and 'extra_private' not in result['entries'][0]
    m.close()
