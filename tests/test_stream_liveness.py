import asyncio
import json
import pytest
from wb2api.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('tools', [False, True])
async def test_sse_starts_while_upstream_is_still_waiting(tmp_path, tools):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'stream-admin', 'api_key': 'stream-client'})
    async with app.router.lifespan_context(app):
        app.state.store.add_account(label='fixture', refresh_token='fixture', firebase_api_key='fixture')
        gate = asyncio.Event()
        started = asyncio.Event()
        data_sent = asyncio.Event()
        frames = []
        async def upstream(*args):
            await gate.wait()
            yield 'complete'
        app.state.wabi.stream_chat = upstream
        payload = {'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True}
        if tools:
            payload['tools'] = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {}}}]
        body = json.dumps(payload).encode()
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            frames.append(message)
            if message['type'] == 'http.response.start':
                started.set()
            if message['type'] == 'http.response.body' and message.get('body'):
                data_sent.set()
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions',
                 'query_string': b'', 'headers': [(b'authorization', b'Bearer stream-client'),
                    (b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())],
                 'client': ('127.0.0.1', 1), 'server': ('test', 80)}
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            await asyncio.wait_for(data_sent.wait(), 0.5)
            assert not gate.is_set()
            gate.set()
            await asyncio.wait_for(task, 2)
            wire = b''.join(f.get('body', b'') for f in frames)
            assert b'[DONE]' in wire and b'complete' in wire
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_or_replay_upstream():
    from wb2api.streaming import with_heartbeats
    gate = asyncio.Event()
    closed = []
    calls = []
    async def source():
        try:
            calls.append('started')
            await gate.wait()
            yield 'data: final\n\n'
        finally:
            closed.append(True)
    stream = with_heartbeats(source(), interval=0.01)
    assert await anext(stream) == ': connected\n\n'
    assert await anext(stream) == ': keep-alive\n\n'
    assert await anext(stream) == ': keep-alive\n\n'
    assert calls == ['started']
    await stream.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_real_asgi_disconnect_finishes_async_cleanup():
    from starlette.responses import StreamingResponse
    from wb2api.streaming import with_heartbeats
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield 'unused'
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()
    async def receive():
        await entered.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        pass
    response = StreamingResponse(with_heartbeats(source(), interval=0.01))
    await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send), 1)
    assert cleaned.is_set()
