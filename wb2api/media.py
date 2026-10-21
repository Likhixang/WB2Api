"""Verified image capability adapter, independent of the text event mapper.

Only completed generate_image tool results are media. Text, attachments and
is_final_message are not completion signals. No downloads, retries or video
endpoint guesses are performed here.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from typing import TypeGuard
from urllib.parse import quote, urlsplit
import uuid

import httpx

from .protocol import ProtocolError, parse_sse
from .wabi import UpstreamError, Wabi

# Observed anonymous artifact origin. Do not accept arbitrary tool-provided URLs.
IMAGE_HOSTS = frozenset({'d34c7q5wjf4spy.cloudfront.net'})


def image_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 16384 or any(
        ord(c) <= 32 or c in '<>\\' for c in value
    ):
        raise UpstreamError('Upstream returned an invalid image URL', code='invalid_media_url')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname in IMAGE_HOSTS
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and not parsed.fragment and bool(parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise UpstreamError('Upstream image origin is not verified', code='invalid_media_url')
    return value


def _integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class ImageTurn:
    """State for one fresh thread; state revisions fence out opening/replays."""

    def __init__(self, thread_id: str, *, max_result_bytes: int = 8 * 1024 * 1024):
        self.thread_id = thread_id
        self.max_result_bytes = max_result_bytes
        self.turn_id: str | None = None
        self.opening_finished = False
        self.opening_idle = False
        self.state_revision = -1
        self.opening_revision = -1
        self.finished = False
        self.messages: dict[str, int] = {}
        self.results: dict[str, list[str]] = {}
        self.tools: set[str] = set()
        self.result_bytes = 0

    def accept(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.opening_revision = self.state_revision
        self.finished = False
        self.messages.clear()
        self.results.clear()
        self.tools.clear()
        self.result_bytes = 0

    def consume(self, data: dict) -> bool:
        message = data.get('message')
        message = message if isinstance(message, dict) else {}
        threads = [v for v in (data.get('thread_id'), message.get('thread_id')) if v is not None]
        if not threads or any(v != self.thread_id for v in threads):
            return False
        turns = [v for v in (data.get('turn_id'), message.get('turn_id')) if v is not None]
        if turns and (any(not isinstance(v, str) or not v for v in turns) or len(set(turns)) != 1):
            return False
        kind = data.get('type')
        if kind == 'turn_state':
            revision = data.get('revision')
            if not _integer(revision) or revision <= self.state_revision:
                return False
            self.state_revision = revision
            active = data.get('active_turn')
            if self.turn_id and isinstance(active, dict) and active.get('turn_id') != self.turn_id:
                raise UpstreamError('Unexpected turn in the dedicated media thread', code='media_turn_conflict')
            idle = ('active_turn' in data and active is None
                    and data.get('has_background_work') is False and data.get('queued_turns') == [])
            if not self.turn_id:
                self.opening_idle = idle and self.opening_finished
                return self.opening_idle
            return (idle and self.finished and revision > self.opening_revision
                    and (not turns or turns == [self.turn_id]))
        if self.turn_id and (not turns or any(v != self.turn_id for v in turns)):
            return False
        if kind in ('turn_error', 'message_error'):
            # Do not echo untrusted upstream error messages or request contents.
            if turns:
                raise UpstreamError('Upstream media generation failed', code='media_generation_failed')
            return False
        if kind != 'message_finished' or message.get('role') != 'assistant':
            return False
        if message.get('status') in ('failed', 'error', 'cancelled'):
            raise UpstreamError('Upstream media generation failed', code='media_generation_failed')
        if message.get('status') != 'completed' or not turns:
            return False
        if not self.turn_id:
            self.opening_finished = True
            return False
        identity, revision = message.get('id'), message.get('revision')
        if not isinstance(identity, str) or not identity or len(identity) > 512 or not _integer(revision):
            raise UpstreamError('Invalid completed media message', code='invalid_upstream_protocol')
        if revision <= self.messages.get(identity, -1):
            return False
        if identity not in self.messages and len(self.messages) >= 4096:
            raise UpstreamError('Too many media messages', code='upstream_response_too_large')
        self.messages[identity] = revision
        self.finished = True
        blocks = message.get('blocks')
        if not isinstance(blocks, list):
            raise UpstreamError('Invalid media blocks', code='invalid_upstream_protocol')
        for block in blocks:
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'generate_image':
                tool_id = block.get('id')
                if isinstance(tool_id, str) and tool_id and len(tool_id) <= 512:
                    if tool_id not in self.tools and len(self.tools) >= 4096:
                        raise UpstreamError('Too many media tool references', code='upstream_response_too_large')
                    self.tools.add(tool_id)
        urls = []
        for block in blocks:
            if not isinstance(block, dict) or block.get('type') != 'tool_result':
                continue
            tool_id = block.get('tool_use_id')
            if not isinstance(tool_id, str) or tool_id not in self.tools or block.get('phase') != 'succeeded':
                continue
            content = block.get('content')
            results = content.get('results') if isinstance(content, dict) else None
            if not isinstance(results, list):
                continue
            for result in results:
                if isinstance(result, dict) and 'image_url' in result:
                    url = image_url(result['image_url'])
                    if url not in urls:
                        urls.append(url)
                        if len(urls) > 1:
                            raise UpstreamError('Upstream returned more than the requested single image', code='unexpected_image_count')
        self.result_bytes -= sum(len(url.encode('utf-8')) for url in self.results.get(identity, []))
        self.result_bytes += sum(len(url.encode('utf-8')) for url in urls)
        if self.result_bytes > self.max_result_bytes:
            raise UpstreamError('Media results exceed the size limit', code='upstream_response_too_large')
        self.results[identity] = urls
        return False

    def result(self) -> list[str]:
        urls = list(dict.fromkeys(url for values in self.results.values() for url in values))
        if not urls:
            raise UpstreamError('Upstream completed without a successful image artifact; text is not an image',
                                code='image_not_generated')
        if len(urls) != 1:
            raise UpstreamError('Upstream returned more than the requested single image', code='unexpected_image_count')
        return urls


class Media:
    def __init__(self, wabi: Wabi):
        self.wabi = wabi

    async def generate_image(self, account_id: str, prompt: str) -> list[str]:
        w = self.wabi
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200000:
            raise UpstreamError('A nonempty image prompt of at most 200000 characters is required', 400, 'invalid_prompt')
        reader = None
        try:
            # Unlike an async generator, this coroutine can safely keep one
            # deadline across lock wait, credentials, opening, POST and completion.
            async with asyncio.timeout(w.timeout):
                async with w._account_lock(account_id):
                    account = await w._account(account_id)
                    if not account.get('device_id'):
                        raise UpstreamError('Account has no persistent device ID', 400, 'missing_device_id')
                    queue: asyncio.Queue[dict | UpstreamError] = asyncio.Queue(w.queue_size)
                    ready = asyncio.get_running_loop().create_future()
                    terminal = asyncio.get_running_loop().create_future()

                    async def consume_events():
                        failure = UpstreamError('Media SSE disconnected; request was not replayed', code='sse_disconnected')
                        try:
                            async with w.client.stream('GET', w.base_url + '/api/v1/chat/events',
                                                       headers=w._headers(account, sse=True)) as response:
                                if response.status_code != 200:
                                    raise w._http_error(response.status_code)
                                if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/event-stream':
                                    raise UpstreamError('Upstream did not provide SSE', code='invalid_upstream_protocol')
                                ready.set_result(None)
                                async for event in parse_sse(w._sse_lines(response), max_event_bytes=w.max_event_bytes):
                                    try:
                                        data = json.loads(event['data'])
                                    except (ValueError, RecursionError) as exc:
                                        raise UpstreamError('Invalid media event JSON', code='invalid_upstream_protocol') from exc
                                    if not isinstance(data, dict):
                                        raise UpstreamError('Media event must be an object', code='invalid_upstream_protocol')
                                    data.setdefault('type', event['event'])
                                    await queue.put(data)
                        except asyncio.CancelledError:
                            raise
                        except ProtocolError as exc:
                            failure = UpstreamError(exc.message, exc.status, exc.code)
                        except UpstreamError as exc:
                            failure = exc
                        except httpx.TimeoutException:
                            failure = UpstreamError('Media stream timed out', 504, 'upstream_timeout')
                        except httpx.HTTPError:
                            failure = UpstreamError('Media SSE transport failed; request was not replayed', code='sse_disconnected')
                        except Exception:
                            failure = UpstreamError('Media SSE reader failed', code='invalid_upstream_protocol')
                        finally:
                            if not ready.done():
                                ready.set_result(failure)
                        if not terminal.done():
                            terminal.set_result(failure)
                        await queue.put(failure)

                    async def next_event():
                        event = await queue.get()
                        if isinstance(event, UpstreamError):
                            raise event
                        return event

                    reader = asyncio.create_task(consume_events(), name='wabi-media-sse-reader')
                    w._readers.add(reader)
                    failure = await ready
                    if failure:
                        raise failure
                    if terminal.done():
                        raise terminal.result()
                    created = await w._json('POST', w.base_url + '/api/v1/threads', headers=w._headers(account),
                                            json={'title': '', 'request_id': str(uuid.uuid4()), 'kind': 'group'})
                    thread = created.get('thread')
                    thread_id = thread.get('id') if isinstance(thread, dict) else None
                    if not isinstance(thread_id, str) or not thread_id:
                        raise UpstreamError('Media thread creation returned no id', code='invalid_upstream_response')
                    state = ImageTurn(thread_id, max_result_bytes=w.max_text_bytes)
                    while not state.consume(await next_event()):
                        pass
                    if terminal.done():
                        raise terminal.result()
                    media_prompt = ('Please generate exactly ONE actual image now using your image generation tool. '
                                    'Do not merely describe it. Use the default available settings. '
                                    'Do not purchase anything or accept any paid upgrade; if payment or additional '
                                    'authorization is required, stop and tell me. Image request:\n' + prompt)
                    accepted = await w._json('POST', w.base_url + '/api/v1/threads/' + quote(thread_id, safe='') + '/messages',
                                             headers=w._headers(account), json={
                                                 'client_id': str(uuid.uuid4()), 'blocks': [{'type': 'text', 'text': media_prompt}],
                                                 'attachment_ids': [], 'source': 'typed', 'page_context': {'app_surface': 'chat_only'}})
                    turn_id = accepted.get('turn_id')
                    if not isinstance(turn_id, str) or not turn_id:
                        raise UpstreamError('Media acceptance returned no turn_id; request was not replayed',
                                            code='invalid_upstream_response')
                    if accepted.get('disposition', 'started') != 'started':
                        raise UpstreamError('Media request did not start an isolated turn; request was not replayed',
                                            code='media_turn_conflict')
                    accepted_message = accepted.get('message')
                    if isinstance(accepted_message, dict) and any(
                        accepted_message.get(key, expected) != expected
                        for key, expected in [('thread_id', thread_id), ('turn_id', turn_id)]
                    ):
                        raise UpstreamError('Media acceptance has conflicting identifiers', code='media_turn_conflict')
                    state.accept(turn_id)
                    while not state.consume(await next_event()):
                        pass
                    return state.result()
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError('Media request timed out; request was not replayed', 504, 'upstream_timeout') from exc
        except httpx.HTTPError as exc:
            raise UpstreamError('Media transport failed; request was not replayed', code='upstream_transport_error') from exc
        finally:
            if reader is not None:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader
                w._readers.discard(reader)
