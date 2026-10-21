import asyncio
import json
import stat
import uuid

import pytest

from wb2api.observability import RequestLogMiddleware


async def invoke(middleware, path='/v1/chat/completions', app_receive=None):
    sent = []
    async def receive():
        return {'type': 'http.request', 'body': b'body-secret', 'more_body': False}
    async def send(message):
        sent.append(message)
    scope = {'type': 'http', 'method': 'POST', 'path': path,
             'query_string': b'token=query-secret',
             'headers': [(b'authorization', b'Bearer auth-secret'),
                         (b'x-request-id', b'client-secret')]}
    await middleware(scope, app_receive or receive, send)
    return sent


async def ok(scope, receive, send):
    await receive()
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'x-request-id', b'unsafe-upstream-id')]})
    await send({'type': 'http.response.body', 'body': b'response-secret'})


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'logs/requests.log').read_text().splitlines()]


@pytest.mark.asyncio
async def test_safe_fields_and_server_generated_id(tmp_path):
    middleware = RequestLogMiddleware(ok, tmp_path)
    sent = await invoke(middleware)
    middleware.close()
    row = records(tmp_path)[0]
    assert row['path'] == '/v1/chat/completions' and row['status'] == 200
    assert row['completed'] and not row['disconnected'] and row['error_type'] is None
    assert 0 <= row['first_byte_ms'] <= row['duration_ms']
    assert str(uuid.UUID(row['request_id'])) == row['request_id']
    assert [v.decode() for k, v in sent[0]['headers'] if k == b'x-request-id'] == [row['request_id']]
    text = (tmp_path / 'logs/requests.log').read_text()
    for secret in ['query-secret', 'body-secret', 'response-secret', 'auth-secret', 'client-secret', 'unsafe-upstream-id']:
        assert secret not in text
    assert stat.S_IMODE((tmp_path / 'logs').stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'logs/requests.log').stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_unknown_and_dynamic_paths_never_leak_ids(tmp_path):
    middleware = RequestLogMiddleware(ok, tmp_path)
    await invoke(middleware, '/unknown/path-secret')
    await invoke(middleware, '/admin/api/accounts/account-secret/reauth')
    middleware.close()
    assert [r['path'] for r in records(tmp_path)] == ['<unmatched>', '/admin/api/accounts/{account_id}/reauth']
    assert 'secret' not in (tmp_path / 'logs/requests.log').read_text()


@pytest.mark.asyncio
async def test_stream_timing_and_partial_failure(tmp_path):
    async def broken(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await asyncio.sleep(0.01)
        await send({'type': 'http.response.body', 'body': b'heartbeat', 'more_body': True})
        await asyncio.sleep(0.01)
        raise ValueError('token=error-secret')
    middleware = RequestLogMiddleware(broken, tmp_path)
    with pytest.raises(ValueError):
        await invoke(middleware)
    middleware.close()
    row = records(tmp_path)[0]
    assert row['status'] == 200 and not row['completed']
    assert row['error_type'] == 'ApplicationError'
    assert 5 <= row['first_byte_ms'] < row['duration_ms']
    assert 'error-secret' not in (tmp_path / 'logs/requests.log').read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['cancel', 'disconnect', 'incomplete', 'early'])
async def test_non_success_completion(tmp_path, failure):
    async def app(scope, receive, send):
        if failure == 'early':
            raise RuntimeError('secret')
        if failure == 'disconnect':
            await receive()
            return
        if failure == 'cancel':
            raise asyncio.CancelledError('secret')
    middleware = RequestLogMiddleware(app, tmp_path)
    async def disconnected():
        return {'type': 'http.disconnect'}
    if failure in ('cancel', 'early'):
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else RuntimeError):
            await invoke(middleware)
    else:
        await invoke(middleware, app_receive=disconnected)
    middleware.close()
    row = records(tmp_path)[0]
    assert not row['completed'] and row['first_byte_ms'] is None
    assert row['status'] is None
    assert row['disconnected'] == (failure in ('cancel', 'disconnect'))
    assert row['error_type'] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('backups', [0, 3])
async def test_rotation_strict_total_budget_and_permissions(tmp_path, backups):
    budget = 4096
    middleware = RequestLogMiddleware(ok, tmp_path, max_bytes=budget, backups=backups)
    for _ in range(100):
        await invoke(middleware)
    middleware.close()
    files = list((tmp_path / 'logs').iterdir())
    assert len(files) == backups + 1
    assert sum(p.stat().st_size for p in files) <= budget
    for p in files:
        assert p.stat().st_size <= budget // (backups + 1)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert all(json.loads(line)['completed'] for line in p.read_text().splitlines())


@pytest.mark.asyncio
async def test_disabled_creates_no_directory_but_still_correlates(tmp_path):
    middleware = RequestLogMiddleware(ok, tmp_path, max_bytes=0)
    sent = await invoke(middleware)
    middleware.close()
    assert not (tmp_path / 'logs').exists()
    assert any(k == b'x-request-id' for k, v in sent[0]['headers'])


def test_invalid_settings_and_shrinking_existing_budget(tmp_path):
    for kwargs in [{'max_bytes': -1}, {'max_bytes': 100}, {'backups': -1}, {'backups': 33}]:
        with pytest.raises(ValueError):
            RequestLogMiddleware(ok, tmp_path, **kwargs)
    logs = tmp_path / 'logs'
    logs.mkdir(exist_ok=True)
    (logs / 'requests.log').write_text('x' * 5000)
    (logs / 'requests.log.9').write_text('x' * 5000)
    middleware = RequestLogMiddleware(ok, tmp_path, max_bytes=4096)
    middleware.close()
    assert sum(p.stat().st_size for p in logs.iterdir()) <= 4096


@pytest.mark.asyncio
async def test_lifespan_closes_handler_and_instances_are_independent(tmp_path):
    async def lifecycle(scope, receive, send):
        await send({'type': 'lifespan.shutdown.complete'})
    middleware = RequestLogMiddleware(lifecycle, tmp_path)
    async def send(message):
        pass
    await middleware({'type': 'lifespan'}, None, send)
    assert middleware.handler.stream is None
    assert middleware.logger.handlers == []
    other = RequestLogMiddleware(ok, tmp_path / 'other')
    assert other.logger is not middleware.logger
    await invoke(other)
    other.close()
    assert len(records(tmp_path / 'other')) == 1


@pytest.mark.asyncio
async def test_restart_retains_records_and_untrusted_method_is_redacted(tmp_path):
    first = RequestLogMiddleware(ok, tmp_path)
    await invoke(first)
    first.close()
    second = RequestLogMiddleware(ok, tmp_path)
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    await second({'type': 'http', 'method': 'method-secret', 'path': '/'}, receive, send)
    second.close()
    rows = records(tmp_path)
    assert len(rows) == 2 and rows[-1]['method'] == 'OTHER'
    assert 'method-secret' not in (tmp_path / 'logs/requests.log').read_text()


@pytest.mark.asyncio
async def test_send_failure_and_log_io_failure_do_not_leak(tmp_path, capsys):
    middleware = RequestLogMiddleware(ok, tmp_path)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    async def send(message):
        raise BrokenPipeError('transport-secret')
    with pytest.raises(BrokenPipeError):
        await middleware({'type': 'http', 'method': 'GET', 'path': '/'}, receive, send)
    row = records(tmp_path)[0]
    assert row['error_type'] == 'TransportError' and row['disconnected']
    assert not row['completed'] and row['status'] is None
    middleware.handler.stream.close()
    # A failed log write does not turn a successful request into an API failure.
    await invoke(middleware)
    middleware.close()
    assert capsys.readouterr().err == ''


def test_gateway_stream_error_is_recorded_as_failure(tmp_path):
    from fastapi.testclient import TestClient
    from wb2api.main import create_app
    from wb2api.wabi import UpstreamError
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'log-test-admin', 'api_key': 'log-test-client'})
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', refresh_token='fixture', firebase_api_key='fixture')
        async def upstream(*args):
            raise UpstreamError('safe message', 504, 'upstream_timeout')
            yield
        app.state.wabi.stream_chat = upstream
        r = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer log-test-client'},
                        json={'messages': [{'role': 'user', 'content': 'private-prompt'}], 'stream': True})
        assert r.status_code == 200 and '[DONE]' not in r.text
    row = records(tmp_path)[0]
    assert row['request_id'] == r.headers['x-request-id']
    assert row['gateway_error_code'] == 'upstream_timeout'
    assert not row['completed'] and row['first_byte_ms'] is not None
    assert 'private-prompt' not in (tmp_path / 'logs/requests.log').read_text()
