import pytest
from fastapi.testclient import TestClient
from wb2api.main import Completion, create_app
from wb2api.tool_compat import ToolAdapter, ToolProtocolError

HEADERS = {'Authorization': 'Bearer inference-test-key'}


def tools(count):
    return [{'type': 'function', 'function': {'name': f'fn_{i}', 'parameters': {}}} for i in range(count)]


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-test-key', 'api_key': 'inference-test-key'})
    with TestClient(app) as session:
        yield session


@pytest.mark.parametrize('count', [65, 128])
def test_tool_count_acceptance_without_upstream(client, count):
    response = client.post('/v1/chat/completions', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'hi'}], 'tools': tools(count)})
    assert response.status_code == 503, response.text  # no account, never a network call
    assert len(ToolAdapter(tools(count)).tools) == count


def test_tool_limit_returns_safe_openai_error(client):
    response = client.post('/v1/chat/completions', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'secret-token'}], 'tools': tools(129)})
    assert response.status_code == 400
    error = response.json()['error']
    assert error['type'] == 'invalid_request_error'
    assert error['code'] == 'invalid_request'
    assert '128' in error['message'] and 'body.tools' in error['message']
    assert 'secret-token' not in response.text
    with pytest.raises(ToolProtocolError):
        ToolAdapter(tools(129))


def test_metadata_is_not_prompt_content():
    body = Completion.model_validate({'messages': [
        {'role': 'assistant', 'content': 'answer', 'tool_calls': [], 'name': None,
         'reasoning_content': 'private-reasoning', 'refusal': None, 'annotations': []},
        {'role': 'user', 'content': 'next', 'metadata': {'secret': 'private-metadata'}}],
        'parallel_tool_calls': None, 'client_metadata': {'secret': 'private-metadata'}})
    assert body.parallel_tool_calls is True
    assert body.messages[0].tool_calls is None
    prompt = ToolAdapter().history_prompt(body.messages)
    assert prompt == '[assistant]\nanswer\n\n[user]\nnext'
    assert 'client_metadata' in body.ignored_parameters()
    assert 'messages.reasoning_content' in body.ignored_parameters()


@pytest.mark.parametrize('value', [None, []])
def test_null_empty_tools(client, value):
    response = client.post('/v1/chat/completions', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'hi'}], 'tools': value, 'parallel_tool_calls': None})
    assert response.status_code == 503, response.text


@pytest.mark.parametrize('payload,code', [
    ({'response_format': {'type': 'json_object'}}, 'unsupported_response_format'),
    ({'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'secret-url'}}]}]}, 'unsupported_content'),
])
def test_known_unsupported_capabilities(client, payload, code):
    response = client.post('/v1/chat/completions', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'hi'}], **payload})
    assert response.status_code == 400
    assert response.json()['error']['code'] == code
    assert 'secret-url' not in response.text


def test_control_plane_remains_strict(client):
    response = client.post('/admin/api/keys', headers={'Authorization': 'Bearer management-test-key'},
                           json={'name': 'fixture', 'unknown': 'private-token'})
    assert response.status_code == 422
    assert 'private-token' not in response.text


@pytest.mark.parametrize('value', [False, {}, '', 0])
def test_falsey_malformed_tools_not_silently_ignored(value):
    with pytest.raises(ToolProtocolError):
        ToolAdapter(value)


@pytest.mark.parametrize('count', [128, 129])
def test_historical_call_limit(client, count):
    calls = [{'id': f'call_{i}', 'type': 'function', 'function': {'name': 'old_tool', 'arguments': '{}'},
              'index': i, 'annotations': []} for i in range(count)]
    history = [{'role': 'assistant', 'content': None, 'tool_calls': calls}]
    history += [{'role': 'tool', 'content': 'done', 'tool_call_id': f'call_{i}'} for i in range(count)]
    response = client.post('/v1/chat/completions', headers=HEADERS, json={'messages': history})
    assert response.status_code == (503 if count == 128 else 400), response.text


@pytest.mark.parametrize('count', [128, 129])
def test_generated_call_limit(count):
    adapter = ToolAdapter(tools(1))
    call = '<function_call><tool>fn_0</tool><args_json>{}</args_json></function_call>'
    output = adapter.start + '<function_calls>' + call * count + '</function_calls>' + adapter.end
    if count == 128:
        assert len(adapter.parse(output)[0]['tool_calls']) == 128
    else:
        with pytest.raises(ToolProtocolError):
            adapter.parse(output)


@pytest.mark.parametrize('bad', [
    [{'type': 'function', 'name': 'flat_responses_tool', 'parameters': {}}],
    [{'type': 'function', 'function': {'name': 'x', 'parameters': {'properties': 123}}}],
    tools(1) * 2,
])
def test_malformed_tools_remain_rejected(client, bad):
    response = client.post('/v1/chat/completions', headers=HEADERS, json={
        'messages': [{'role': 'user', 'content': 'hi'}], 'tools': bad})
    assert response.status_code == 400, response.text
