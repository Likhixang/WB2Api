"""Synthetic private-video protocol regressions; never generate live media."""
import asyncio
import json
import time

import httpx
import pytest

from wb2api.video import Video, video_url
from wb2api.wabi import UpstreamError, Wabi

THREAD = 'a' * 24
TURN = 'b' * 24
CALLBACK = 'c' * 24
AID = 'd' * 24
URL = 'https://d34c7q5wjf4spy.cloudfront.net/fixture/video.mp4'


def message(turn=TURN, **changes):
    return {'id': 'e' * 24, 'thread_id': THREAD, 'turn_id': turn,
            'conversation_branch_id': None, 'role': 'assistant', 'status': 'completed',
            'blocks': [], 'attachments': [], **changes}


def queued(**changes):
    return message(blocks=[
        {'type': 'tool_use', 'id': 'call_video', 'name': 'generate_video'},
        {'type': 'tool_result', 'tool_use_id': 'call_video', 'phase': 'succeeded',
         'summary': 'Queued video generation', 'content': {}}], **changes)


def artifact(**changes):
    result = message(CALLBACK, id='f' * 24, conversation_branch_id=TURN,
                     attachments=[{'id': AID, 'attachment_id': AID,
                                   'mime_type': 'video/mp4', 'download_url': URL}])
    result.update(changes)
    return result


class Store:
    def get_account(self, account_id):
        return {'id_token': 'fixture', 'device_id': 'fixture-device',
                'expires_at': time.time() + 3600}


class Fixture:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or [[queued()], [queued(), artifact()]]
        self.calls = []
        self.polls = 0
        self.prep = 0
        self.sent = False
        self.fail = None
        self.hang = False
        self.entered = asyncio.Event()
        self.media = {'data': {'id': AID, 'attachment_id': AID,
                               'mime_type': 'video/mp4', 'url': URL}}
        self.wrapped = True

    async def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path, request))
        if self.fail == (request.method, path):
            raise httpx.ReadError('PRIVATE_DO_NOT_ECHO', request=request)
        if request.method == 'POST' and path == '/api/v1/threads':
            body = json.loads(request.content)
            assert body['title'].startswith('Video ')
            assert set(body) == {'title', 'request_id', 'kind'} and body['kind'] == 'private'
            return httpx.Response(200, json={'thread': {'id': THREAD, 'kind': 'private'}})
        if path == '/api/v1/threads/' + THREAD:
            return httpx.Response(200, json={'thread': {'id': THREAD, 'kind': 'private'}})
        if path.endswith('/turn-state'):
            state = {'thread_id': THREAD, 'active_turn': None, 'queued_turns': [],
                     'queue_paused': False, 'has_background_work': False, 'revision': 1}
            if not self.sent and self.prep:
                self.prep -= 1
                state['has_background_work'] = True
            return httpx.Response(200, json=state)
        if path.endswith('/messages') and request.method == 'POST':
            self.sent = True
            assert json.loads(request.content)['blocks'] == [{'type': 'text', 'text': 'a red boat video'}]
            return httpx.Response(200, json={'turn_id': TURN, 'disposition': 'started',
                                           'message': message(role='user')})
        if path.endswith('/messages'):
            self.entered.set()
            if self.hang:
                await asyncio.Event().wait()
            snapshot = self.snapshots[min(self.polls, len(self.snapshots) - 1)]
            self.polls += 1
            return httpx.Response(200, json={'items': [{'message': m} if self.wrapped else m for m in snapshot],
                                           'has_older': False, 'has_newer': False})
        if path == '/api/v1/media/get':
            assert request.url.params['media_id'] == AID
            return httpx.Response(200, json=self.media)
        raise AssertionError(str(request.url))

    def posts(self):
        return [path for method, path, _ in self.calls if method == 'POST']


async def setup(fixture, timeout=0.3, wtimeout=1.0):
    w = Wabi(Store(), {'request_timeout': wtimeout})
    await w.client.aclose()
    w.client = httpx.AsyncClient(transport=httpx.MockTransport(fixture))
    return w, Video(w, timeout=timeout, poll_interval=0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [True, False])
async def test_private_pending_idle_then_cross_turn_completed_callback(wrapped):
    opening = message(None, attachments=artifact()['attachments'])
    pending = artifact(status='in_progress')
    fixture = Fixture([[opening, queued()], [queued(), pending], [queued(), artifact()]])
    fixture.prep = 2
    fixture.wrapped = wrapped
    w, video = await setup(fixture)
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert fixture.polls == 3
        assert len(fixture.posts()) == 2
        assert not w._locks and not w._readers
        assert all('events' not in path for _, path, _ in fixture.calls)
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'thread_id': 'other'}, {'conversation_branch_id': 'other'},
    {'conversation_branch_id': None}, {'role': 'user'}, {'status': 'in_progress'},
    {'attachments': [{'id': AID, 'mime_type': 'image/png', 'url': URL}]},
    {'attachments': [], 'blocks': [{'type': 'text', 'text': URL}]},
])
async def test_foreign_unfinished_and_non_video_never_return(change):
    bad = artifact()
    bad.update(change)
    fixture = Fixture([[queued(), bad], [queued(), artifact()]])
    w, video = await setup(fixture)
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert fixture.polls == 2
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['wrong_tool', 'wrong_reference', 'pending'])
async def test_attachments_require_correlated_successful_video_tool(mutation):
    q = queued()
    if mutation == 'wrong_tool':
        q['blocks'][0]['name'] = 'generate_image'
    elif mutation == 'wrong_reference':
        q['blocks'][1]['tool_use_id'] = 'foreign'
    else:
        q['blocks'][1]['phase'] = 'running'
    fixture = Fixture([[q, artifact()], [queued(), artifact()]])
    w, video = await setup(fixture)
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert fixture.polls == 2
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_media_get_unwraps_data_and_matches_real_attachment_id():
    att = artifact()
    del att['attachments'][0]['download_url']
    fixture = Fixture([[queued(), att]])
    w, video = await setup(fixture)
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert len([p for _, p, _ in fixture.calls if p == '/api/v1/media/get']) == 1
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'error', 'cancelled', 'canceled'])
async def test_correlated_failure_is_error(status):
    fixture = Fixture([[queued(), artifact(status=status)]])
    w, video = await setup(fixture)
    try:
        with pytest.raises(UpstreamError) as exc:
            await video.generate_video('account', 'a red boat video')
        assert exc.value.code == 'media_generation_failed'
        assert len(fixture.posts()) == 2 and not w._locks
    finally:
        await w.close()


@pytest.mark.parametrize('url', [None, '', 'http://d34c7q5wjf4spy.cloudfront.net/x',
    'https://example.com/x.mp4', 'https://127.0.0.1/x',
    'https://d34c7q5wjf4spy.cloudfront.net.evil/x',
    'https://u:p@d34c7q5wjf4spy.cloudfront.net/x',
    'https://d34c7q5wjf4spy.cloudfront.net:8443/x',
    URL + '#fragment', URL + '\n', URL + '<x>', URL + '(x)', URL + '\\x', URL + 'x' * 16384])
def test_video_url_allowlist_and_markdown_safety(url):
    with pytest.raises(UpstreamError) as exc:
        video_url(url)
    assert exc.value.code == 'invalid_media_url'
    assert video_url(URL + '?token=fixture') == URL + '?token=fixture'


@pytest.mark.asyncio
async def test_video_deadline_independent_of_chat_timeout():
    fixture = Fixture([[queued()]] * 8 + [[queued(), artifact()]])
    w, video = await setup(fixture, timeout=0.4, wtimeout=0.02)
    video.poll_interval = 0.01
    try:
        assert video.timeout == 0.4
        assert Video(w).timeout == 900
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert fixture.polls == 9
        assert w.timeout == 0.02
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_deadline_and_cancellation_release_account_lock_without_replay():
    fixture = Fixture([[queued()]])
    w, video = await setup(fixture, timeout=0.02)
    try:
        with pytest.raises(UpstreamError) as exc:
            await video.generate_video('account', 'a red boat video')
        assert exc.value.code == 'upstream_timeout'
        assert len(fixture.posts()) == 2 and not w._locks
        fixture.hang = True
        fixture.entered.clear()
        task = asyncio.create_task(video.generate_video('account', 'a red boat video'))
        await fixture.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not w._locks and not w._readers
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_lock_wait_included_in_video_deadline():
    fixture = Fixture()
    w, video = await setup(fixture, timeout=0.02)
    try:
        async with w._account_lock('account'):
            with pytest.raises(UpstreamError) as exc:
                await video.generate_video('account', 'a red boat video')
            assert exc.value.code == 'upstream_timeout'
            assert not fixture.calls
        assert not w._locks
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/v1/threads', '/api/v1/threads/' + THREAD + '/messages',
                                  '/api/v1/threads/' + THREAD + '/turn-state'])
async def test_transport_failure_never_retries_posts_or_echoes_details(path):
    fixture = Fixture()
    fixture.fail = ('GET' if path.endswith('turn-state') else 'POST', path)
    w, video = await setup(fixture)
    try:
        with pytest.raises(UpstreamError) as exc:
            await video.generate_video('account', 'a red boat video')
        assert exc.value.code == 'upstream_transport_error'
        assert 'PRIVATE_DO_NOT_ECHO' not in str(exc.value)
        assert fixture.posts().count(path) <= 1 and not w._locks
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['multiple', 'bad_url', 'bad_id', 'media_conflict'])
async def test_invalid_artifacts_fail_closed(problem):
    final = artifact()
    expected = 'invalid_upstream_response'
    fixture = Fixture([[queued(), final]])
    if problem == 'multiple':
        final['attachments'].append({'id': '1' * 24, 'mime_type': 'video/mp4', 'url': URL + '?second'})
        expected = 'unexpected_video_count'
    elif problem == 'bad_url':
        final['attachments'][0]['download_url'] = 'https://unverified.example/video.mp4'
        expected = 'invalid_media_url'
    elif problem == 'bad_id':
        final['attachments'][0]['id'] = '../not-an-id'
    else:
        del final['attachments'][0]['download_url']
        fixture.media['data']['id'] = '1' * 24
    w, video = await setup(fixture)
    try:
        with pytest.raises(UpstreamError) as exc:
            await video.generate_video('account', 'a red boat video')
        assert exc.value.code == expected
        assert len(fixture.posts()) == 2 and not w._locks
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_completed_empty_callback_waits_for_later_media_revision():
    empty = artifact(attachments=[])
    fixture = Fixture([[queued(), empty], [queued(), artifact()]])
    w, video = await setup(fixture)
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert fixture.polls == 2
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_whole_deadline_includes_credentials_before_any_post():
    class SlowStore(Store):
        async def get_account(self, account_id):
            await asyncio.Event().wait()
    fixture = Fixture()
    w, video = await setup(fixture, timeout=0.01)
    w.store = SlowStore()
    try:
        with pytest.raises(UpstreamError) as exc:
            await video.generate_video('account', 'a red boat video')
        assert exc.value.code == 'upstream_timeout'
        assert not fixture.calls and not w._locks
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_messages_pagination_keeps_original_tool_correlation():
    fixture = Fixture()
    w, video = await setup(fixture)
    pages = []
    async def transport(request):
        if request.method == 'GET' and request.url.path.endswith('/messages'):
            pages.append(dict(request.url.params))
            if 'before_id' not in request.url.params:
                return httpx.Response(200, json={'items': [{'message': artifact()}], 'has_older': True})
            assert request.url.params['before_id'] == 'f' * 24
            return httpx.Response(200, json={'items': [{'message': queued()}], 'has_older': False})
        return await fixture(request)
    await w.client.aclose()
    w.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        assert await video.generate_video('account', 'a red boat video') == [URL]
        assert len(pages) == 2
    finally:
        await w.close()
