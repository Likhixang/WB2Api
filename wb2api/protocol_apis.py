"""Responses/Messages wire adapters over the existing chat completion kernel.

Install *after* defining completion: install_protocol_apis(app, completion,
Completion, inference). No HTTP loopback and exactly one kernel call per create.
Function execution remains the client's responsibility. Only text and function
calling are supported; hosted tools, media, background work and structured output
are rejected rather than simulated. Effort is a prompt preference, not a native
reasoning budget. Usage is unknown (Responses null, Messages {}) unless supplied
by the kernel; no estimated/billable token counts are manufactured.

Responses continuation is private, process-local, API-key isolated and bounded
(15 minutes, 256 entries, 64 MiB total, 32 MiB/entry). Restart, TTL, eviction and
store=false make an ID unavailable; this is NOT durable OpenAI storage. IDs are
cached only after successful generation, including successful streaming EOF.
"""
from __future__ import annotations

import codecs
import copy
import hashlib
import inspect
import json
import threading
import time
import uuid
from collections import OrderedDict

from fastapi import Request
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse

from .budgets import REQUEST_BYTES
from .tool_compat import ToolAdapter, ToolProtocolError


class ProtocolError(Exception):
    def __init__(self, message, status=400, code='invalid_request_error', param=None):
        super().__init__(message)
        self.status, self.code, self.param = status, code, param


def _error(exc, anthropic=False):
    status = getattr(exc, 'status', getattr(exc, 'status_code', 400))
    code = getattr(exc, 'code', 'authentication_error' if status == 401 else 'invalid_request_error')
    message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    if anthropic:
        kind = ('authentication_error' if status == 401 else 'permission_error' if status == 403
                else 'not_found_error' if status == 404 else 'rate_limit_error' if status == 429
                else 'api_error' if status >= 500 else 'invalid_request_error')
        return JSONResponse({'type': 'error', 'error': {'type': kind, 'message': message}}, status_code=status)
    return JSONResponse({'error': {'message': message, 'type': 'invalid_request_error' if status < 500 else 'server_error',
                                  'code': code, 'param': getattr(exc, 'param', None)}}, status_code=status)


def _object(value, label):
    if not isinstance(value, dict):
        raise ProtocolError(label + ' must be an object')
    return value


def _string(value, label):
    if not isinstance(value, str):
        raise ProtocolError(label + ' must be text')
    return value


def _text(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ProtocolError('Only text content is supported')
    parts = []
    for part in value:
        _object(part, 'content part')
        if part.get('type') not in ('text', 'input_text', 'output_text'):
            raise ProtocolError('Only text content is supported', code='unsupported_content')
        parts.append(_string(part.get('text'), 'text'))
    return ''.join(parts)


def _call(call_id, name, arguments):
    return {'id': _string(call_id, 'call_id'), 'type': 'function',
            'function': {'name': _string(name, 'function name'), 'arguments': _string(arguments, 'arguments')}}


def _append_calls(messages, calls):
    # Responses exposes parallel calls as consecutive flat items. Preserve one
    # assistant turn, otherwise chat's pending-call validation rejects the history.
    if messages and messages[-1]['role'] == 'assistant':
        messages[-1].setdefault('tool_calls', []).extend(calls)
    else:
        messages.append({'role': 'assistant', 'content': None, 'tool_calls': calls})


def _responses_input(value):
    if isinstance(value, str):
        return [{'role': 'user', 'content': value}]
    if not isinstance(value, list):
        raise ProtocolError('input must be text or a list of input items', param='input')
    messages = []
    for item in value:
        _object(item, 'input item')
        kind = item.get('type', 'message')
        if kind == 'function_call':
            _append_calls(messages, [_call(item.get('call_id'), item.get('name'), item.get('arguments'))])
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': _string(item.get('call_id'), 'call_id'),
                             'content': _text(item.get('output'))})
        elif kind == 'message':
            role = item.get('role')
            if role not in ('user', 'assistant', 'system', 'developer'):
                raise ProtocolError('Invalid message role')
            messages.append({'role': role, 'content': _text(item.get('content'))})
        elif kind == 'reasoning':
            # Encrypted/provider reasoning cannot be replayed as actual thought.
            # SDKs may echo our empty summaries; never insert it into the prompt.
            if item.get('encrypted_content'):
                raise ProtocolError('Encrypted reasoning replay is not supported', code='unsupported_content')
        else:
            raise ProtocolError('Unsupported input item type', code='unsupported_content')
    return messages


def _tools(value, anthropic=False):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 128:
        raise ProtocolError('tools must be a list of at most 128 functions', param='tools')
    result = []
    for tool in value:
        _object(tool, 'tool')
        if anthropic:
            if tool.get('type', 'custom') != 'custom':
                raise ProtocolError('Only client function tools are supported', code='unsupported_tool')
            fn = {'name': tool.get('name'), 'parameters': tool.get('input_schema', {})}
        else:
            if tool.get('type') != 'function':
                raise ProtocolError('Only client function tools are supported', code='unsupported_tool')
            fn = {key: tool[key] for key in ('name', 'parameters', 'strict') if key in tool}
        if 'description' in tool:
            fn['description'] = tool['description']
        result.append({'type': 'function', 'function': fn})
    return result


def _choice(value, anthropic=False):
    if value is None:
        return None
    if anthropic:
        _object(value, 'tool_choice')
        kind = value.get('type')
        if kind == 'tool':
            return {'type': 'function', 'function': {'name': value.get('name')}}
        if kind in ('auto', 'any', 'none'):
            return 'required' if kind == 'any' else kind
        raise ProtocolError('Invalid tool_choice')
    if isinstance(value, dict):
        if value.get('type') != 'function':
            raise ProtocolError('Only function tool_choice is supported')
        return {'type': 'function', 'function': {'name': value.get('name')}}
    if value not in ('auto', 'none', 'required'):
        raise ProtocolError('Invalid tool_choice')
    return value


def _messages_input(body):
    messages = []
    if 'system' in body:
        messages.append({'role': 'system', 'content': _text(body['system'])})
    source = body.get('messages')
    if not isinstance(source, list) or not source:
        raise ProtocolError('messages must be a non-empty list', param='messages')
    for item in source:
        _object(item, 'message')
        role = item.get('role')
        if role not in ('user', 'assistant'):
            raise ProtocolError('Messages role must be user or assistant')
        content = item.get('content')
        if isinstance(content, str):
            messages.append({'role': role, 'content': content})
            continue
        if not isinstance(content, list):
            raise ProtocolError('content must be text or content blocks')
        text, calls, results = [], [], []
        for block in content:
            _object(block, 'content block')
            kind = block.get('type')
            if kind == 'text':
                text.append(_string(block.get('text'), 'text'))
            elif kind == 'tool_use' and role == 'assistant':
                args = _object(block.get('input'), 'tool input')
                calls.append(_call(block.get('id'), block.get('name'), json.dumps(args, ensure_ascii=False, allow_nan=False)))
            elif kind == 'tool_result' and role == 'user':
                result = _text(block.get('content', ''))
                # Preserve error indication as untrusted result data, not a policy.
                if block.get('is_error'):
                    result = json.dumps({'is_error': True, 'content': result}, ensure_ascii=False)
                results.append({'role': 'tool', 'tool_call_id': _string(block.get('tool_use_id'), 'tool_use_id'), 'content': result})
            elif kind in ('thinking', 'redacted_thinking') and role == 'assistant':
                continue  # no claimed/replayed chain of thought
            else:
                raise ProtocolError('Unsupported Messages content block', code='unsupported_content')
        # Results must immediately follow their assistant calls, even if a client
        # interleaves ordinary user text in the same content array.
        messages.extend(results)
        if text or calls or not results:
            message = {'role': role, 'content': ''.join(text) if text else (None if calls else '')}
            if calls:
                message['tool_calls'] = calls
            messages.append(message)
    return messages


def _normalized(body, anthropic=False):
    if type(body.get('stream', False)) is not bool:
        raise ProtocolError('stream must be boolean')
    if body.get('model', 'wabi') not in ('wabi', 'default', 'sabi', 'gpt-6.1-sol', 'gpt-image-2.5-flare', 'wan-2.1', 'wabi-image', 'wabi-video'):
        raise ProtocolError('Model routing alias is not recognized', code='model_not_found')
    value = {'model': body.get('model', 'wabi'), 'stream': body.get('stream', False),
             'tools': _tools(body.get('tools'), anthropic), 'tool_choice': _choice(body.get('tool_choice'), anthropic)}
    if anthropic:
        value['messages'] = _messages_input(body)
        choice = body.get('tool_choice') or {}
        disabled = choice.get('disable_parallel_tool_use', False)
        if type(disabled) is not bool:
            raise ProtocolError('disable_parallel_tool_use must be boolean')
        value['parallel_tool_calls'] = not disabled
        output_config = body.get('output_config') or {}
        _object(output_config, 'output_config')
        if output_config.get('format'):
            raise ProtocolError('Structured output is not supported', code='unsupported_response_format')
        if 'effort' in output_config:
            value['reasoning_effort'] = output_config['effort']
        thinking = body.get('thinking')
        if thinking is not None:
            _object(thinking, 'thinking')
            if thinking.get('type') not in ('enabled', 'disabled', 'adaptive'):
                raise ProtocolError('Invalid thinking type')
            value.setdefault('reasoning_effort', 'none' if thinking['type'] == 'disabled' else 'high')
        for source, target in [('max_tokens', 'max_tokens'), ('stop_sequences', 'stop'), ('temperature', 'temperature'), ('top_p', 'top_p')]:
            if source in body:
                value[target] = body[source]
    else:
        if body.get('background'):
            raise ProtocolError('Background responses are not supported', code='unsupported_background')
        if body.get('conversation'):
            raise ProtocolError('Use previous_response_id for process-local continuation', code='unsupported_conversation')
        if type(body.get('store', True)) is not bool:
            raise ProtocolError('store must be boolean')
        value['messages'] = _responses_input(body.get('input'))
        if 'parallel_tool_calls' in body:
            if type(body['parallel_tool_calls']) is not bool:
                raise ProtocolError('parallel_tool_calls must be boolean')
            value['parallel_tool_calls'] = body['parallel_tool_calls']
        reasoning = body.get('reasoning')
        if reasoning is not None:
            _object(reasoning, 'reasoning')
            if 'effort' in reasoning:
                value['reasoning_effort'] = reasoning['effort']
        text = body.get('text')
        if text is not None:
            _object(text, 'text')
            format_value = text.get('format') or {}
            _object(format_value, 'text.format')
            if format_value.get('type', 'text') != 'text':
                raise ProtocolError('Structured output is not supported', code='unsupported_response_format')
        for source, target in [('max_output_tokens', 'max_completion_tokens'), ('temperature', 'temperature'), ('top_p', 'top_p')]:
            if source in body:
                value[target] = body[source]
    return value


class ResponseCache:
    """Byte-counted LRU snapshots; never keeps raw API keys or writes to disk."""
    def __init__(self, ttl=900, max_entries=256, max_bytes=64 * 1024 * 1024, max_entry_bytes=32 * 1024 * 1024):
        self.ttl, self.max_entries, self.max_bytes, self.max_entry_bytes = ttl, max_entries, max_bytes, max_entry_bytes
        self.entries, self.bytes, self.lock = OrderedDict(), 0, threading.Lock()

    def _prune(self):
        now = time.monotonic()
        for key in list(self.entries):
            if self.entries[key][0] <= now:
                self.bytes -= len(self.entries.pop(key)[1])

    def put(self, owner, identity, response, history):
        blob = json.dumps({'response': response, 'history': history}, ensure_ascii=False, allow_nan=False).encode()
        if len(blob) > min(self.max_entry_bytes, self.max_bytes) or self.max_entries <= 0:
            return False
        with self.lock:
            self._prune()
            key = (owner, identity)
            old = self.entries.pop(key, None)
            if old:
                self.bytes -= len(old[1])
            while self.entries and (len(self.entries) >= self.max_entries or self.bytes + len(blob) > self.max_bytes):
                self.bytes -= len(self.entries.popitem(last=False)[1][1])
            self.entries[key] = (time.monotonic() + self.ttl, blob)
            self.bytes += len(blob)
        return True

    def get(self, owner, identity):
        if not isinstance(identity, str):
            raise ProtocolError('previous_response_id must be text')
        with self.lock:
            self._prune()
            key = (owner, identity)
            if key not in self.entries:
                raise ProtocolError('Response unavailable: unknown ID, different API key, store=false, expiry, eviction or process restart',
                                    404, 'response_not_found', 'previous_response_id')
            self.entries.move_to_end(key)
            return json.loads(self.entries[key][1])


def _response_base(body, identity):
    return {'id': identity, 'object': 'response', 'created_at': int(time.time()), 'status': 'in_progress',
            'model': body.get('model', 'wabi'), 'output': [], 'error': None, 'incomplete_details': None,
            'usage': None, 'parallel_tool_calls': body.get('parallel_tool_calls', True),
            'tool_choice': body.get('tool_choice', 'auto'), 'tools': body.get('tools', []),
            'store': body.get('store', True), 'metadata': body.get('metadata') or {},
            'previous_response_id': body.get('previous_response_id'), 'instructions': body.get('instructions')}


def _output(message):
    output = []
    if message.get('content') is not None and (message['content'] or not message.get('tool_calls')):
        output.append({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': message['content'], 'annotations': [], 'logprobs': []}]})
    for call in message.get('tool_calls') or []:
        output.append({'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call', 'status': 'completed',
                       'call_id': call['id'], **call['function']})
    return output


def _usage(usage, anthropic=False):
    if not isinstance(usage, dict):
        return {} if anthropic else None
    result = {}
    for source, target in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens'), ('total_tokens', 'total_tokens')]:
        if type(usage.get(source)) is int and usage[source] >= 0:
            result[target] = usage[source]
    if anthropic:
        result.pop('total_tokens', None)
    return result or ({} if anthropic else None)


def _anthropic_message(message, model, identity, finish='stop', usage=None):
    content = []
    if message.get('content'):
        content.append({'type': 'text', 'text': message['content']})
    for call in message.get('tool_calls') or []:
        content.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                        'input': json.loads(call['function']['arguments'])})
    return {'id': identity, 'type': 'message', 'role': 'assistant', 'model': model, 'content': content,
            'stop_reason': 'tool_use' if message.get('tool_calls') else 'max_tokens' if finish == 'length' else 'end_turn',
            'stop_sequence': None, 'usage': _usage(usage, True)}


def _headers(response):
    return {key: value for key, value in response.headers.items()
            if key.lower() not in ('content-length', 'content-type', 'transfer-encoding', 'content-encoding')}


def _completion_error(response, anthropic=False):
    """Convert legacy kernel JSON errors, retaining retry/diagnostic headers."""
    try:
        data = json.loads(bytes(response.body))
        error = data.get('error') or {}
        exc = ProtocolError(error.get('message', 'Completion failed'), response.status_code,
                            error.get('code', 'upstream_error'))
    except (ValueError, TypeError, AttributeError):
        exc = ProtocolError('Completion failed', response.status_code, 'upstream_error')
    result = _error(exc, anthropic)
    result.headers.update(_headers(response))
    return result


def _encode(event, data):
    return 'event: ' + event + '\ndata: ' + json.dumps(data, ensure_ascii=False, allow_nan=False) + '\n\n'


async def _chat_frames(response):
    """Incremental UTF-8/SSE parser: handles split frames and keeps comments live."""
    decoder, buffer = codecs.getincrementaldecoder('utf-8')(), ''
    iterator = response.body_iterator
    try:
        async for chunk in iterator:
            buffer += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
            buffer = buffer.replace('\r\n', '\n')
            while '\n\n' in buffer:
                frame, buffer = buffer.split('\n\n', 1)
                comments = [line for line in frame.splitlines() if line.startswith(':')]
                if comments:
                    yield 'comment', '\n'.join(comments) + '\n\n'
                data = '\n'.join(line[5:].lstrip(' ') for line in frame.splitlines() if line.startswith('data:'))
                if data == '[DONE]':
                    yield 'done', None
                elif data:
                    try:
                        yield 'data', json.loads(data)
                    except (ValueError, TypeError):
                        raise ProtocolError('Malformed completion stream', 502, 'invalid_upstream_stream') from None
        buffer += decoder.decode(b'', final=True)
        if buffer.strip():
            raise ProtocolError('Truncated completion stream', 502, 'invalid_upstream_stream')
    finally:
        close = getattr(iterator, 'aclose', None)
        if close:
            await close()


async def _stream(response, body, adapter, anthropic, save):
    identity = ('msg_' if anthropic else 'resp_') + uuid.uuid4().hex
    result = _response_base(body, identity)
    sequence = 0
    message = {'role': 'assistant', 'content': '', 'tool_calls': []}
    calls, text_item, text_index, block_index = {}, None, None, 0
    finish, usage, done = None, None, False

    def event(kind, **data):
        nonlocal sequence
        if not anthropic:
            data['sequence_number'] = sequence
            sequence += 1
        return _encode(kind, {'type': kind, **data})

    if anthropic:
        start = _anthropic_message({}, body.get('model', 'wabi'), identity)
        start['stop_reason'] = None
        # Preserve actual usage if the kernel ever supplies it; absent token
        # counts deliberately remain {} instead of fictional zero-token billing.
        yield event('message_start', message=start)
    else:
        yield event('response.created', response=copy.deepcopy(result))
        yield event('response.in_progress', response=copy.deepcopy(result))
    try:
        async for kind, frame in _chat_frames(response):
            if kind == 'comment':
                yield event('ping') if anthropic else frame
                continue
            if kind == 'done':
                done = True
                break
            if not isinstance(frame, dict):
                raise ProtocolError('Malformed completion stream', 502, 'invalid_upstream_stream')
            if frame.get('error'):
                error = frame['error']
                raise ProtocolError(error.get('message', 'Completion failed'), 502, error.get('code', 'upstream_error'))
            if frame.get('usage') is not None:
                usage = frame['usage']
            for choice in frame.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                delta = choice.get('delta') or {}
                if delta.get('content'):
                    text = _string(delta['content'], 'completion text')
                    if text_item is None:
                        text_index = 0
                        text_item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}
                        if anthropic:
                            yield event('content_block_start', index=block_index, content_block={'type': 'text', 'text': ''})
                        else:
                            yield event('response.output_item.added', output_index=text_index, item=copy.deepcopy(text_item))
                            yield event('response.content_part.added', item_id=text_item['id'], output_index=text_index, content_index=0,
                                        part={'type': 'output_text', 'text': '', 'annotations': [], 'logprobs': []})
                    message['content'] += text
                    if anthropic:
                        yield event('content_block_delta', index=block_index, delta={'type': 'text_delta', 'text': text})
                    else:
                        yield event('response.output_text.delta', item_id=text_item['id'], output_index=text_index, content_index=0, delta=text, logprobs=[])
                for call in delta.get('tool_calls') or []:
                    index = call.get('index', 0)
                    if type(index) is not int or index < 0 or index >= 128:
                        raise ProtocolError('Invalid tool call index', 502, 'invalid_upstream_stream')
                    target = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    if call.get('id'):
                        target['id'] += call['id']
                    fn = call.get('function') or {}
                    target['function']['name'] += fn.get('name', '')
                    target['function']['arguments'] += fn.get('arguments', '')
                if choice.get('finish_reason') is not None:
                    finish = choice['finish_reason']
        if not done or finish is None:
            raise ProtocolError('Completion stream ended without a successful finish', 502, 'incomplete_upstream_stream')
        message['tool_calls'] = [calls[index] for index in sorted(calls)]
        _validate_output(message, adapter)
        if text_item:
            if anthropic:
                yield event('content_block_stop', index=block_index)
                block_index += 1
            else:
                part = {'type': 'output_text', 'text': message['content'], 'annotations': [], 'logprobs': []}
                text_item.update(status='completed', content=[part])
                result['output'].append(text_item)
                yield event('response.output_text.done', item_id=text_item['id'], output_index=text_index, content_index=0, text=message['content'], logprobs=[])
                yield event('response.content_part.done', item_id=text_item['id'], output_index=text_index, content_index=0, part=part)
                yield event('response.output_item.done', output_index=text_index, item=text_item)
        for item in _output({'tool_calls': message['tool_calls']}):
            if anthropic:
                yield event('content_block_start', index=block_index, content_block={'type': 'tool_use', 'id': item['call_id'], 'name': item['name'], 'input': {}})
                yield event('content_block_delta', index=block_index, delta={'type': 'input_json_delta', 'partial_json': item['arguments']})
                yield event('content_block_stop', index=block_index)
                block_index += 1
            else:
                index = len(result['output'])
                yield event('response.output_item.added', output_index=index, item={**item, 'status': 'in_progress', 'arguments': ''})
                yield event('response.function_call_arguments.delta', item_id=item['id'], output_index=index, delta=item['arguments'])
                yield event('response.function_call_arguments.done', item_id=item['id'], output_index=index, arguments=item['arguments'], name=item['name'])
                result['output'].append(item)
                yield event('response.output_item.done', output_index=index, item=item)
        if anthropic:
            final = _anthropic_message(message, body.get('model', 'wabi'), identity, finish, usage)
            yield event('message_delta', delta={'stop_reason': final['stop_reason'], 'stop_sequence': None}, usage=final['usage'])
            yield event('message_stop')
        else:
            result.update(status='incomplete' if finish == 'length' else 'completed', usage=_usage(usage))
            if finish == 'length':
                result['incomplete_details'] = {'reason': 'max_output_tokens'}
            else:
                save(result, message)
            yield event('response.incomplete' if finish == 'length' else 'response.completed', response=result)
    except Exception as exc:
        # Never emit successful completion after a kernel error or malformed JSON.
        if not isinstance(exc, (ProtocolError, ToolProtocolError)):
            exc = ProtocolError('Completion stream failed', 502, 'upstream_error')
        error = {'type': 'api_error' if anthropic else 'server_error', 'message': str(exc)}
        if not anthropic:
            error['code'] = getattr(exc, 'code', 'upstream_error')
        if anthropic:
            yield event('error', error=error)
        else:
            result.update(status='failed', error=error)
            yield event('error', code=error['code'], message=error['message'], param=None)
            yield event('response.failed', response=result)
    finally:
        close = getattr(response.body_iterator, 'aclose', None)
        if close:
            await close()
        if response.background:
            await response.background()


def _validate_output(message, adapter):
    calls = message.get('tool_calls') or []
    if calls and (adapter.choice == 'none' or ((not adapter.parallel or adapter.forced) and len(calls) > 1)):
        raise ProtocolError('Completion violated tool policy', 502, 'invalid_tool_call')
    if not calls and (adapter.choice == 'required' or adapter.forced):
        raise ProtocolError('Completion omitted required tool call', 502, 'invalid_tool_call')
    seen = set()
    for call in calls:
        if not call.get('id') or call['id'] in seen:
            raise ProtocolError('Invalid or duplicate tool call ID', 502, 'invalid_tool_call')
        seen.add(call['id'])
        fn = call['function']
        if adapter.forced and fn['name'] != adapter.forced:
            raise ProtocolError('Completion violated forced tool choice', 502, 'invalid_tool_call')
        fn['arguments'] = adapter.validate_arguments(fn['name'], fn['arguments'])


def install_protocol_apis(app, completion_handler, completion_model, inference_dependency):
    """Bind protocol routes to a live completion closure and inference-only auth.

    inference_dependency must be callable(request), sync or async. For Messages,
    x-api-key is translated on an independent Request scope before invoking it;
    the same inference policy is used, never the admin authorization dependency.
    """
    cache = ResponseCache()
    app.state.protocol_response_cache = cache

    async def authorize(request, anthropic=False):
        auth_request = request
        if anthropic and not request.headers.get('authorization') and request.headers.get('x-api-key'):
            scope = dict(request.scope)
            scope['headers'] = [(key, value) for key, value in request.scope['headers'] if key.lower() != b'authorization']
            scope['headers'].append((b'authorization', ('Bearer ' + request.headers['x-api-key']).encode('latin-1')))
            auth_request = Request(scope, request.receive)
        result = inference_dependency(auth_request)
        if inspect.isawaitable(result):
            await result
        owner = hashlib.sha256(auth_request.headers.get('authorization', '').encode()).hexdigest()
        return auth_request, owner

    async def create(request, anthropic=False):
        try:
            auth_request, owner = await authorize(request, anthropic)
            raw = await request.body()
            if len(raw) > REQUEST_BYTES:
                raise ProtocolError('Request exceeds the transport byte limit', 413, 'body_too_large')
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ProtocolError('Invalid JSON request') from None
            _object(body, 'request')
            normalized = _normalized(body, anthropic)
            history = normalized['messages']
            if not anthropic:
                previous = body.get('previous_response_id')
                if previous is not None:
                    history = cache.get(owner, previous)['history'] + history
                normalized['messages'] = copy.deepcopy(history)
                if body.get('instructions') is not None:
                    normalized['messages'].insert(0, {'role': 'developer', 'content': _string(body['instructions'], 'instructions')})
            try:
                model = completion_model(**normalized)
            except ValidationError:
                raise ProtocolError('Invalid message, tool, effort or generation parameter') from None
            adapter = ToolAdapter(model.tools, model.tool_choice, model.parallel_tool_calls)
            adapter.history_prompt(model.messages)

            def save(response, message):
                if body.get('store', True):
                    cache.put(owner, response['id'], response, history + [message])

            # Direct call shares the same kernel/tools/heartbeats/errors. No
            # upstream request or generation exists in this adapter itself.
            response = await completion_handler(model, auth_request)
            if isinstance(response, StreamingResponse):
                return StreamingResponse(_stream(response, body, adapter, anthropic, save), status_code=response.status_code,
                                         media_type='text/event-stream', headers=_headers(response))
            headers = _headers(response) if isinstance(response, Response) else {}
            if isinstance(response, Response):
                if response.status_code >= 400:
                    return _completion_error(response, anthropic)
                data = json.loads(bytes(response.body))
            else:
                data = response
            if data.get('error'):
                error = data['error']
                return _error(ProtocolError(error.get('message', 'Completion failed'), 502, error.get('code', 'upstream_error')), anthropic)
            choice = data['choices'][0]
            message = copy.deepcopy(choice['message'])
            _validate_output(message, adapter)
            if anthropic:
                result = _anthropic_message(message, body.get('model', 'wabi'), 'msg_' + uuid.uuid4().hex,
                                            choice.get('finish_reason'), data.get('usage'))
            else:
                result = _response_base(body, 'resp_' + uuid.uuid4().hex)
                result.update(status='completed', output=_output(message), usage=_usage(data.get('usage')))
                if choice.get('finish_reason') == 'length':
                    result.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
                else:
                    save(result, message)
            return JSONResponse(result, headers=headers)
        except (ProtocolError, ToolProtocolError, HTTPException) as exc:
            request.state.gateway_error_code = getattr(exc, 'code', 'invalid_request')
            return _error(exc, anthropic)
        except Exception as exc:
            # Upstream errors expose a deliberate status/code, programming errors
            # stay opaque; neither may leak submitted prompts, tokens or schemas.
            if hasattr(exc, 'code') and hasattr(exc, 'status'):
                return _error(exc, anthropic)
            return _error(ProtocolError('Completion failed', 502, 'upstream_error'), anthropic)

    @app.post('/v1/responses')
    async def responses(request: Request):
        return await create(request)

    @app.get('/v1/responses/{response_id}')
    async def retrieve_response(response_id: str, request: Request):
        try:
            _, owner = await authorize(request)
            return JSONResponse(cache.get(owner, response_id)['response'], headers={'Cache-Control': 'no-store'})
        except (ProtocolError, HTTPException) as exc:
            return _error(exc)

    @app.post('/v1/messages')
    async def messages(request: Request):
        return await create(request, True)

    return cache
