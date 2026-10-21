import json

import pytest
from fastapi.testclient import TestClient

from wb2api.main import create_app
from wb2api.tool_compat import ToolAdapter, ToolProtocolError

HEADERS = {'Authorization': 'Bearer inference-test-key'}
TOOLS = [{'type': 'function', 'function': {'name': 'client_add_numbers', 'parameters': {
    'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
    'required': ['a', 'b'], 'additionalProperties': False}}}]


def wrapped(adapter, args='{"a":17,"b":29}', name='client_add_numbers'):
    return (adapter.start + '<function_calls><function_call><tool>' + name + '</tool><args_json>'
            + args + '</args_json></function_call></function_calls>' + adapter.end)


def test_strict_complete_output_and_schema_validation():
    adapter = ToolAdapter(TOOLS, 'required', False)
    message, finish = adapter.parse(wrapped(adapter))
    assert message['content'] is None and finish == 'tool_calls'
    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'a': 17, 'b': 29}
    for output in [wrapped(adapter, '{"a":"17","b":29}'), wrapped(adapter, name='unknown'),
                   wrapped(adapter, '{"a":17,"a":18,"b":29}'), wrapped(adapter) + ' extra',
                   wrapped(adapter).replace(adapter.start, '<Function_wrong_Start/>'), 'I cannot call']:
        with pytest.raises(ToolProtocolError):
            adapter.parse(output)


@pytest.mark.parametrize('stream', [False, True])
def test_client_tool_round_trip_and_openai_formats(tmp_path, stream):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-test-key', 'api_key': 'inference-test-key'})
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', refresh_token='fixture', firebase_api_key='fixture')

        async def upstream(account, prompt, protocol):
            import re
            start = re.search(r'<Function_[a-f0-9]+_Start/>', prompt).group()
            end = start.replace('_Start/>', '_End/>')
            if '<tool_result>' in prompt:
                assert '"tool_call_id":' in prompt and '46' in prompt
                yield '46'
            else:
                output = (start + '<function_calls><function_call><tool>client_add_numbers</tool>'
                          '<args_json>{"a":17,"b":29}</args_json></function_call></function_calls>' + end)
                for offset in range(0, len(output), 3):
                    yield output[offset:offset + 3]

        app.state.wabi.stream_chat = upstream
        history = [{'role': 'user', 'content': 'Add 17 and 29 using the client tool.'}]
        response = client.post('/v1/chat/completions', headers=HEADERS, json={
            'messages': history, 'tools': TOOLS, 'tool_choice': 'required', 'stream': stream})
        assert response.status_code == 200, response.text
        if stream:
            assert response.headers['x-wb2api-tool-stream'] == 'buffered'
            assert '<function_call' not in response.text and '<Function_' not in response.text
            frames = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
            assert frames[-1] == '[DONE]'
            chunks = [json.loads(x) for x in frames[:-1]]
            assert chunks[-1]['choices'][0]['finish_reason'] == 'tool_calls'
            call = next(c['choices'][0]['delta']['tool_calls'][0] for c in chunks if 'tool_calls' in c['choices'][0]['delta'])
            assert call.pop('index') == 0
            message = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
        else:
            choice = response.json()['choices'][0]
            assert choice['finish_reason'] == 'tool_calls'
            message = choice['message']
        # Execute the returned function on this test client, never inside the API.
        args = json.loads(message['tool_calls'][0]['function']['arguments'])
        result = str(args['a'] + args['b'])
        assert result == '46'
        history += [message, {'role': 'tool', 'tool_call_id': message['tool_calls'][0]['id'], 'content': result}]
        response = client.post('/v1/chat/completions', headers=HEADERS, json={
            'messages': history, 'tools': TOOLS, 'tool_choice': 'auto'})
        assert response.status_code == 200, response.text
        assert response.json()['choices'][0]['message']['content'] == '46'
        assert response.json()['choices'][0]['finish_reason'] == 'stop'
        # A final-answer request may omit tools entirely while preserving linkage.
        response = client.post('/v1/chat/completions', headers=HEADERS, json={'messages': history})
        assert response.status_code == 200, response.text
        assert response.json()['choices'][0]['message']['content'] == '46'
        response = client.post('/v1/chat/completions', headers=HEADERS, json={
            'messages': history, 'tools': TOOLS, 'tool_choice': 'none', 'stream': True})
        assert response.status_code == 200
        frames = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
        assert frames[-1] == '[DONE]'
        chunks = [json.loads(frame) for frame in frames[:-1]]
        assert ''.join(c['choices'][0]['delta'].get('content', '') for c in chunks) == '46'
        assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'


@pytest.mark.parametrize('choice', ['auto', 'none'])
def test_plain_text_and_none_policy(choice):
    adapter = ToolAdapter(TOOLS, choice)
    assert adapter.parse('plain answer') == ({'role': 'assistant', 'content': 'plain answer'}, 'stop')
    if choice == 'none':
        with pytest.raises(ToolProtocolError):
            adapter.parse(wrapped(adapter))


def test_forced_choice_parallel_limits_and_unique_markers():
    choice = {'type': 'function', 'function': {'name': 'client_add_numbers'}}
    adapter = ToolAdapter(TOOLS, choice, False)
    assert adapter.parse(wrapped(adapter))[1] == 'tool_calls'
    assert ToolAdapter(TOOLS).start != adapter.start
    output = wrapped(adapter)
    duplicate = output.replace('</function_calls>', output.split('<function_calls>')[1].split('</function_calls>')[0] + '</function_calls>')
    with pytest.raises(ToolProtocolError):
        adapter.parse(duplicate)
    parallel = ToolAdapter(TOOLS)
    output = duplicate.replace(adapter.start, parallel.start).replace(adapter.end, parallel.end)
    calls = parallel.parse(output)[0]['tool_calls']
    assert len(calls) == 2 and calls[0]['id'] != calls[1]['id']


def test_forced_choice_rejects_other_allowed_tool_and_escaped_json_roundtrips():
    other = {'type': 'function', 'function': {'name': 'other', 'parameters': {'type': 'object'}}}
    adapter = ToolAdapter(TOOLS + [other], {'type': 'function', 'function': {'name': 'client_add_numbers'}})
    with pytest.raises(ToolProtocolError):
        adapter.parse(wrapped(adapter, '{}', 'other'))
    adapter = ToolAdapter([other])
    message, finish = adapter.parse(wrapped(adapter, '{"text":"a &amp; b &lt; c"}', 'other'))
    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'text': 'a & b < c'}
    assert finish == 'tool_calls'


def test_local_schema_ref_and_non_finite_numeric_arguments():
    tools = [{'type': 'function', 'function': {'name': 'x', 'parameters': {
        'type': 'object', '$defs': {'integer': {'type': 'integer'}},
        'properties': {'n': {'$ref': '#/$defs/integer'}}, 'required': ['n']}}}]
    adapter = ToolAdapter(tools)
    assert adapter.parse(wrapped(adapter, '{"n":3}', 'x'))[1] == 'tool_calls'
    for args in ['{"n":"3"}', '{"n":NaN}', '{"n":1e999}']:
        with pytest.raises(ToolProtocolError):
            adapter.parse(wrapped(adapter, args, 'x'))


@pytest.mark.parametrize('output', [
    '```xml\n{wrapped}\n```', '{wrapped} trailing', '<function_calls/>',
    '{start}<function_calls></function_calls>{end}',
    '{start}<!DOCTYPE x [<!ENTITY x "no">]><function_calls/>{end}',
    '{start}<function_calls><function_call><tool>client_add_numbers</tool><args_json>{{"a":NaN,"b":1}}</args_json></function_call></function_calls>{end}',
])
def test_malformed_envelopes_fail_closed(output):
    adapter = ToolAdapter(TOOLS, 'required')
    output = output.replace('{wrapped}', wrapped(adapter)).replace('{start}', adapter.start).replace('{end}', adapter.end)
    with pytest.raises(ToolProtocolError):
        adapter.parse(output)


@pytest.mark.parametrize('tools,choice', [
    (TOOLS + TOOLS, 'auto'), (TOOLS, 'invalid'), ([], 'required'),
    (TOOLS, {'type': 'function', 'function': {'name': 'unknown'}}),
    ([{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object', '$ref': 'https://example.com/schema'}}}], 'auto'),
    ([{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object', 'properties': 123}}}], 'auto'),
])
def test_invalid_tool_definitions_fail_before_upstream(tools, choice):
    with pytest.raises(ToolProtocolError) as exc:
        ToolAdapter(tools, choice)
    assert exc.value.status == 400


@pytest.mark.parametrize('stream', [True, False])
def test_bad_model_call_is_http_error_and_never_leaks_xml(tmp_path, stream):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-test-key', 'api_key': 'inference-test-key'})
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', refresh_token='fixture', firebase_api_key='fixture')
        async def bad_upstream(*args):
            yield '<function_calls>secret invalid protocol</function_calls>'
        app.state.wabi.stream_chat = bad_upstream
        response = client.post('/v1/chat/completions', headers=HEADERS, json={
            'messages': [{'role': 'user', 'content': 'hi'}], 'tools': TOOLS, 'stream': stream})
        if stream:
            assert response.status_code == 200
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            assert frames[-1]['error']['code'] == 'invalid_tool_response'
            assert '[DONE]' not in response.text and '"finish_reason": "stop"' not in response.text
        else:
            assert response.status_code == 502
            assert response.json()['error']['code'] == 'invalid_tool_response'
        assert 'secret' not in response.text and '<function_calls>' not in response.text


@pytest.mark.parametrize('case', ['orphan', 'missing', 'duplicate_result', 'duplicate_call', 'invalid_args', 'assistant_without_content'])
def test_history_validation_prevents_invalid_round_trips(tmp_path, case):
    call = {'id': 'call_fixture', 'type': 'function', 'function': {'name': 'client_add_numbers', 'arguments': '{"a":17,"b":29}'}}
    history = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': None, 'tool_calls': [call]}]
    result = {'role': 'tool', 'tool_call_id': 'call_fixture', 'content': '46'}
    if case == 'orphan':
        history = [result]
    elif case == 'duplicate_result':
        history += [result, result]
    elif case == 'duplicate_call':
        history[1]['tool_calls'].append(call)
        history.append(result)
    elif case == 'invalid_args':
        call['function']['arguments'] = '{"a":'
        history.append(result)
    elif case == 'assistant_without_content':
        history = [{'role': 'assistant', 'content': None}, {'role': 'user', 'content': 'hi'}]
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-test-key', 'api_key': 'inference-test-key'})
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', headers=HEADERS, json={'messages': history, 'tools': TOOLS})
        if case == 'assistant_without_content':
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'no_account'
        else:
            assert response.status_code in (400, 422), response.text


def test_historical_tool_does_not_need_to_remain_in_current_tools():
    from wb2api.main import Message
    current = [{'type': 'function', 'function': {'name': 'new_tool', 'parameters': {'type': 'object'}}}]
    messages = [
        Message(role='user', content='previous task'),
        Message.model_validate({'role': 'assistant', 'tool_calls': [{'id': 'call_old', 'type': 'function',
            'function': {'name': 'old_tool', 'arguments': '{"old_parameter":42}'}}]}),
        Message(role='tool', tool_call_id='call_old', content='previous result'),
        Message(role='user', content='now use the newly available tool'),
    ]
    prompt = ToolAdapter(current).history_prompt(messages)
    assert 'old_tool' in prompt and 'previous result' in prompt
