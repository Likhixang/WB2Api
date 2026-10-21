"""Private-thread video adapter based on persisted, observed Wabi responses.

A successful generate_video tool result only queues work. The eventual completed
assistant attachment can belong to a new turn whose conversation_branch_id is
our accepted turn. Read-only polling recovers both without account-wide SSE,
message replay, artifact downloads, or modifications to the shared chat timeout.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urlsplit
import uuid

import httpx

from .wabi import UpstreamError, Wabi, thread_title

# Exact origin observed in the verified private-video capture, not *.cloudfront.net.
VIDEO_HOSTS = frozenset({'d34c7q5wjf4spy.cloudfront.net'})
_ID = re.compile(r'[0-9a-f]{24}\Z')
_FAILED = frozenset({'failed', 'error', 'cancelled', 'canceled'})


def video_url(value: object) -> str:
    """Allow only verified HTTPS artifact links, safe even in Markdown links."""
    if not isinstance(value, str) or not value or len(value) > 16384 or any(
        ord(c) <= 32 or 127 <= ord(c) <= 159 or c in '<>()\\#' for c in value
    ):
        raise UpstreamError('Upstream returned an invalid video URL', code='invalid_media_url')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname in VIDEO_HOSTS
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and bool(parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise UpstreamError('Upstream video origin is not verified', code='invalid_media_url')
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise UpstreamError('Invalid upstream video identifier', code='invalid_upstream_response')
    return value


def _protocol() -> UpstreamError:
    return UpstreamError('Invalid upstream video response', code='invalid_upstream_response')


def _failed() -> UpstreamError:
    return UpstreamError('Upstream video generation failed', code='media_generation_failed')


def _idle(state: dict, thread_id: str) -> bool:
    if (state.get('thread_id') != thread_id or 'active_turn' not in state
            or not isinstance(state['active_turn'], (dict, type(None)))
            or not isinstance(state.get('queued_turns'), list)
            or not isinstance(state.get('has_background_work'), bool)
            or not isinstance(state.get('queue_paused'), bool)):
        raise _protocol()
    return (state['active_turn'] is None and not state['queued_turns']
            and not state['has_background_work'] and not state['queue_paused'])


def _belongs(message: dict, thread_id: str, turn_id: str) -> bool:
    if message.get('thread_id') != thread_id or message.get('role') != 'assistant':
        return False
    turn, branch = message.get('turn_id'), message.get('conversation_branch_id')
    if not isinstance(turn, str) or not _ID.fullmatch(turn):
        return False
    # A conflicting branch must never be rescued merely by a matching turn ID.
    return ((turn == turn_id and branch in (None, turn_id))
            or (turn != turn_id and branch == turn_id))


class Video:
    def __init__(self, wabi: Wabi, timeout: float = 900, poll_interval: float = 3):
        self.wabi = wabi
        self.timeout = Wabi._positive_float(timeout, 'video_timeout')
        self.poll_interval = Wabi._positive_float(poll_interval, 'video_poll_interval')

    async def _messages(self, root: str, headers: dict) -> list[dict]:
        """Read complete bounded persisted history, including older tool results."""
        messages: dict[str, dict] = {}
        before = None
        boundaries: set[str] = set()
        while True:
            params = {'limit': 100}
            if before is not None:
                params['before_id'] = before
            data = await self.wabi._json('GET', root + '/messages', headers=headers, params=params)
            items = data.get('items')
            if not isinstance(items, list) or len(items) > 100:
                raise _protocol()
            page = []
            for item in items:
                msg = item.get('message', item) if isinstance(item, dict) else None
                if not isinstance(msg, dict):
                    raise _protocol()
                identity = _id(msg.get('id'))
                page.append(identity)
                # Newest page wins if a message straddles pagination boundaries.
                messages.setdefault(identity, msg)
                if len(messages) > 4096:
                    raise UpstreamError('Too many video messages', code='upstream_response_too_large')
            older = data.get('has_older', len(items) == 100)
            if not isinstance(older, bool):
                raise _protocol()
            if not older:
                return list(messages.values())
            if not page or page[0] in boundaries:
                raise _protocol()
            before = page[0]
            boundaries.add(before)
            if len(page) != len(set(page)):
                raise _protocol()

    async def _artifacts(self, messages: list[dict], thread_id: str,
                         turn_id: str, headers: dict) -> list[str]:
        relevant = [m for m in messages if _belongs(m, thread_id, turn_id)]
        completed = []
        for msg in relevant:
            if msg.get('status') in _FAILED:
                raise _failed()
            if msg.get('status') == 'completed':
                if not isinstance(msg.get('blocks'), list) or not isinstance(msg.get('attachments'), list):
                    raise _protocol()
                completed.append(msg)
        # Two passes make tool-result ordering in persisted messages irrelevant.
        tools = set()
        for msg in completed:
            for block in msg['blocks']:
                if (isinstance(block, dict) and block.get('type') == 'tool_use'
                        and block.get('name') == 'generate_video'):
                    identity = block.get('id')
                    if not isinstance(identity, str) or not identity or len(identity) > 512:
                        raise _protocol()
                    tools.add(identity)
        succeeded = False
        for msg in completed:
            for block in msg['blocks']:
                if (not isinstance(block, dict) or block.get('type') != 'tool_result'
                        or not isinstance(block.get('tool_use_id'), str)
                        or block['tool_use_id'] not in tools):
                    continue
                if block.get('phase') in _FAILED:
                    raise _failed()
                succeeded |= block.get('phase') == 'succeeded'
        if not succeeded:
            return []
        attachments: dict[str, dict] = {}
        for msg in completed:
            for att in msg['attachments']:
                if not isinstance(att, dict):
                    raise _protocol()
                # Covers and other media do not count, even if named video.mp4.
                if att.get('mime_type') not in (None, 'video/mp4'):
                    continue
                aid = _id(att.get('attachment_id') or att.get('id'))
                if any(key in att and att[key] != aid for key in ('id', 'attachment_id')):
                    raise _protocol()
                attachments[aid] = att
        urls = []
        for aid, att in attachments.items():
            if att.get('mime_type') is None or not (att.get('download_url') or att.get('url')):
                response = await self.wabi._json(
                    'GET', self.wabi.base_url + '/api/v1/media/get',
                    headers=headers, params={'media_id': aid})
                dto = response.get('data')
                if not isinstance(dto, dict):
                    raise _protocol()
                if any(key in dto and dto[key] != aid for key in ('id', 'attachment_id')):
                    raise _protocol()
                if not any(dto.get(key) == aid for key in ('id', 'attachment_id')):
                    raise _protocol()
                att = dto
            if att.get('mime_type') != 'video/mp4':
                continue
            url = att.get('download_url') or att.get('url')
            if not url:
                continue  # Callback/media metadata can persist before the URL.
            url = video_url(url)
            if url not in urls:
                urls.append(url)
            if len(urls) > 1:
                raise UpstreamError('Upstream returned more than one video', code='unexpected_video_count')
        return urls

    async def generate_video(self, account_id: str, prompt: str) -> list[str]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200000:
            raise UpstreamError('A nonempty video prompt of at most 200000 characters is required',
                                400, 'invalid_prompt')
        w = self.wabi
        try:
            # One independent deadline covers lock wait, refresh, creation, the
            # single message POST and all read-only polling, including media/get.
            async with asyncio.timeout(self.timeout):
                async with w._account_lock(account_id):
                    account = await w._account(account_id)
                    if not account.get('device_id'):
                        raise UpstreamError('Account has no persistent device ID', 400, 'missing_device_id')
                    headers = w._headers(account)
                    created = await w._json('POST', w.base_url + '/api/v1/threads', headers=headers,
                                            json={'title': thread_title('video'), 'request_id': str(uuid.uuid4()), 'kind': 'private'})
                    thread = created.get('thread')
                    if not isinstance(thread, dict):
                        raise _protocol()
                    thread_id = _id(thread.get('id'))
                    root = w.base_url + '/api/v1/threads/' + quote(thread_id, safe='')
                    readback = await w._json('GET', root, headers=headers)
                    thread = readback.get('thread')
                    if not isinstance(thread, dict) or thread.get('id') != thread_id or thread.get('kind') != 'private':
                        raise _protocol()
                    # Private threads have a static turn_id=null greeting, not a
                    # group opening turn. Only validate/poll idle preparation.
                    while not _idle(await w._json('GET', root + '/turn-state', headers=headers), thread_id):
                        await asyncio.sleep(self.poll_interval)
                    accepted = await w._json('POST', root + '/messages', headers=headers, json={
                        'client_id': str(uuid.uuid4()), 'blocks': [{'type': 'text', 'text': prompt}],
                        'attachment_ids': [], 'source': 'typed', 'page_context': {'app_surface': 'chat_only'}})
                    turn_id = _id(accepted.get('turn_id'))
                    if accepted.get('disposition') != 'started':
                        raise UpstreamError('Video did not start an isolated turn; request was not replayed',
                                            code='media_turn_conflict')
                    accepted_message = accepted.get('message')
                    if not isinstance(accepted_message, dict) or any(
                        accepted_message.get(key) != expected
                        for key, expected in (('thread_id', thread_id), ('turn_id', turn_id))
                    ):
                        raise _protocol()
                    while True:
                        messages = await self._messages(root, headers)
                        urls = await self._artifacts(messages, thread_id, turn_id, headers)
                        if urls:
                            return urls
                        # Idle with no artifact is not completion: a background
                        # video callback may arrive minutes after the queue ack.
                        _idle(await w._json('GET', root + '/turn-state', headers=headers), thread_id)
                        await asyncio.sleep(self.poll_interval)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError('Video request timed out; request was not replayed',
                                504, 'upstream_timeout') from exc
        except httpx.HTTPError as exc:
            raise UpstreamError('Video transport failed; request was not replayed',
                                code='upstream_transport_error') from exc
