import json
import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app
from wb2api.reasoning import effort_hint, GATEWAY_PROMPT

H = {'Authorization': 'Bearer effort-client'}


@pytest.fixture
def session(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'effort-admin', 'api_key': 'effort-client'})
    with TestClient(app) as c:
        app.state.store.add_account(label='fixture', id_token='fixture', expires_at=9999999999)
        yield c


@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
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
    assert observed == [GATEWAY_PROMPT + effort_hint(effort) + 'question']
    assert 'not private internal deliberations' in observed[0]
    assert '[DONE]' in r.text if stream else r.json()['choices'][0]['message']['content'] == 'answer'


def test_omitted_effort_leaves_prompt_unchanged(session):
    seen = []
    async def upstream(account, prompt, protocol):
        seen.append(prompt)
        yield 'answer'
    session.app.state.wabi.stream_chat = upstream
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'plain'}]})
    assert r.status_code == 200 and seen == [GATEWAY_PROMPT + 'plain']
    assert 'x-wb2api-reasoning-mode' not in r.headers


@pytest.mark.parametrize('value', ['unsupported', 123, {}, ' HIGH '])
def test_invalid_effort_rejected(session, value):
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'question'}], 'reasoning_effort': value})
    assert r.status_code == 400


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


@pytest.mark.parametrize('stream', [False, True])
def test_standard_client_fields_accepted_with_disclosure(session, stream):
    async def upstream(account, prompt, protocol):
        assert GATEWAY_PROMPT in prompt
        yield 'compatible'
    session.app.state.wabi.stream_chat = upstream
    payload = {'messages': [{'role': 'user', 'content': 'question'}], 'stream': stream,
               'reasoning_effort': 'max', 'temperature': 0.7, 'top_p': 1,
               'max_tokens': 1024, 'max_completion_tokens': 1024, 'n': 1,
               'frequency_penalty': 0, 'presence_penalty': 0, 'seed': 12,
               'user': 'test-client', 'stream_options': {'include_usage': True}}
    r = session.post('/v1/chat/completions', headers=H, json=payload)
    assert r.status_code == 200
    assert r.headers['x-wb2api-reasoning-effort'] == 'max'
    assert set(r.headers['x-wb2api-ignored-parameters'].split(',')) == {
        'temperature', 'top_p', 'user',
        'frequency_penalty', 'presence_penalty', 'seed', 'stream_options'}
    assert 'compatible' in r.text and 'reasoning_content' not in r.text


@pytest.mark.parametrize('params', [{'temperature': 3}, {'top_p': -1}, {'max_tokens': 0}, {'n': 2}])
def test_client_fields_invalid_still_rejected(session, params):
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': 'question'}], **params})
    assert r.status_code == 400


@pytest.mark.parametrize('stream', [False, True])
def test_byok_text_parts_store_and_null_history(session, stream):
    async def upstream(account, prompt, protocol):
        assert '[user]\nhello' in prompt and '[user]\nnext' in prompt
        assert '[assistant]' not in prompt
        for part in ('我是 W', 'a', 'bi。'):
            yield part
    session.app.state.wabi.stream_chat = upstream
    r = session.post('/v1/chat/completions', headers=H, json={
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}], 'name': 'user'},
                     {'role': 'assistant', 'content': None}, {'role': 'user', 'content': 'next'}],
        'store': False, 'metadata': {}, 'reasoning': {'effort': 'max'},
        'stop': [], 'response_format': {'type': 'text'}, 'stream': stream})
    assert r.status_code == 200
    assert 'store' in r.headers['x-wb2api-ignored-parameters']
    assert r.headers['x-wb2api-reasoning-effort'] == 'max'
    assert 'wabi' not in r.text.lower().replace('"model":"wabi"', '') if not stream else '通用助手' in r.text
    if not stream:
        assert r.json()['choices'][0]['message']['content'] == '我是 通用助手。'


def test_nontext_parts_not_silently_dropped(session):
    r = session.post('/v1/chat/completions', headers=H, json={'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image'}}]}]})
    assert r.status_code == 400


def test_brand_filter_split_points():
    import asyncio
    from wb2api.reasoning import neutral_text
    async def run():
        for split in range(1, 5):
            async def source():
                yield 'WABI'[:split]
                yield 'WABI'[split:] + ' window'
            assert ''.join([s async for s in neutral_text(source())]) == '通用助手 window'
    asyncio.run(run())
