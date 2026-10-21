import asyncio
import json
import time

import httpx
import pytest

from wb2api.store import Store
from wb2api.protocol import ProtocolMapper
from wb2api.wabi import UpstreamError, Wabi


MAPPING = {'delta_events': ['fixture.delta'], 'done_events': ['fixture.done'],
           'error_events': ['fixture.error']}


def idle_state(**changes):
    return {'thread_id': 'thread', 'active_turn': None, 'queued_turns': [],
            'has_background_work': False, 'queue_paused': False, 'revision': 3,
            'agent_read_message_id': None, **changes}


@pytest.mark.parametrize('opening_final', [False, True])
@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'opening_error', 'disconnect', 'timeout',
                                    'state_http', 'state_malformed', 'wrong_thread', 'merged_turn',
                                    'late_error', 'late_disconnect'])
async def test_opening_barrier_waits_for_completed_message_and_idle_and_fails_closed(tmp_path, failure, opening_final):
    store = Store(str(tmp_path))
    account = store.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
    wabi = Wabi(store, {'request_timeout': 0.2 if failure == 'timeout' else 3,
                        'sse_queue_size': 2})
    created = asyncio.Event()
    opening_finished = asyncio.Event()
    submitted = asyncio.Event()
    state_checked = asyncio.Event()
    calls = []
    polls = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await created.wait()
            # Account-wide unrelated events can fill the bounded queue. The
            # opening barrier must actively consume them, including pre-ack.
            for i in range(8):
                yield ('data: ' + json.dumps({'type': 'fixture.delta', 'thread_id': 'other',
                                             'delta': {'text': str(i)}}) + '\n\n').encode()
            if failure == 'disconnect':
                return
            if failure == 'timeout':
                await asyncio.Event().wait()
            if failure == 'opening_error':
                yield ('data: ' + json.dumps({'type': 'fixture.error', 'thread_id': 'thread',
                                             'turn_id': 'opening'}) + '\n\n').encode()
                await asyncio.Event().wait()
            # Empty initialization is not the final opening response.
            for blocks in [[], [{'type': 'text', 'text': 'greeting must not leak'}]]:
                yield ('data: ' + json.dumps({'type': 'fixture.done', 'thread_id': 'thread',
                    'message': {'thread_id': 'thread', 'role': 'assistant', 'status': 'completed',
                                'turn_id': 'opening', 'is_final_message': opening_final, 'blocks': blocks}}) + '\n\n').encode()
            opening_finished.set()
            if failure in ('late_error', 'late_disconnect'):
                await state_checked.wait()
                if failure == 'late_disconnect':
                    return
                yield ('data: ' + json.dumps({'type': 'fixture.error', 'thread_id': 'thread',
                                             'turn_id': 'opening'}) + '\n\n').encode()
            await submitted.wait()
            yield ('data: ' + json.dumps({'type': 'fixture.done', 'thread_id': 'thread',
                'message': {'role': 'assistant', 'turn_id': 'user-turn',
                            'is_final_message': True,
                            'blocks': [{'type': 'text', 'text': '61'}]}}) + '\n\n').encode()
            await asyncio.Event().wait()

    async def handler(request):
        nonlocal polls
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/events'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())
        if request.url.path == '/api/v1/threads':
            created.set()
            # Give the independently consumed stream a chance to fill its queue.
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={'id': 'thread'})
        if request.url.path.endswith('/turn-state'):
            assert opening_finished.is_set(), 'initial idle cannot prove opening is complete'
            polls += 1
            if failure in ('late_error', 'late_disconnect'):
                state_checked.set()
                await asyncio.sleep(0.01)
                return httpx.Response(200, json=idle_state())
            if failure == 'state_http':
                return httpx.Response(503)
            if failure == 'state_malformed':
                return httpx.Response(200, json={'active_turn': None})
            if failure == 'wrong_thread':
                return httpx.Response(200, json=idle_state(thread_id='other'))
            # Final SSE delivery may precede the state commit. An active turn,
            # queued work, background work, or a paused queue each block POST.
            states = [idle_state(active_turn={'id': 'opening'}),
                      idle_state(queued_turns=[{'id': 'queued'}]),
                      idle_state(has_background_work=True),
                      idle_state(queue_paused=True), idle_state()]
            return httpx.Response(200, json=states[min(polls - 1, len(states) - 1)])
        if request.url.path.endswith('/messages'):
            assert opening_finished.is_set(), 'user POST raced with the opening turn'
            assert polls >= 5, 'user POST raced with pending opening work'
            submitted.set()
            return httpx.Response(200, json={'turn_id': 'opening' if failure == 'merged_turn' else 'user-turn'})
        raise AssertionError(request.url)

    await wabi.client.aclose()
    wabi.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        if failure:
            with pytest.raises(UpstreamError) as exc:
                _ = [text async for text in wabi.stream_chat(account['id'], 'prompt', MAPPING)]
            expected = {'opening_error': 'upstream_turn_error', 'disconnect': 'sse_disconnected',
                        'timeout': 'upstream_timeout', 'state_http': 'upstream_http_error',
                        'state_malformed': 'invalid_upstream_response',
                        'wrong_thread': 'invalid_upstream_response',
                        'merged_turn': 'upstream_turn_not_isolated',
                        'late_error': 'upstream_turn_error', 'late_disconnect': 'sse_disconnected'}
            assert exc.value.code == expected[failure]
            assert submitted.is_set() == (failure == 'merged_turn')
        else:
            assert ''.join([text async for text in wabi.stream_chat(account['id'], 'prompt', MAPPING)]) == '61'
        assert not wabi._readers and not wabi._locks
        assert calls.count(('POST', '/api/v1/threads')) == 1
        assert calls.count(('POST', '/api/v1/threads/thread/messages')) == (1 if failure in (None, 'merged_turn') else 0)
    finally:
        await wabi.close()
        store.close()


@pytest.mark.parametrize('change', [
    {'thread_id': 'other'}, {'message': {'thread_id': 'other'}},
    {'message': {'role': 'user'}}, {'message': {'status': 'streaming'}},
    {'message': {'status': 'failed'}}, {'message': {'status': None}},
    {'message': {'blocks': []}}, {'message': {'turn_id': ''}}, {'turn_id': 'different'},
])
def test_opening_completion_requires_matching_completed_assistant_message(change):
    payload = {'type': 'message_finished', 'thread_id': 'thread', 'message': {
        'id': 'opening-message', 'thread_id': 'thread', 'role': 'assistant',
        'status': 'completed', 'turn_id': 'opening', 'is_final_message': False,
        'blocks': [{'type': 'text', 'text': 'any unmodified opening content'}]}}
    mapper = ProtocolMapper({**MAPPING, 'done_events': ['message_finished']})
    assert mapper.opening_turn({'data': json.dumps(payload)}, 'thread') == 'opening'
    for key, value in change.items():
        if key == 'message':
            payload['message'].update(value)
        else:
            payload[key] = value
    assert mapper.opening_turn({'data': json.dumps(payload)}, 'thread') is None
    assert not mapper.has_text and mapper.emitted == ''
