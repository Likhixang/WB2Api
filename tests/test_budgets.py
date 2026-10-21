import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'budget-admin', 'api_key': 'budget-client'})
    with TestClient(app) as c:
        app.state.store.add_account(label='fixture', id_token='fixture', expires_at=9999999999)
        yield c


@pytest.mark.parametrize('protocol', ['chat/completions', 'responses', 'messages'])
def test_large_input_no_old_limits(client, protocol):
    text = ' hello' * 220000  # > 200k characters and > 1 MiB, well below token budget
    seen = []
    async def upstream(a, prompt, p):
        seen.append(prompt)
        yield 'ok'
    client.app.state.wabi.stream_chat = upstream
    payload = {'input': text} if protocol == 'responses' else {'messages': [{'role': 'user', 'content': text}]}
    r = client.post('/v1/' + protocol, headers={'Authorization': 'Bearer budget-client'}, json=payload)
    assert r.status_code == 200, r.text[:300]
    assert len(seen) == 1 and text in seen[0]
    assert r.headers['x-wb2api-token-count-mode'] == 'estimated-o200k_base'


@pytest.mark.parametrize('protocol', ['chat/completions', 'responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_output_limit_protocol_finish(client, protocol, stream):
    async def upstream(*args):
        yield ' hello' * 40
    client.app.state.wabi.stream_chat = upstream
    payload = {'input': 'hi', 'max_output_tokens': 5} if protocol == 'responses' else {
        'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 5}
    payload['stream'] = stream
    r = client.post('/v1/' + protocol, headers={'Authorization': 'Bearer budget-client'}, json=payload)
    assert r.status_code == 200, r.text
    assert ' hello' * 6 not in r.text
    if protocol == 'responses':
        assert 'incomplete' in r.text and 'max_output_tokens' in r.text
    elif protocol == 'messages':
        assert 'max_tokens' in r.text
    else:
        assert 'length' in r.text


def test_budget_overflow_does_not_call_upstream(client, monkeypatch):
    import wb2api.budgets as budgets
    monkeypatch.setattr(budgets, 'INPUT_TOKENS', 4)
    async def upstream(*args):
        raise AssertionError('must reject before upstream')
        yield ''
    client.app.state.wabi.stream_chat = upstream
    r = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer budget-client'}, json={'messages': [{'role': 'user', 'content': 'hello'}]})
    assert r.status_code == 400 and r.json()['error']['code'] == 'context_length_exceeded'


def test_output_budget_unit_and_unicode():
    from wb2api.budgets import count_tokens, OutputBudget, check_input, BudgetError
    for limit in (1, 3, 7, 20):
        b = OutputBudget(limit)
        result = b.push('你好🌏 test' * 30, final=True)
        assert count_tokens(result) <= limit
        assert b.truncated
    with pytest.raises(BudgetError):
        check_input('hi', 128001)


def test_large_responses_continuation(client):
    seen = []
    async def upstream(a, prompt, protocol):
        seen.append(prompt)
        yield 'ok'
    client.app.state.wabi.stream_chat = upstream
    text = ' hello' * 220000
    h = {'Authorization': 'Bearer budget-client'}
    first = client.post('/v1/responses', headers=h, json={'input': text})
    assert first.status_code == 200
    second = client.post('/v1/responses', headers=h, json={'previous_response_id': first.json()['id'], 'input': 'continue'})
    assert second.status_code == 200 and len(seen) == 2
    assert text in seen[1]


@pytest.mark.parametrize('stream', [False, True])
def test_partial_tool_never_emitted_and_upstream_closed(client, stream):
    import re
    closed = []
    async def upstream(a, prompt, p):
        try:
            marker = re.search(r'<Function_[a-f0-9]+_Start/>', prompt).group()
            yield marker + '<function_calls><function_call><tool>lookup</tool><args_json>{"q":"' + ' hello' * 100 + '"}</args_json></function_call></function_calls>' + marker.replace('_Start/>', '_End/>')
        finally:
            closed.append(True)
    client.app.state.wabi.stream_chat = upstream
    r = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer budget-client'}, json={
        'messages': [{'role': 'user', 'content': 'lookup'}], 'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {}}}],
        'tool_choice': 'required', 'max_completion_tokens': 2, 'stream': stream})
    assert 'max_output_tokens_exceeded' in r.text and '<Function_' not in r.text
    assert '"tool_calls":' not in r.text and '[DONE]' not in r.text and closed == [True]
    assert r.status_code == (200 if stream else 502)


def test_actual_input_budget_boundary():
    from wb2api.budgets import check_input, BudgetError
    assert check_input(' hello' * 922000) == (922000, 128000)
    with pytest.raises(BudgetError):
        check_input(' hello' * 922001)


def test_many_messages_and_large_historical_arguments():
    from wb2api.main import Completion, CallFunction
    assert len(Completion(messages=[{'role': 'user', 'content': 'hi'}] * 201).messages) == 201
    assert len(CallFunction(name='x', arguments='a' * 300000).arguments) == 300000


def test_admin_retains_small_transport_guard(client):
    r = client.post('/admin/api/accounts', content=b' ' * (1024 * 1024 + 1), headers={'Authorization': 'Bearer budget-admin', 'content-type': 'application/json'})
    assert r.status_code == 413
