import asyncio
import json
import time

import httpx
import pytest

from wb2api.protocol import ProtocolError, ProtocolMapper, parse_sse
from wb2api.store import Store
from wb2api.wabi import UpstreamError, Wabi

MAPPING = {'delta_events': ['fixture.delta'], 'done_events': ['fixture.done'],
           'error_events': ['fixture.error']}


def event(kind, text=None, **ids):
    data = {'type': kind, **ids}
    if text is not None:
        data['delta'] = {'text': text}
    return {'event': 'message', 'data': json.dumps(data), 'id': ''}


@pytest.mark.asyncio
async def test_sse_framing_multiline_and_size_bound():
    async def lines():
        for line in [': heartbeat', '', 'id: 7', 'event: fixture', 'data: one', 'data: two', '', 'data: unterminated']:
            yield line
    assert [e async for e in parse_sse(lines())] == [{'event': 'fixture', 'data': 'one\ntwo', 'id': '7'}]
    with pytest.raises(ProtocolError):
        _ = [e async for e in parse_sse(lines(), max_event_bytes=4)]


def test_mapper_isolates_threads_and_turns_and_detects_empty_completion():
    mapper = ProtocolMapper(MAPPING)
    assert mapper.consume(event('fixture.delta', 'wrong', thread_id='other', turn_id='turn'), 'thread', 'turn') == (None, False)
    assert mapper.consume(event('fixture.delta', 'global'), 'thread', 'turn') == (None, False)
    assert mapper.consume(event('fixture.delta', 'wrong', turn_id='other'), 'thread', 'turn') == (None, False)
    with pytest.raises(ProtocolError, match='without text'):
        mapper.consume(event('fixture.done', turn_id='turn'), 'thread', 'turn')
    assert mapper.consume(event('fixture.delta', 'yes', turn_id='turn'), 'thread', 'turn') == ('yes', False)
    assert mapper.consume(event('fixture.done', turn_id='turn'), 'thread', 'turn') == (None, True)


def test_snapshot_revisions_and_invalid_json_fail_closed():
    mapper = ProtocolMapper({**MAPPING, 'mode': 'snapshot'})
    assert mapper.consume(event('fixture.delta', 'a', turn_id='turn'), 'thread', 'turn') == ('a', False)
    assert mapper.consume(event('fixture.delta', 'ab', turn_id='turn'), 'thread', 'turn') == ('b', False)
    with pytest.raises(ProtocolError, match='revised'):
        mapper.consume(event('fixture.delta', 'x', turn_id='turn'), 'thread', 'turn')
    with pytest.raises(ProtocolError):
        mapper.consume({'data': '{"a":1,"a":2}'}, 'thread', 'turn')
    with pytest.raises(ProtocolError) as error:
        ProtocolMapper({})
    assert error.value.status == 501


@pytest.mark.asyncio
async def test_pipeline_subscribes_before_post_and_closes_reader(tmp_path):
    store = Store(str(tmp_path))
    account = store.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
    wabi = Wabi(store, {'request_timeout': 5})
    subscribed = asyncio.Event()
    submitted = asyncio.Event()
    order = []

    class FixtureStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            subscribed.set()
            await submitted.wait()
            # The response can arrive before the POST acknowledgement is processed.
            for payload in [event('fixture.delta', 'hello', thread_id='thread', turn_id='turn'),
                            event('fixture.done', thread_id='thread', turn_id='turn')]:
                yield ('data: ' + payload['data'] + '\n\n').encode()
            await asyncio.Event().wait()

    async def handler(request):
        order.append(request.url.path)
        if request.url.path.endswith('/events'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=FixtureStream())
        if request.url.path == '/api/v1/threads':
            assert subscribed.is_set()
            return httpx.Response(200, json={'id': 'thread'})
        if request.url.path.endswith('/messages'):
            assert subscribed.is_set()
            submitted.set()
            return httpx.Response(200, json={'turn_id': 'turn'})
        raise AssertionError(request.url)

    await wabi.client.aclose()
    wabi.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert ''.join([text async for text in wabi.stream_chat(account['id'], 'prompt', MAPPING)]) == 'hello'
        assert order == ['/api/v1/chat/events', '/api/v1/threads', '/api/v1/threads/thread/messages']
        assert not wabi._readers and not wabi._locks
    finally:
        await wabi.close()
        store.close()


@pytest.mark.asyncio
async def test_refresh_persists_rotated_token_and_does_not_retry(tmp_path):
    store = Store(str(tmp_path))
    account = store.add_account(label='fixture', refresh_token='old', firebase_api_key='fixture-key')
    wabi = Wabi(store, {})
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == 'securetoken.googleapis.com':
            return httpx.Response(200, json={'id_token': 'new-id', 'refresh_token': 'rotated', 'expires_in': '3600'})
        return httpx.Response(401, json={'sensitive': 'must-not-leak'})

    await wabi.client.aclose()
    wabi.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as error:
            await wabi.validate_account(account['id'])
        assert error.value.status == 401
        assert 'must-not-leak' not in str(error.value)
        assert calls == ['securetoken.googleapis.com', 'app.getwabi.ai']
        assert store.get_account(account['id'])['refresh_token'] == 'rotated'
    finally:
        await wabi.close()
        store.close()
