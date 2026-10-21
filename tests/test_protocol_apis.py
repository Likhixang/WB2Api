"""Protocol fixtures exercise wire compatibility, not paid/model-native capabilities."""
import asyncio
import json
import re
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from wb2api.main import Completion, create_app
from wb2api.protocol_apis import ResponseCache, install_protocol_apis
from wb2api.tool_compat import ToolAdapter


HEADERS = {'Authorization': 'Bearer inference-one'}
TOOL = {'type': 'function', 'name': 'lookup', 'parameters': {
    'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q'], 'additionalProperties': False}}
ATOOL = {'name': 'lookup', 'input_schema': TOOL['parameters']}
CALL = {'id': 'call_fixture', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"中文"}'}}


def events(response):
    return [json.loads('\n'.join(line[6:] for line in frame.splitlines() if line.startswith('data: ')))
            for frame in response.text.split('\n\n') if any(line.startswith('data: ') for line in frame.splitlines())]


@pytest.fixture
def api():
    app, calls = FastAPI(), []

    def authorize(request: Request):
        if request.headers.get('authorization') not in ('Bearer inference-one', 'Bearer inference-two'):
            raise HTTPException(401, 'Invalid bearer key')

    async def completion(body, request):
        calls.append(body)
        mode = request.headers.get('x-fixture', 'text')
        if mode == 'http_error':
            return JSONResponse({'error': {'message': 'Fixture unavailable', 'code': 'no_account', 'type': 'wb2api_error'}},
                                status_code=503, headers={'Retry-After': '3'})
        tool = mode in ('tool', 'parallel', 'bad_args')
        message = {'role': 'assistant', 'content': None if tool else 'Hello 中文'}
        if tool:
            message['tool_calls'] = [json.loads(json.dumps(CALL))]
            if mode == 'parallel':
                message['tool_calls'].append({**CALL, 'id': 'call_second'})
            if mode == 'bad_args':
                message['tool_calls'][0]['function']['arguments'] = '{"q":123}'
        finish = 'tool_calls' if tool else 'stop'
        headers = {'X-WB2Api-Tool-Stream': 'buffered', 'X-WB2Api-Reasoning-Mode': 'prompt'}
        if not body.stream:
            return JSONResponse({'choices': [{'message': message, 'finish_reason': finish}]}, headers=headers)

        async def stream():
            yield ': heartbeat\n\n'
            if mode == 'error':
                yield 'data: {"error":{"message":"Fixture failure","code":"fixture_failure"}}\n\n'
                return
            chunks = []
            if tool:
                for index, call in enumerate(message['tool_calls']):
                    # Incremental argument fragments must never leak before validation.
                    chunks.extend([{'tool_calls': [{'index': index, 'id': call['id'], 'type': 'function',
                                                    'function': {'name': call['function']['name'], 'arguments': call['function']['arguments'][:4]}}]},
                                   {'tool_calls': [{'index': index, 'function': {'arguments': call['function']['arguments'][4:]}}]}])
            else:
                chunks = [{'role': 'assistant'}, {'content': 'Hello '}, {'content': '中文'}]
            for delta in chunks:
                wire = ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n').encode()
                # Split inside UTF-8 and SSE boundaries.
                for index in range(0, len(wire), 7):
                    yield wire[index:index + 7]
            if mode == 'truncated':
                return
            yield 'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]}) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream', headers=headers)

    cache = install_protocol_apis(app, completion, Completion, authorize)
    with TestClient(app) as client:
        yield client, calls, cache


@pytest.mark.parametrize('stream', [False, True])
def test_responses_text_alias_headers_usage(api, stream):
    client, calls, _ = api
    response = client.post('/v1/responses', headers=HEADERS, json={
        'model': 'sabi', 'input': 'hi', 'stream': stream, 'instructions': 'Be terse',
        'reasoning': {'effort': 'high', 'summary': 'auto'}, 'metadata': {'client': 'fixture'}, 'unknown_hint': True})
    assert response.status_code == 200, response.text
    assert response.headers['x-wb2api-reasoning-mode'] == 'prompt'
    assert len(calls) == 1 and calls[0].reasoning_effort == 'high'
    assert calls[0].messages[0].role == 'developer'
    if stream:
        frames = events(response)
        assert frames[0]['type'] == 'response.created'
        assert frames[1]['type'] == 'response.in_progress'
        assert ': heartbeat' in response.text
        assert [item['sequence_number'] for item in frames] == list(range(len(frames)))
        result = frames[-1]['response']
        assert frames[-1]['type'] == 'response.completed'
        ids = {item['item_id'] for item in frames if 'item_id' in item}
        assert ids == {result['output'][0]['id']}
        assert sum(item['type'] == 'response.output_text.delta' for item in frames) == 2
    else:
        result = response.json()
    assert result['model'] == 'sabi' and result['usage'] is None
    assert result['output'][0]['content'][0]['text'] == 'Hello 中文'
    assert client.get('/v1/responses/' + result['id'], headers=HEADERS).json() == result


@pytest.mark.parametrize('stream', [False, True])
def test_responses_parallel_tools_continuation(api, stream):
    client, calls, _ = api
    first = client.post('/v1/responses', headers={**HEADERS, 'x-fixture': 'parallel'}, json={
        'input': 'find two', 'tools': [TOOL], 'tool_choice': 'required', 'stream': stream})
    assert first.status_code == 200, first.text
    result = events(first)[-1]['response'] if stream else first.json()
    output = result['output']
    assert [item['call_id'] for item in output] == ['call_fixture', 'call_second']
    if stream:
        frames = events(first)
        assert sum(frame['type'] == 'response.function_call_arguments.delta' for frame in frames) == 2
        assert not any(frame['type'] == 'response.output_text.delta' for frame in frames)
        for index, item in enumerate(output):
            add = next(frame for frame in frames if frame['type'] == 'response.output_item.added' and frame['output_index'] == index)
            assert add['item']['id'] == item['id'] and add['item']['arguments'] == ''
    second = client.post('/v1/responses', headers=HEADERS, json={
        'previous_response_id': result['id'], 'tools': [TOOL], 'instructions': 'New policy',
        'input': [{'type': 'function_call_output', 'call_id': item['call_id'], 'output': 'result ' + item['call_id']} for item in output]})
    assert second.status_code == 200, second.text
    assert len(calls) == 2
    history = calls[-1].messages
    assert [message.role for message in history] == ['developer', 'user', 'assistant', 'tool', 'tool']
    assert history[-1].content == 'result call_second' and history[-2].tool_call_id == 'call_fixture'
    third = client.post('/v1/responses', headers=HEADERS, json={'previous_response_id': second.json()['id'], 'input': 'continue'})
    assert third.status_code == 200, third.text
    assert len(calls) == 3
    assert len([m for m in calls[-1].messages if m.role == 'developer']) == 0  # instructions do not persist


def test_flat_history_annotations_forced_choice(api):
    client, calls, _ = api
    response = client.post('/v1/responses', headers={**HEADERS, 'x-fixture': 'tool'}, json={
        'tools': [TOOL], 'tool_choice': {'type': 'function', 'name': 'lookup'},
        'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'find', 'cache_control': {'type': 'ephemeral'}}]},
                  {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'searching', 'annotations': []}], 'metadata': {}},
                  {'type': 'function_call', 'call_id': 'old_one', 'name': 'lookup', 'arguments': '{"q":"a"}'},
                  {'type': 'function_call', 'call_id': 'old_two', 'name': 'lookup', 'arguments': '{"q":"b"}'},
                  {'type': 'function_call_output', 'call_id': 'old_one', 'output': [{'type': 'text', 'text': 'first'}]},
                  {'type': 'function_call_output', 'call_id': 'old_two', 'output': 'second'}]})
    assert response.status_code == 200, response.text
    assert calls[-1].tool_choice == {'type': 'function', 'function': {'name': 'lookup'}}
    assert [call.id for call in calls[-1].messages[1].tool_calls] == ['old_one', 'old_two']


@pytest.mark.parametrize('stream', [False, True])
def test_anthropic_tool_roundtrip(api, stream):
    client, calls, _ = api
    response = client.post('/v1/messages', headers={'x-api-key': 'inference-one', 'x-fixture': 'tool'}, json={
        'model': 'default', 'max_tokens': 100, 'stream': stream,
        'system': [{'type': 'text', 'text': 'Be precise', 'cache_control': {'type': 'ephemeral'}}],
        'messages': [{'role': 'user', 'content': 'lookup'}], 'tools': [ATOOL],
        'tool_choice': {'type': 'any', 'disable_parallel_tool_use': True}, 'thinking': {'type': 'adaptive'},
        'output_config': {'effort': 'high'}})
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and not calls[0].parallel_tool_calls
    assert calls[0].reasoning_effort == 'high'
    if stream:
        frames = events(response)
        assert frames[0]['message']['model'] == 'default' and frames[0]['message']['usage'] == {}
        assert [frame['type'] for frame in frames] == ['message_start', 'ping', 'content_block_start',
                  'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop']
        block = frames[2]['content_block']
        block['input'] = json.loads(frames[3]['delta']['partial_json'])
        assert frames[-2]['delta']['stop_reason'] == 'tool_use' and frames[-2]['usage'] == {}
    else:
        result = response.json()
        assert result['stop_reason'] == 'tool_use' and result['model'] == 'default' and result['usage'] == {}
        block = result['content'][0]
    assert block == {'type': 'tool_use', 'id': 'call_fixture', 'name': 'lookup', 'input': {'q': '中文'}}
    response = client.post('/v1/messages', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'lookup'}, {'role': 'assistant', 'content': [block]},
                     {'role': 'user', 'content': [{'type': 'text', 'text': 'Now summarize'},
                       {'type': 'tool_result', 'tool_use_id': block['id'], 'content': [{'type': 'text', 'text': 'Complete result', 'annotations': []}]}]}],
        'tools': [ATOOL], 'max_tokens': 100})
    assert response.status_code == 200, response.text
    assert response.json()['stop_reason'] == 'end_turn'
    assert [message.role for message in calls[-1].messages] == ['user', 'assistant', 'tool', 'user']
    assert calls[-1].messages[-2].content == 'Complete result'


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('mode', ['error', 'truncated', 'bad_args'])
def test_stream_failure_never_completes_or_leaks_tools(api, protocol, mode):
    client, calls, cache = api
    body = {'stream': True, 'tools': [TOOL], 'input': 'hi'} if protocol == 'responses' else {
        'stream': True, 'tools': [ATOOL], 'messages': [{'role': 'user', 'content': 'hi'}]}
    response = client.post('/v1/' + protocol, headers={**HEADERS, 'x-fixture': mode}, json=body)
    frames = events(response)
    assert any(frame['type'] == 'error' for frame in frames), response.text
    assert not any(frame['type'] in ('response.completed', 'message_stop') for frame in frames)
    assert not any(frame['type'] in ('response.function_call_arguments.delta', 'content_block_start') and
                   (frame.get('content_block') or {}).get('type') == 'tool_use' for frame in frames)
    assert len(calls) == 1 and not cache.entries
    if mode == 'bad_args':
        assert '"q":123' not in response.text


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('token', [None, 'admin-key', 'invalid'])
def test_inference_auth_only(api, protocol, token):
    client, calls, _ = api
    headers = {} if token is None else {'x-api-key' if protocol == 'messages' else 'authorization': token if protocol == 'messages' else 'Bearer ' + token}
    response = client.post('/v1/' + protocol, headers=headers, json={'input': 'hello'})
    assert response.status_code == 401 and not calls
    if protocol == 'messages':
        assert response.json()['type'] == 'error' and response.json()['error']['type'] == 'authentication_error'


def test_cache_key_isolation_store_false_and_expiry(api):
    client, _, cache = api
    first = client.post('/v1/responses', headers=HEADERS, json={'input': 'private'}).json()
    assert client.get('/v1/responses/' + first['id'], headers={'Authorization': 'Bearer inference-two'}).status_code == 404
    assert client.post('/v1/responses', headers={'Authorization': 'Bearer inference-two'},
                       json={'input': 'next', 'previous_response_id': first['id']}).status_code == 404
    second = client.post('/v1/responses', headers=HEADERS, json={'input': 'private', 'store': False}).json()
    assert client.get('/v1/responses/' + second['id'], headers=HEADERS).status_code == 404
    cache.ttl = -1
    third = client.post('/v1/responses', headers=HEADERS, json={'input': 'expires'}).json()
    response = client.post('/v1/responses', headers=HEADERS, json={'input': 'next', 'previous_response_id': third['id']})
    assert response.status_code == 404 and 'process restart' in response.text
    assert all('inference-one' not in key[0] for key in cache.entries)


def test_cache_bounded_lru_and_bytes():
    cache = ResponseCache(max_entries=2, max_bytes=1000, max_entry_bytes=400)
    assert cache.put('owner', '1', {'value': 'one'}, [])
    assert cache.put('owner', '2', {'value': 'two'}, [])
    cache.get('owner', '1')
    assert cache.put('owner', '3', {'value': 'three'}, [])
    assert set(key[1] for key in cache.entries) == {'1', '3'}
    assert not cache.put('owner', 'huge', {'value': 'x' * 500}, [])
    assert cache.bytes == sum(len(entry[1]) for entry in cache.entries.values()) <= 1000


@pytest.mark.parametrize('extra', [
    {'tools': [{'type': 'web_search'}]}, {'tools': [{'type': 'custom', 'name': 'shell'}]},
    {'input': [{'type': 'input_image', 'image_url': 'x'}]}, {'input': 12}, {'stream': 'true'},
    {'tools': [TOOL], 'tool_choice': {'type': 'web_search'}}, {'tools': [TOOL], 'tool_choice': 'invalid'},
    {'input': [{'type': 'function_call_output', 'call_id': 'missing', 'output': 'x'}]},
    {'text': {'format': {'type': 'json_schema'}}}, {'background': True}, {'parallel_tool_calls': 'false'},
    {'reasoning': {'effort': 'invalid'}}, {'store': 'false'}, {'instructions': []}, {'previous_response_id': 123}])
def test_invalid_responses_before_completion(api, extra):
    client, calls, _ = api
    response = client.post('/v1/responses', headers=HEADERS, json={'input': 'hi', **extra})
    assert response.status_code == 400, response.text
    assert response.json()['error']['type'] == 'invalid_request_error' and not calls


@pytest.mark.parametrize('extra', [
    {'messages': []}, {'messages': [{'role': 'user', 'content': [{'type': 'image'}]}]},
    {'tools': [{'type': 'computer_20250124', 'name': 'computer'}]}, {'tool_choice': 'auto'},
    {'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': 'false'}},
    {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'absent', 'content': 'x'}]}]}])
def test_invalid_messages_before_completion(api, extra):
    client, calls, _ = api
    response = client.post('/v1/messages', headers=HEADERS, json={'messages': [{'role': 'user', 'content': 'hi'}], **extra})
    assert response.status_code == 400, response.text
    assert response.json()['type'] == 'error' and not calls


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
def test_http_error_envelope(api, protocol):
    client, calls, _ = api
    body = {'input': 'hi'} if protocol == 'responses' else {'messages': [{'role': 'user', 'content': 'hi'}]}
    response = client.post('/v1/' + protocol, headers={**HEADERS, 'x-fixture': 'http_error'}, json=body)
    assert response.status_code == 503 and response.json()['error']['message'] == 'Fixture unavailable'
    assert response.headers['retry-after'] == '3'
    assert response.json()['error']['type'] == ('api_error' if protocol == 'messages' else 'server_error')
    assert 'wb2api_error' not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('policy', ['none', 'parallel_disabled', 'required_missing'])
@pytest.mark.parametrize('stream', [False, True])
def test_output_tool_policy_revalidated(api, protocol, policy, stream):
    client, _, cache = api
    mode = 'text' if policy == 'required_missing' else 'parallel' if policy == 'parallel_disabled' else 'tool'
    if protocol == 'responses':
        body = {'input': 'hi', 'tools': [TOOL], 'stream': stream, 'parallel_tool_calls': policy != 'parallel_disabled',
                'tool_choice': 'required' if policy == 'required_missing' else 'none' if policy == 'none' else 'auto'}
    else:
        body = {'messages': [{'role': 'user', 'content': 'hi'}], 'tools': [ATOOL], 'stream': stream,
                'tool_choice': {'type': 'any' if policy == 'required_missing' else 'none' if policy == 'none' else 'auto',
                                'disable_parallel_tool_use': policy == 'parallel_disabled'}}
    response = client.post('/v1/' + protocol, headers={**HEADERS, 'x-fixture': mode}, json=body)
    if stream:
        kinds = [frame['type'] for frame in events(response)]
        assert 'error' in kinds and 'response.completed' not in kinds and 'message_stop' not in kinds
        assert 'response.function_call_arguments.delta' not in kinds
    else:
        assert response.status_code == 502, response.text
    assert not cache.entries


@pytest.mark.parametrize('stream', [False, True])
def test_anthropic_parallel_results_preserved(api, stream):
    client, calls, _ = api
    first = client.post('/v1/messages', headers={**HEADERS, 'x-fixture': 'parallel'}, json={
        'messages': [{'role': 'user', 'content': 'two lookups'}], 'tools': [ATOOL], 'stream': stream,
        'tool_choice': {'type': 'auto'}})
    assert first.status_code == 200, first.text
    if stream:
        frames = events(first)
        blocks = [frame['content_block'] for frame in frames if frame['type'] == 'content_block_start']
        deltas = [frame['delta']['partial_json'] for frame in frames if frame['type'] == 'content_block_delta']
        for block, delta in zip(blocks, deltas):
            block['input'] = json.loads(delta)
    else:
        blocks = first.json()['content']
    assert [block['id'] for block in blocks] == ['call_fixture', 'call_second']
    second = client.post('/v1/messages', headers=HEADERS, json={'tools': [ATOOL], 'messages': [
        {'role': 'user', 'content': 'two lookups'}, {'role': 'assistant', 'content': blocks},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': block['id'], 'content': block['id'] + ' result'} for block in blocks]}]})
    assert second.status_code == 200, second.text
    assert [m.tool_call_id for m in calls[-1].messages if m.role == 'tool'] == ['call_fixture', 'call_second']


@pytest.mark.asyncio
async def test_stream_heartbeat_is_live_before_upstream_text():
    from wb2api.protocol_apis import _stream
    release, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            yield ': still-alive\n\n'
            await release.wait()
            yield 'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'
        finally:
            closed.set()

    converted = _stream(StreamingResponse(source()), {'model': 'wabi'}, ToolAdapter(), False, lambda *_: None)
    try:
        assert 'response.created' in await anext(converted)
        assert 'response.in_progress' in await anext(converted)
        assert await asyncio.wait_for(anext(converted), .2) == ': still-alive\n\n'
        assert not release.is_set()
    finally:
        await converted.aclose()
    assert closed.is_set()


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_real_completion_kernel_tool_closed_loop(tmp_path, protocol, stream):
    """Real closure + schema validation + XML parse; deterministic unpaid upstream."""
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'admin-fixture-key', 'api_key': 'inference-one'})
    route = next(route for route in app.routes if getattr(route, 'path', '') == '/v1/chat/completions')
    install_protocol_apis(app, route.endpoint, Completion, route.dependant.dependencies[0].call)
    count = []
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', refresh_token='fixture', firebase_api_key='fixture')
        app.state.store.set_setting('protocol', json.dumps({'delta_events': ['delta'], 'done_events': ['done']}))

        async def upstream(account, prompt, protocol_config):
            count.append(prompt)
            if '<tool_result>' in prompt:
                yield 'Task complete'
                return
            start = re.search(r'<Function_[a-f0-9]+_Start/>', prompt).group()
            end = start.replace('_Start/>', '_End/>')
            yield start + '<function_calls><function_call><tool>lookup</tool><args_json>{"q":"中文"}</args_json></function_call></function_calls>' + end

        app.state.wabi.stream_chat = upstream
        if protocol == 'responses':
            body = {'input': 'look up', 'tools': [TOOL], 'stream': stream}
        else:
            body = {'messages': [{'role': 'user', 'content': 'look up'}], 'tools': [ATOOL], 'stream': stream}
        first = client.post('/v1/' + protocol, headers=HEADERS, json=body)
        assert first.status_code == 200, first.text
        if protocol == 'responses':
            result = events(first)[-1]['response'] if stream else first.json()
            assert result['output'][0]['type'] == 'function_call', first.text
            call = result['output'][0]
            second_body = {'previous_response_id': result['id'], 'tools': [TOOL], 'stream': stream,
                           'input': [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'actual fixture result'}]}
        else:
            if stream:
                frames = events(first)
                call = next(frame['content_block'] for frame in frames if frame['type'] == 'content_block_start')
                call['input'] = json.loads(next(frame['delta']['partial_json'] for frame in frames if frame['type'] == 'content_block_delta'))
            else:
                call = first.json()['content'][0]
            second_body = {'tools': [ATOOL], 'stream': stream, 'messages': [*body['messages'], {'role': 'assistant', 'content': [call]},
                           {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call['id'], 'content': 'actual fixture result'}]}]}
        second = client.post('/v1/' + protocol, headers=HEADERS, json=second_body)
        assert second.status_code == 200 and 'Task complete' in second.text, second.text
        assert len(count) == 2
        assert '<function_calls>' not in first.text and 'actual fixture result' in count[1]
