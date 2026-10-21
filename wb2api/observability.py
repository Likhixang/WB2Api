"""Single-process, bounded and allowlisted request diagnostics (no payload logging)."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
import uuid


_STATIC_PATHS = frozenset({
    '/', '/healthz', '/docs', '/redoc', '/openapi.json', '/v1/models',
    '/v1/chat/completions', '/v1/images/generations', '/admin/api/status',
    '/admin/api/protocol', '/admin/api/accounts', '/admin/api/accounts/export',
    '/admin/api/keys', '/admin/api/auth/send-code', '/admin/api/auth/check-code',
    '/admin/api/auth/google-verify', '/admin/api/auth/mailbox-verify',
    '/admin/api/auth/mailbox-resend',
})
_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE'})
_DYNAMIC_PATHS = (
    (re.compile(r'/admin/api/accounts/[^/]+/(reauth|check)\Z'), '/admin/api/accounts/{account_id}/'),
    (re.compile(r'/admin/api/accounts/[^/]+\Z'), '/admin/api/accounts/{account_id}'),
    (re.compile(r'/admin/api/keys/[^/]+\Z'), '/admin/api/keys/{key_id}'),
)


def _safe_path(path):
    if path in _STATIC_PATHS:
        return path
    for pattern, template in _DYNAMIC_PATHS:
        match = pattern.fullmatch(path)
        if match:
            return template + match.group(1) if match.lastindex else template
    return '<unmatched>'


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, 'a', encoding='utf-8')
        except BaseException:
            os.close(fd)
            raise

    def doRollover(self):
        # stdlib's backupCount=0 never truncates; enforce the budget here too.
        if self.backupCount == 0:
            if self.stream:
                self.stream.close()
                self.stream = None
            os.truncate(self.baseFilename, 0)
            self.stream = self._open()
        else:
            super().doRollover()

    def handleError(self, record):
        # Disk failures must not dump tracebacks or interrupt an API response.
        pass


class RequestLogMiddleware:
    """ASGI middleware. max_bytes bounds current + backups, not each file.

    Use one worker/writer per data directory. max_bytes=0 disables disk writes;
    otherwise each file must have at least 1024 bytes. Backups range from 0 to 32.
    Only fixed endpoint labels, server-generated IDs and numeric timings are logged.
    """

    def __init__(self, app, data_dir, max_bytes=20 * 1024 * 1024, backups=3):
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError('log_max_bytes must be a nonnegative integer')
        if not isinstance(backups, int) or not 0 <= backups <= 32:
            raise ValueError('log_backups must be between 0 and 32')
        per_file = max_bytes // (backups + 1)
        if max_bytes and per_file < 1024:
            raise ValueError('log budget must allow at least 1024 bytes per file')
        self.app = app
        self.directory = Path(data_dir) / 'logs'
        self.max_bytes = max_bytes
        self.backups = backups
        self.active = {}
        self.handler = None
        self.logger = logging.Logger('wb2api.requests.' + uuid.uuid4().hex, level=logging.INFO)
        self.logger.propagate = False
        self._closed = False
        if not max_bytes:
            return
        directory = Path(data_dir) / 'logs'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError('log directory must not be a symlink')
        directory.chmod(0o700)
        # Reconcile prior limits on startup so lowering the budget takes effect now.
        for path in directory.iterdir():
            if path.name != 'requests.log' and not re.fullmatch(r'requests\.log\.[0-9]+', path.name):
                continue
            if path.is_symlink():
                raise ValueError('log files must not be symlinks')
            suffix = 0 if path.name == 'requests.log' else int(path.name.rsplit('.', 1)[1])
            valid_name = path.name == 'requests.log' or path.name in {
                'requests.log.' + str(index) for index in range(1, backups + 1)
            }
            if not valid_name or suffix > backups or path.stat().st_size > per_file:
                path.unlink()
            else:
                path.chmod(0o600)
        self.handler = _PrivateRotatingHandler(
            directory / 'requests.log', maxBytes=per_file, backupCount=backups, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(self.handler)

    def snapshot(self, limit=200, request_id='', errors_only=False):
        """Bounded newest-first tail; never accept a caller-controlled filename."""
        rows = []
        used = 0
        remaining = 2 * 1024 * 1024
        files = [self.directory / ('requests.log' + ('.' + str(i) if i else ''))
                 for i in range(self.backups + 1)]
        for path in files:
            if not path.exists() or path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            used += size
            if not remaining or len(rows) >= limit:
                continue
            count = min(size, remaining)
            with path.open('rb') as handle:
                handle.seek(size - count)
                raw = handle.read(count)
            remaining -= count
            lines = raw.splitlines()
            if count < size and lines:
                lines = lines[1:]
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (not isinstance(row, dict) or not isinstance(row.get('request_id'), str)
                        or not isinstance(row.get('timestamp'), str)
                        or (row.get('status') is not None and (type(row['status']) is not int or not 100 <= row['status'] <= 599))):
                    continue
                # Return only the diagnostic schema, never arbitrary disk fields.
                row = {key: value for key, value in row.items() if key in {
                    'timestamp', 'request_id', 'method', 'path', 'status', 'duration_ms',
                    'first_byte_ms', 'completed', 'disconnected', 'error_type', 'gateway_error_code'}}
                if request_id and row['request_id'] != request_id:
                    continue
                failed = bool(row.get('gateway_error_code') or row.get('error_type') or row.get('disconnected') or row.get('status', 0) and row['status'] >= 400 or not row.get('completed'))
                if errors_only and not failed:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    break
        now = time.monotonic()
        active = [{**row, 'duration_ms': round((now - started) * 1000, 3)}
                  for started, row in self.active.values()
                  if (not request_id or row['request_id'] == request_id) and not errors_only]
        entries = sorted(active + rows, key=lambda row: row.get('timestamp', ''), reverse=True)[:limit]
        return {'entries': entries, 'active_count': len(self.active),
                'storage': {'enabled': bool(self.max_bytes), 'max_bytes': self.max_bytes,
                            'backups': self.backups, 'used_bytes': used}}

    def close(self):
        if not self._closed:
            self._closed = True
            if self.handler:
                self.logger.removeHandler(self.handler)
                try:
                    self.handler.close()
                except (OSError, ValueError):
                    # A broken disk/stream must not prevent application shutdown.
                    pass

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            try:
                return await self.app(scope, receive, send)
            finally:
                self.close()
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope.get('app') is not None:
            scope['app'].state.request_logger = self
        if scope.get('path') == '/admin/api/logs':
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        status = None
        first_byte = None
        completed = False
        disconnected = False
        error_type = None
        active = {'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'request_id': request_id, 'method': scope.get('method') if scope.get('method') in _METHODS else 'OTHER',
                  'path': _safe_path(scope.get('path', '')), 'status': None, 'first_byte_ms': None,
                  'completed': False, 'disconnected': False, 'error_type': None, 'in_flight': True}
        if len(self.active) < 256:
            self.active[request_id] = (started, active)

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status, first_byte, completed
            if message['type'] == 'http.response.start':
                message = dict(message)
                headers = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'x-request-id']
                message['headers'] = headers + [(b'x-request-id', request_id.encode('ascii'))]
            await send(message)
            if message['type'] == 'http.response.start':
                value = message.get('status')
                status = value if isinstance(value, int) and 100 <= value <= 599 else None
                active['status'] = status
            elif message['type'] == 'http.response.body':
                if message.get('body') and first_byte is None:
                    first_byte = time.monotonic()
                    active['first_byte_ms'] = round((first_byte - started) * 1000, 3)
                if not message.get('more_body', False):
                    completed = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            disconnected = True
            error_type = 'CancelledError'
            raise
        except OSError:
            disconnected = True
            error_type = 'TransportError'
            raise
        except BaseException:
            # Never use exception strings or dynamic exception class names.
            error_type = 'ApplicationError'
            raise
        finally:
            self.active.pop(request_id, None)
            if disconnected and error_type is None:
                error_type = 'ClientDisconnect'
            elif not completed and error_type is None:
                error_type = 'IncompleteResponse'
            if self.handler and not self._closed:
                elapsed = lambda value: round(min(max(value - started, 0), 1e9) * 1000, 3)
                row = {
                    'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                    'request_id': request_id,
                    'method': scope.get('method') if scope.get('method') in _METHODS else 'OTHER',
                    'path': _safe_path(scope.get('path', '')),
                    'status': status,
                    'duration_ms': elapsed(time.monotonic()),
                    'first_byte_ms': elapsed(first_byte) if first_byte is not None else None,
                    'completed': completed and error_type is None and not disconnected,
                    'disconnected': disconnected,
                    'error_type': error_type,
                }
                code = scope.get('state', {}).get('gateway_error_code')
                if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,64}', code):
                    row['gateway_error_code'] = code
                    row['completed'] = False
                # All values are bounded/controlled; records stay below the 1024-byte floor.
                self.logger.info(json.dumps(row, ensure_ascii=True, separators=(',', ':')))
