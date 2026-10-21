import json
import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app
from wb2api.reasoning import effort_hint

H = {'Authorization': 'Bearer effort-client'}


@pytest.fixture
def session(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'effort-admin', 'api_key': 'effort-client'})
    with TestClient(app) as c:
        app.state.store.add_account(label='fixture', id_token='fixture', expires_at=9999999999)
        yield c


@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'])
@pytest.mark.parametrize('stream', [False, True])
def test_effort_explicit_prompt_and_header(session, effort, stream):
    observed = []
    async def upstream(account, prompt, protocol):
        observed.append(prompt)
        yield 'answer'
    session.app.state.wabi.stream_chat = upstream
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'question'}], 'reasoning_effort': effort, 'stream': stream})
    assert r.status_code == 200
    assert r.headers['x-wb2api-reasoning-mode'] == 'prompt'
    assert r.headers['x-wb2api-reasoning-effort'] == effort
    assert observed == [effort_hint(effort) + 'question']
    assert 'not private internal deliberations' in observed[0]
    assert '[DONE]' in r.text if stream else r.json()['choices'][0]['message']['content'] == 'answer'


def test_omitted_effort_leaves_prompt_unchanged(session):
    seen = []
    async def upstream(account, prompt, protocol):
        seen.append(prompt)
        yield 'answer'
    session.app.state.wabi.stream_chat = upstream
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'plain'}]})
    assert r.status_code == 200 and seen == ['plain']
    assert 'x-wb2api-reasoning-mode' not in r.headers


@pytest.mark.parametrize('value', ['unsupported', 123, {}, ' HIGH '])
def test_invalid_effort_rejected(session, value):
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'question'}], 'reasoning_effort': value})
    assert r.status_code == 422


def test_effort_with_tool_format_still_strict(session):
    import re
    async def upstream(account, prompt, protocol):
        assert 'Response effort preference: high' in prompt
        marker = re.search(r'<Function_[a-f0-9]+_Start/>', prompt).group()
        yield marker + '<function_calls><function_call><tool>lookup</tool><args_json>{}</args_json></function_call></function_calls>' + marker.replace('_Start/>', '_End/>')
    session.app.state.wabi.stream_chat = upstream
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'question'}], 'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}], 'tool_choice': 'required', 'reasoning_effort': 'high', 'stream': True})
    assert r.status_code == 200 and r.headers['x-wb2api-reasoning-mode'] == 'prompt'
    assert '<Function_' not in r.text and '"tool_calls"' in r.text


def test_image_effort_not_silently_ignored(session):
    r = session.post('/v1/chat/completions', headers=H, json={'model': 'wabi-image', 'messages': [{'role': 'user', 'content': 'draw'}], 'reasoning_effort': 'high'})
    assert r.status_code == 400 and r.json()['error']['code'] == 'unsupported_media_reasoning'
