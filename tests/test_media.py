"""Synthetic fixtures based on verified media event shapes, never live generation."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from wb2api.main import create_app
from wb2api.media import ImageTurn, Media, image_url
from wb2api.wabi import UpstreamError, Wabi

URL = 'https://d34c7q5wjf4spy.cloudfront.net/fixture/image.webp'
IH = {'Authorization': 'Bearer inference-media-test-key'}
AH = {'Authorization': 'Bearer management-media-test-key'}


def idle(revision=1, **changes):
    return {'type': 'turn_state', 'thread_id': 'thread', 'revision': revision,
            'active_turn': None, 'queued_turns': [], 'has_background_work': False, **changes}


def finished(turn='target', identity='image', revision=2, blocks=None, **changes):
    return {'type': 'message_finished', 'thread_id': 'thread', 'message': {
        'id': identity, 'revision': revision, 'thread_id': 'thread', 'turn_id': turn,
        'role': 'assistant', 'status': 'completed', 'attachments': [],
        'is_final_message': False,
        'blocks': blocks if blocks is not None else [
            {'type': 'tool_use', 'id': 'generate', 'name': 'generate_image'},
            {'type': 'tool_result', 'tool_use_id': 'generate', 'phase': 'succeeded',
             'content': {'results': [{'image_url': URL}]}}], **changes}}


def opened():
    state = ImageTurn('thread')
    assert not state.consume(finished('opening', 'opening', blocks=[{'type': 'text', 'text': 'welcome'}]))
    assert state.consume(idle())
    state.accept('target')
    return state


def test_media_result_needs_target_finished_and_new_idle_not_final_flag():
    state = opened()
    assert not state.consume(idle())  # replay of opening idle
    assert not state.consume(finished('other'))
    assert not state.consume(finished(identity='intro', blocks=[{'type': 'text', 'text': 'Generating...'}]))
    assert not state.consume(finished())
    assert not state.consume(finished())  # duplicate revision
    assert not state.consume(finished(revision=1))  # stale revision
    assert not state.consume(idle(2, has_background_work=True))
    assert not state.consume(idle(3, active_turn={'turn_id': 'target'}))
    assert not state.consume(idle(4, queued_turns=[{'turn_id': 'queued'}]))
    assert state.consume(idle(5))
    assert state.result() == [URL]
    assert len(state.messages) == 2


def test_media_correlation_conflicts_and_incomplete_states_ignored():
    state = ImageTurn('thread')
    assert not state.consume(idle())  # idle before opening completion not enough
    assert not state.consume(finished('opening', blocks=[]))
    assert not state.consume(idle())  # stale revision cannot complete opening
    assert state.consume(idle(2))
    state.accept('target')
    other_thread = finished()
    other_thread['message']['thread_id'] = 'other'
    assert not state.consume(other_thread)
    conflict = finished()
    conflict['turn_id'] = 'other'
    assert not state.consume(conflict)
    no_turn = finished()
    del no_turn['message']['turn_id']
    assert not state.consume(no_turn)
    assert not state.consume(idle(3))  # no target completed message
    assert not state.consume(finished())
    assert not state.consume({'type': 'turn_state', 'thread_id': 'thread', 'revision': 4})
    assert state.consume(idle(5))
    assert state.result() == [URL]


@pytest.mark.parametrize('blocks', [
    [], [{'type': 'text', 'text': 'https://example.com/not-a-real-image.png'}],
    [{'type': 'tool_result', 'tool_use_id': 'generate', 'phase': 'succeeded',
      'content': {'results': [{'image_url': URL}]}}],
    [{'type': 'tool_use', 'name': 'generate_image', 'id': 'generate'},
     {'type': 'tool_result', 'tool_use_id': 'generate', 'phase': 'running',
      'content': {'results': [{'image_url': URL}]}}],
    [{'type': 'tool_use', 'name': 'some_other_tool', 'id': 'generate'},
     {'type': 'tool_result', 'tool_use_id': 'generate', 'phase': 'succeeded',
      'content': {'results': [{'image_url': URL}]}}],
])
def test_text_refusal_attachments_and_non_success_results_never_become_image(blocks):
    state = opened()
    state.consume(finished(blocks=blocks, attachments=[{'url': URL}]))
    assert state.consume(idle(2))
    with pytest.raises(UpstreamError) as exc:
        state.result()
    assert exc.value.code == 'image_not_generated'


def test_later_revision_replaces_old_result_and_multiple_images_fail_closed():
    state = opened()
    state.consume(finished())
    state.consume(finished(revision=3, blocks=[]))
    with pytest.raises(UpstreamError, match='without a successful image'):
        state.result()
    message = finished(revision=4)
    message['message']['blocks'][1]['content']['results'].append({'image_url': URL + '?second'})
    with pytest.raises(UpstreamError) as exc:
        state.consume(message)
    assert exc.value.code == 'unexpected_image_count'


def test_media_state_bounds_results_and_rejects_conflicting_active_turn():
    state = opened()
    state.max_result_bytes = 1
    with pytest.raises(UpstreamError) as exc:
        state.consume(finished())
    assert exc.value.code == 'upstream_response_too_large'
    state = opened()
    with pytest.raises(UpstreamError) as exc:
        state.consume(idle(2, active_turn={'turn_id': 'foreign'}))
    assert exc.value.code == 'media_turn_conflict'


@pytest.mark.parametrize('kind', ['turn_error', 'message_error'])
def test_target_errors_do_not_echo_raw_upstream_text(kind):
    state = opened()
    with pytest.raises(UpstreamError) as exc:
        state.consume({'type': kind, 'thread_id': 'thread', 'turn_id': 'target', 'error': 'NEVER_ECHO'})
    assert exc.value.code == 'media_generation_failed' and 'NEVER_ECHO' not in str(exc.value)


@pytest.mark.parametrize('url', [
    'http://d34c7q5wjf4spy.cloudfront.net/image.png', 'https://127.0.0.1/x',
    'https://169.254.169.254/latest/meta-data', 'https://example.com/image.png',
    'https://d34c7q5wjf4spy.cloudfront.net.evil.example/x',
    'https://user:password@d34c7q5wjf4spy.cloudfront.net/x',
    'https://d34c7q5wjf4spy.cloudfront.net:8443/x',
    'https://d34c7q5wjf4spy.cloudfront.net/x#fragment',
    'https://d34c7q5wjf4spy.cloudfront.net/x\nsecret', 'file:///etc/passwd',
    'https://d34c7q5wjf4spy.cloudfront.net/x>markdown',
    'https://d34c7q5wjf4spy.cloudfront.net/x\\path', None,
])
def test_artifact_origin_allowlist(url):
    with pytest.raises(UpstreamError) as exc:
        image_url(url)
    assert exc.value.code == 'invalid_media_url'
    assert image_url(URL) == URL


class FixtureStore:
    def get_account(self, account):
        return {'id_token': 'fixture-token', 'expires_at': time.time() + 3600, 'device_id': 'fixture-device'}


class EventStream(httpx.AsyncByteStream):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def __aiter__(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield ('data: ' + json.dumps(event) + '\n\n').encode()

    async def aclose(self):
        self.closed = True


class FixtureTransport:
    def __init__(self, *, refusal=False, disconnect=False, fail_at=None, hang=False, invalid_json=False):
        self.stream = EventStream()
        self.refusal, self.disconnect, self.fail_at = refusal, disconnect, fail_at
        self.hang, self.invalid_json = hang, invalid_json
        self.posts = []
        self.sse_connected = False

    async def __call__(self, request):
        path = request.url.path
        if path == '/api/v1/chat/events':
            self.sse_connected = True
            if self.fail_at == 'sse':
                return httpx.Response(401, json={'secret': 'NEVER_ECHO'})
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=self.stream)
        assert request.method == 'POST'  # no media downloads or invented endpoints
        assert self.sse_connected  # SSE must be connected before thread creation
        self.posts.append((path, json.loads(request.content)))
        if self.fail_at == 'post':
            return httpx.Response(429, json={'error': 'NEVER_ECHO'})
        if path == '/api/v1/threads':
            await self.stream.queue.put(finished('opening', 'opening', blocks=[{'type': 'text', 'text': 'Welcome'}]))
            await self.stream.queue.put(idle())
            return httpx.Response(200, json={'thread': {'id': 'thread'}})
        assert path == '/api/v1/threads/thread/messages'
        assert self.stream.queue.empty()  # opening completion AND idle were consumed before POST
        if self.disconnect:
            await self.stream.queue.put(None)
        elif not self.hang:
            await self.stream.queue.put(idle())  # stale opening state
            await self.stream.queue.put(finished('other', 'unrelated'))
            await self.stream.queue.put(finished(identity='intro', blocks=[{'type': 'text', 'text': 'Generating'}]))
            if not self.refusal:
                await self.stream.queue.put(finished())
                await self.stream.queue.put(finished())
            await self.stream.queue.put(finished(identity='empty-final', blocks=[]))
            await self.stream.queue.put(idle(2, has_background_work=True))
            await self.stream.queue.put(idle(3))
        return httpx.Response(200, json={} if self.invalid_json else {'turn_id': 'target'})


def run_scenario(**options):
    async def run():
        w = Wabi(FixtureStore(), {'request_timeout': 0.2, 'queue_size': 64})
        await w.client.aclose()
        transport = FixtureTransport(**options)
        w.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            try:
                result = await Media(w).generate_image('account', 'a red paper boat')
                return result, transport
            finally:
                assert not w._readers and not w._locks
                assert transport.stream.closed or options.get('fail_at') == 'sse'
        finally:
            await w.close()
    return asyncio.run(run())


def test_full_verified_media_flow_and_no_retries_or_downloads():
    result, transport = run_scenario()
    assert result == [URL]
    assert len(transport.posts) == 2
    thread, message = transport.posts
    assert thread[1]['title'] == '' and thread[1]['kind'] == 'group'
    assert message[1]['attachment_ids'] == [] and message[1]['source'] == 'typed'
    assert message[1]['page_context'] == {'app_surface': 'chat_only'}
    assert 'a red paper boat' in message[1]['blocks'][0]['text']
    assert 'Do not purchase' in message[1]['blocks'][0]['text']


@pytest.mark.parametrize('options,code,status', [
    ({'refusal': True}, 'image_not_generated', 502),
    ({'disconnect': True}, 'sse_disconnected', 502),
    ({'hang': True}, 'upstream_timeout', 504),
    ({'fail_at': 'sse'}, 'upstream_authentication_failed', 401),
    ({'fail_at': 'post'}, 'upstream_rate_limited', 429),
    ({'invalid_json': True}, 'invalid_upstream_response', 502),
])
def test_media_errors_are_safe_and_release_resources(options, code, status):
    with pytest.raises(UpstreamError) as exc:
        run_scenario(**options)
    assert exc.value.code == code and exc.value.status == status
    assert 'NEVER_ECHO' not in str(exc.value)


def test_task_cancellation_and_same_account_lock_share():
    async def run():
        w = Wabi(FixtureStore(), {'request_timeout': 1})
        await w.client.aclose()
        transport = FixtureTransport(hang=True)
        w.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            async with w._account_lock('account'):
                task = asyncio.create_task(Media(w).generate_image('account', 'boat'))
                await asyncio.sleep(0.01)
                assert not transport.posts
            while len(transport.posts) < 2:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert transport.stream.closed and not w._readers and not w._locks
        finally:
            await w.close()
    asyncio.run(run())


@pytest.mark.parametrize('stream', [False, True])
def test_real_adapter_through_http_fixture(tmp_path, stream):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-media-test-key',
                      'api_key': 'inference-media-test-key', 'request_timeout': 1})
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
        transport = FixtureTransport()

        async def install_transport():
            await app.state.wabi.client.aclose()
            app.state.wabi.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))

        assert client.portal is not None
        client.portal.call(install_transport)
        if stream:
            response = client.post('/v1/chat/completions', headers=IH, json={
                'model': 'wabi-image', 'messages': [{'role': 'user', 'content': 'boat'}], 'stream': True})
            assert response.status_code == 200 and URL in response.text
            assert response.text.endswith('data: [DONE]\n\n')
        else:
            response = client.post('/v1/images/generations', headers=IH, json={'prompt': 'boat'})
            assert response.status_code == 200 and response.json()['data'] == [{'url': URL}]
        assert len(transport.posts) == 2 and transport.stream.closed
        assert not app.state.wabi._locks and not app.state.wabi._readers


def test_media_http_without_account_and_with_revoked_key(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-media-test-key',
                      'api_key': 'inference-media-test-key'})
    with TestClient(app) as client:
        assert client.post('/v1/images/generations', headers=IH, json={'prompt': 'boat'}).status_code == 503
        key = client.post('/admin/api/keys', headers=AH, json={'name': 'media fixture'}).json()
        headers = {'Authorization': 'Bearer ' + key['key']}
        assert client.post('/v1/images/generations', headers=headers, json={'prompt': 'boat'}).status_code == 503
        client.patch('/admin/api/keys/' + key['id'], headers=AH, json={'enabled': False})
        assert client.post('/v1/images/generations', headers=headers, json={'prompt': 'boat'}).status_code == 401


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'management-media-test-key',
                      'api_key': 'inference-media-test-key'})
    with TestClient(app) as client:
        app.state.store.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
        async def media(account, prompt):
            assert account and prompt
            return [URL]
        async def no_text_chat(*args):
            raise AssertionError('Image capability must not route through text chat')
            yield ''
        app.state.media.generate_image = media
        app.state.wabi.stream_chat = no_text_chat
        yield client


def test_three_aliases_and_explicit_unavailable_video(client):
    models = client.get('/v1/models', headers=IH).json()['data']
    assert [m['id'] for m in models] == ['gpt-6.1-sol', 'gpt-image-2.5-flare', 'wan-2.1']
    assert all(m['capability_alias'] and m['available'] for m in models)
    async def video(*args):
        return ['https://d34c7q5wjf4spy.cloudfront.net/fixture/video.mp4']
    client.app.state.video.generate_video = video
    response = client.post('/v1/chat/completions', headers=IH, json={'model': 'wan-2.1', 'messages': [{'role': 'user', 'content': 'boat'}]})
    assert response.status_code == 200 and 'video.mp4' in response.json()['choices'][0]['message']['content']
    response = client.post('/v1/images/generations', headers=IH, json={'model': 'wabi-video', 'prompt': 'boat'})
    assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_media_endpoint'


def test_images_inference_auth_and_openai_url_shape(client):
    for headers in [{}, AH]:
        assert client.post('/v1/images/generations', headers=headers, json={'prompt': 'boat'}).status_code == 401
    response = client.post('/v1/images/generations', headers=IH,
                           json={'model': 'wabi-image', 'prompt': 'boat', 'n': 1, 'response_format': 'url'})
    assert response.status_code == 200
    assert response.json()['data'] == [{'url': URL}]
    assert isinstance(response.json()['created'], int)
    assert 'usage' not in response.json()
    # Media's verified event shapes do not depend on the admin's text mapper.
    client.app.state.store.set_setting('protocol', '{}')
    assert client.post('/v1/images/generations', headers=IH, json={'prompt': 'boat'}).status_code == 200


@pytest.mark.parametrize('extra', [{'n': 2}, {'n': 0}, {'n': True}, {'n': '1'}, {'size': '1536x1536'},
                                  {'quality': 'standard'}, {'response_format': 'b64_json'}, {'prompt': ''},
                                  {'duration': 2}, {'seed': 1}])
def test_unverified_media_controls_fail_before_generation(client, extra):
    async def forbidden(*args):
        raise AssertionError('Invalid controls must not create a paid media request')
    client.app.state.media.generate_image = forbidden
    response = client.post('/v1/images/generations', headers=IH, json={'prompt': 'boat', **extra})
    assert response.status_code == 422


@pytest.mark.parametrize('model', ['wabi', 'default', 'unknown'])
def test_image_endpoint_refuses_text_alias(client, model):
    response = client.post('/v1/images/generations', headers=IH, json={'model': model, 'prompt': 'boat'})
    assert response.status_code == 400 and response.json()['error']['code'] == 'model_not_found'


@pytest.mark.parametrize('stream', [False, True])
def test_image_chat_returns_only_verified_artifact_and_buffered_stream(client, stream):
    response = client.post('/v1/chat/completions', headers=IH, json={
        'model': 'wabi-image', 'messages': [{'role': 'user', 'content': 'boat'}], 'stream': stream})
    assert response.status_code == 200, response.text
    if not stream:
        data = response.json()
        assert data['model'] == 'wabi-image'
        assert URL in data['choices'][0]['message']['content']
        assert 'usage' not in data
    else:
        assert response.headers['x-wb2api-media-stream'] == 'buffered'
        frames = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
        assert frames[-1] == '[DONE]'
        chunks = [json.loads(frame) for frame in frames[:-1]]
        assert all(chunk['model'] == 'wabi-image' for chunk in chunks)
        assert URL in ''.join(chunk['choices'][0]['delta'].get('content', '') for chunk in chunks)
        assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'


@pytest.mark.parametrize('extra', [{'tools': []}, {'tool_choice': 'none'}, {'parallel_tool_calls': False}])
def test_media_empty_tool_controls_are_compatible(client, extra):
    response = client.post('/v1/chat/completions', headers=IH, json={
        'model': 'wabi-image', 'messages': [{'role': 'user', 'content': 'boat'}], **extra})
    assert response.status_code == 200 and URL in response.text


@pytest.mark.parametrize('stream', [False, True])
def test_image_chat_failure_is_http_json_not_fake_success_stream(client, stream):
    async def refusal(*args):
        raise UpstreamError('No image generated', 502, 'image_not_generated')
    client.app.state.media.generate_image = refusal
    response = client.post('/v1/chat/completions', headers=IH, json={
        'model': 'wabi-image', 'messages': [{'role': 'user', 'content': 'boat'}], 'stream': stream})
    if stream:
        assert response.status_code == 200
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert frames[-1]['error']['code'] == 'image_not_generated'
    else:
        assert response.status_code == 502
        assert response.headers['content-type'] == 'application/json'
        assert response.json()['error']['code'] == 'image_not_generated'
    assert '[DONE]' not in response.text
