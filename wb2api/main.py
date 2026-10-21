from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException

from .tool_compat import ToolAdapter, ToolProtocolError
from .reasoning import ReasoningEffort, effort_hint, GATEWAY_PROMPT, neutralize_brand, neutral_text

from .observability import RequestLogMiddleware
from .streaming import with_heartbeats
from .store import Store
from .media import Media
from .wabi import UpstreamError, Wabi

VERSION = '0.1.0'
DEFAULT_PROTOCOL = {
    'event_type_path': 'type',
    'turn_id_path': 'turn_id',
    'thread_id_path': 'thread_id',
    'text_path': 'delta.text',
    'delta_events': ['message_delta'],
    'done_events': ['message_finished'],
    'error_events': ['turn_error', 'message_error'],
    'mode': 'delta',
}
DEFAULT_FIREBASE_API_KEY = 'AIzaSyBqAGBGJrs9ut-Mc1k8f6x8tdD_e-ZDIDI'
STATIC = Path(__file__).parent / 'static'


def error(message: str, status: int, code: str):
    return JSONResponse({'error': {'message': message, 'type': 'wb2api_error', 'code': code}}, status_code=status)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Protocol(Input):
    event_type_path: str = Field(default='type', min_length=1, max_length=200)
    turn_id_path: str = Field(default='turn_id', min_length=1, max_length=200)
    thread_id_path: str = Field(default='thread_id', min_length=1, max_length=200)
    text_path: str = Field(default='delta.text', min_length=1, max_length=200)
    delta_events: list[str] = Field(default_factory=list, max_length=30)
    done_events: list[str] = Field(default_factory=list, max_length=30)
    error_events: list[str] = Field(default_factory=list, max_length=30)
    mode: str = Field(default='delta', pattern='^(delta|snapshot)$')


class AccountInput(Input):
    label: str = Field(min_length=1, max_length=100)
    firebase_api_key: str = Field(default='', max_length=1024)
    refresh_token: str = Field(default='', max_length=20000)
    id_token: str = Field(default='', max_length=20000)
    user_id: str = Field(default='', max_length=200)
    phone_number: str = Field(default='', max_length=50)


class Toggle(Input):
    enabled: bool


class Phone(Input):
    phone_number: str = Field(pattern=r'^\+[1-9]\d{6,14}$')


class CheckCode(Phone):
    code: str = Field(min_length=4, max_length=12)
    firebase_api_key: str = Field(default='', max_length=1024)
    label: str = Field(default='Phone Account', max_length=100)
    account_id: str = Field(default='', max_length=100)


class GoogleVerify(Input):
    phone_number: str = Field(default='', max_length=50)
    state: str = Field(default='', max_length=1000)
    code: str = Field(min_length=1, max_length=5000)
    firebase_api_key: str = Field(default='', max_length=1024)
    label: str = Field(default='', max_length=100)
    account_id: str = Field(default='', max_length=100)


class MailboxVerify(Input):
    phone_number: str = Field(default='', max_length=50)
    state: str = Field(default='', max_length=1000)
    code: str = Field(min_length=1, max_length=50)
    firebase_api_key: str = Field(default='', max_length=1024)
    label: str = Field(default='', max_length=100)
    account_id: str = Field(default='', max_length=100)


class MailboxResend(Input):
    phone_number: str = Field(default='', max_length=50)
    state: str = Field(default='', max_length=1000)


class ApiKeyInput(Input):
    name: str = Field(min_length=1, max_length=100)
    key: str = Field(default='', max_length=200)


class ApiKeyUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None

    @model_validator(mode='after')
    def require_update(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError('Provide a non-null name or enabled value')
        return self


class CallFunction(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    arguments: str = Field(max_length=200000)


class ToolCall(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    id: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern='^function$')
    function: CallFunction


class Message(Input):
    # Preserve prompt whitespace, unlike control-plane fields.
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    role: str = Field(pattern='^(system|developer|user|assistant|tool)$')
    name: str | None = Field(default=None, max_length=64)
    content: str | list[dict] | None = Field(default=None, max_length=200000)
    tool_calls: list[ToolCall] | None = Field(default=None, min_length=1, max_length=64)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def valid_shape(self):
        if isinstance(self.content, list):
            if len(self.content) > 256 or any(part.get('type') != 'text' or not isinstance(part.get('text'), str) or set(part) != {'type', 'text'} for part in self.content):
                raise ValueError('Only text content parts are supported')
            self.content = ''.join(part['text'] for part in self.content)
            if len(self.content) > 200000:
                raise ValueError('Message content exceeds 200000 characters')
        if self.role != 'assistant' and self.tool_calls is not None:
            raise ValueError('Only assistant messages may contain tool_calls')
        if (self.role == 'tool') != (self.tool_call_id is not None):
            raise ValueError('Tool messages require tool_call_id')
        if self.content is None and self.role != 'assistant':
            raise ValueError('Message content is required')
        return self


class Completion(Input):
    model: str = 'wabi'
    messages: list[Message] = Field(min_length=1, max_length=200)
    stream: bool = False
    tools: list[dict] | None = Field(default=None, max_length=64)
    tool_choice: str | dict | None = None
    parallel_tool_calls: bool = True
    reasoning_effort: ReasoningEffort | None = None
    # Client compatibility only: the upstream application exposes no sampling/budget controls.
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    user: str | None = Field(default=None, max_length=256)
    stream_options: dict | None = None
    stop: str | list[str] | None = None
    response_format: dict | None = None
    reasoning: dict | None = None
    store: bool | None = None
    metadata: dict[str, str] | None = None
    n: int = Field(default=1, ge=1, le=1)

    @model_validator(mode='after')
    def client_shapes(self):
        if self.reasoning is not None:
            if set(self.reasoning) - {'effort'} or self.reasoning.get('effort') not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                raise ValueError('reasoning supports only a valid effort')
            if self.reasoning_effort is not None and self.reasoning_effort != self.reasoning['effort']:
                raise ValueError('Conflicting reasoning effort fields')
            self.reasoning_effort = self.reasoning['effort']
        if self.response_format is not None and self.response_format != {'type': 'text'}:
            raise ValueError('Only text response_format is currently supported')
        if self.stream_options is not None and (set(self.stream_options) - {'include_usage'} or ('include_usage' in self.stream_options and type(self.stream_options['include_usage']) is not bool)):
            raise ValueError('stream_options supports only boolean include_usage')
        return self


class ImageGeneration(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    model: str = 'wabi-image'
    prompt: str = Field(min_length=1, max_length=200000)
    n: int = Field(default=1, strict=True, ge=1, le=1)
    response_format: str = Field(default='url', pattern='^url$')


def configured(protocol: dict) -> bool:
    return bool(protocol.get('delta_events') and protocol.get('done_events'))


def jwt_exp(token: str) -> float:
    """Scheduling hint only. Authentication remains the upstream's responsibility."""
    try:
        payload = token.split('.')[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('exp', 0))
    except (ValueError, IndexError, TypeError, KeyError):
        return 0


def create_app(overrides: dict | None = None) -> FastAPI:
    config = {
        'data_dir': os.getenv('WB2API_DATA_DIR', '/data'),
        'admin_key': os.getenv('WB2API_ADMIN_KEY', ''),
        'api_key': os.getenv('WB2API_API_KEY', ''),
        'encryption_key': os.getenv('WB2API_ENCRYPTION_KEY'),
        'base_url': os.getenv('WABI_BASE_URL', 'https://app.getwabi.ai').rstrip('/'),
        'request_timeout': float(os.getenv('WABI_REQUEST_TIMEOUT', '120')),
        'log_max_bytes': int(os.getenv('WB2API_LOG_MAX_BYTES', '20971520')),
        'log_backups': int(os.getenv('WB2API_LOG_BACKUPS', '3')),
        'timezone': os.getenv('WABI_TIME_ZONE', 'Asia/Shanghai'),
        'app_version': '2.0.2', 'app_build': '1010',
    }
    config.update(overrides or {})

    @asynccontextmanager
    async def lifespan(app):
        if min(len(config['admin_key']), len(config['api_key'])) < 8:
            raise RuntimeError('WB2API_ADMIN_KEY and WB2API_API_KEY must each be at least 8 characters')
        if hmac.compare_digest(config['admin_key'], config['api_key']):
            raise RuntimeError('Management and inference keys must differ')
        app.state.store = Store(config['data_dir'], config['encryption_key'])
        app.state.wabi = Wabi(app.state.store, config)
        app.state.media = Media(app.state.wabi)
        app.state.logins = {}
        app.state.counter = 0
        app.state.login_lock = asyncio.Lock()
        yield
        await app.state.wabi.close()
        app.state.store.close()

    app = FastAPI(title='WB2Api', version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(RequestLogMiddleware, data_dir=config['data_dir'],
                       max_bytes=config['log_max_bytes'], backups=config['log_backups'])

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        # Reject oversized/chunked JSON bodies before parsing; Starlette otherwise buffers them.
        if request.method in {'POST', 'PUT', 'PATCH'}:
            length = request.headers.get('content-length', '')
            if not length.isdigit() or int(length) > 1048576:
                return error('JSON requests require Content-Length <= 1 MiB', 413, 'body_too_large')
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def authorize(request: Request, key: str):
        expected = config[key]
        token = request.headers.get('authorization', '')
        if not expected or not hmac.compare_digest(token.encode(), ('Bearer ' + expected).encode()):
            raise HTTPException(401, 'Invalid bearer key')

    def admin(request: Request):
        authorize(request, 'admin_key')

    def inference(request: Request):
        expected = config.get('api_key', '')
        raw = request.headers.get('authorization', '')
        token = raw.removeprefix('Bearer ').strip() if raw.startswith('Bearer ') else ''
        if not token:
            raise HTTPException(401, 'Invalid bearer key')
        if expected and hmac.compare_digest(token.encode(), expected.encode()):
            return
        if app.state.store.verify_api_key(token):
            return
        raise HTTPException(401, 'Invalid bearer key')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(str(exc.detail), exc.status_code, 'http_error')

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo raw validation input, which can contain passwords/tokens.
        fields = ['.'.join(map(str, item['loc'])) for item in exc.errors()]
        return error('Invalid or unsupported fields: ' + ', '.join(fields), 422, 'invalid_request')

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        request.state.gateway_error_code = exc.code
        return error(str(exc), getattr(exc, 'status', 502), getattr(exc, 'code', 'upstream_error'))

    @app.exception_handler(ToolProtocolError)
    async def tool_error(request, exc):
        request.state.gateway_error_code = exc.code
        return error(str(exc), exc.status, exc.code)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return error('Account not found', 404, 'not_found')

    @app.get('/healthz')
    async def health():
        return {'status': 'ok', 'version': VERSION}

    @app.get('/', include_in_schema=False)
    async def index():
        return FileResponse(STATIC / 'index.html')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/admin/api/status', dependencies=[Depends(admin)])
    async def status():
        protocol = json.loads(app.state.store.get_setting('protocol', json.dumps(DEFAULT_PROTOCOL)))
        return {'version': VERSION, 'accounts': app.state.store.list_accounts(),
                'protocol_configured': configured(protocol), 'base_url': config['base_url'],
                'request_timeout': config['request_timeout']}

    @app.get('/admin/api/protocol', dependencies=[Depends(admin)])
    async def get_protocol():
        return json.loads(app.state.store.get_setting('protocol', json.dumps(DEFAULT_PROTOCOL)))

    @app.put('/admin/api/protocol', dependencies=[Depends(admin)])
    async def put_protocol(body: Protocol):
        data = body.model_dump()
        if any(any(not part for part in data[name].split('.')) for name in ('event_type_path', 'turn_id_path', 'thread_id_path', 'text_path')):
            raise HTTPException(422, 'Field paths must have nonempty dot-separated segments')
        groups = [set(data[name]) for name in ('delta_events', 'done_events', 'error_events')]
        if any(not item.strip() or len(item) > 200 for group in groups for item in group):
            raise HTTPException(422, 'Event names must be nonempty and <= 200 characters')
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise HTTPException(422, 'Delta, done and error event names must be distinct')
        app.state.store.set_setting('protocol', json.dumps(data))
        return data

    @app.post('/admin/api/accounts', dependencies=[Depends(admin)])
    async def add_account(body: AccountInput):
        if not body.refresh_token and not body.id_token:
            raise HTTPException(422, 'Provide a refresh token or ID token')
        firebase_key = body.firebase_api_key or DEFAULT_FIREBASE_API_KEY
        payload = body.model_dump()
        payload['firebase_api_key'] = firebase_key
        return app.state.store.add_account(**payload, expires_at=jwt_exp(body.id_token))

    async def reverify_credentials(account_id: str, **credentials):
        app.state.store.update_account_credentials(account_id, **credentials)
        try:
            await app.state.wabi.validate_account(account_id)
        except UpstreamError as exc:
            result = app.state.store.public(app.state.store.get_account(account_id))
            return {**result, 'verification_pending': True, 'verification_code': exc.code}
        return app.state.store.public(app.state.store.get_account(account_id))

    @app.put('/admin/api/accounts/{account_id}/reauth', dependencies=[Depends(admin)])
    async def reauth_account(account_id: str, body: AccountInput):
        if not body.refresh_token and not body.id_token:
            raise HTTPException(422, 'Provide a refresh token or ID token')
        firebase_key = body.firebase_api_key or DEFAULT_FIREBASE_API_KEY
        return await reverify_credentials(
            account_id,
            id_token=body.id_token,
            refresh_token=body.refresh_token,
            expires_at=jwt_exp(body.id_token),
            firebase_api_key=firebase_key,
            phone_number=body.phone_number
        )

    @app.patch('/admin/api/accounts/{account_id}', dependencies=[Depends(admin)])
    async def toggle(account_id: str, body: Toggle):
        return app.state.store.set_enabled(account_id, body.enabled)

    @app.delete('/admin/api/accounts/{account_id}', dependencies=[Depends(admin)])
    async def remove_account(account_id: str):
        app.state.store.delete_account(account_id)
        return {'ok': True}

    @app.get('/admin/api/accounts/export', dependencies=[Depends(admin)])
    async def export_accounts():
        return app.state.store.export_accounts()

    @app.get('/admin/api/keys', dependencies=[Depends(admin)])
    async def list_keys():
        return app.state.store.list_api_keys()

    @app.post('/admin/api/keys', dependencies=[Depends(admin)])
    async def create_key(body: ApiKeyInput):
        return app.state.store.add_api_key(body.name, body.key)

    @app.patch('/admin/api/keys/{key_id}', dependencies=[Depends(admin)])
    async def update_key(key_id: str, body: ApiKeyUpdate):
        return app.state.store.update_api_key(key_id, **body.model_dump(exclude_unset=True))

    @app.delete('/admin/api/keys/{key_id}', dependencies=[Depends(admin)])
    async def delete_key(key_id: str):
        app.state.store.delete_api_key(key_id)
        return {'ok': True}

    @app.post('/admin/api/accounts/{account_id}/check', dependencies=[Depends(admin)])
    async def check(account_id: str):
        try:
            profile = await app.state.wabi.validate_account(account_id)
            return {'ok': True, 'profile': {'authenticated': True}}
        except UpstreamError as exc:
            return {'ok': False, 'status': exc.status, 'error': exc.message, 'code': exc.code}
        except Exception as exc:
            return {'ok': False, 'status': 500, 'error': str(exc), 'code': 'check_failed'}

    @app.post('/admin/api/auth/send-code', dependencies=[Depends(admin)])
    async def send_code(body: Phone):
        async with app.state.login_lock:
            now = time.time()
            app.state.logins = {k: v for k, v in app.state.logins.items() if v['expires'] > now}
            existing = app.state.logins.get(body.phone_number)
            if existing and existing['sent_at'] + 60 > now:
                raise HTTPException(429, 'Wait 60 seconds before requesting another code')
            if len(app.state.logins) >= 100:
                raise HTTPException(429, 'Too many pending login attempts')
            await app.state.wabi.send_code(body.phone_number)
            app.state.logins[body.phone_number] = {'verifier': secrets.token_urlsafe(64),
                                                  'expires': now + 600, 'sent_at': now, 'attempts': 0}
        return {'ok': True}

    @app.post('/admin/api/auth/check-code', dependencies=[Depends(admin)])
    async def check_code(body: CheckCode):
        async with app.state.login_lock:
            pending = app.state.logins.get(body.phone_number)
            if not pending or pending['expires'] < time.time():
                raise HTTPException(400, 'Request a new code first')
            if pending['attempts'] >= 5:
                raise HTTPException(429, 'Too many verification attempts; request a new code')
            pending['attempts'] += 1
            result = await app.state.wabi.check_code(body.phone_number, body.code, pending['verifier'])
            if result.get('google_required'):
                pending['google_state'] = result.get('google_state', '')
                pending['label'] = body.label
                pending['account_id'] = body.account_id
                pending['firebase_api_key'] = body.firebase_api_key
                return {
                    'ok': True,
                    'requires_google': True,
                    'google_url': result.get('google_url', ''),
                    'google_state': result.get('google_state', ''),
                    'phone_number': body.phone_number,
                    'label': body.label,
                    'account_id': body.account_id,
                }
            if result.get('mailbox_code_required'):
                pending['mailbox_state'] = result.get('mailbox_state', '')
                pending['label'] = body.label
                pending['account_id'] = body.account_id
                pending['firebase_api_key'] = body.firebase_api_key
                return {
                    'ok': True,
                    'requires_mailbox': True,
                    'mailbox_state': result.get('mailbox_state', ''),
                    'mailbox_hint': result.get('mailbox_hint', ''),
                    'retry_after_seconds': result.get('retry_after_seconds', 60),
                    'phone_number': body.phone_number,
                    'label': body.label,
                    'account_id': body.account_id,
                }
            token = result.get('firebase_custom_token')
            if not token:
                raise UpstreamError('Upstream returned no custom token')
            firebase_key = body.firebase_api_key or DEFAULT_FIREBASE_API_KEY
            tokens = await app.state.wabi.exchange_custom(token, firebase_key)
            if body.account_id:
                account = await reverify_credentials(
                    body.account_id,
                    id_token=tokens['id_token'],
                    refresh_token=tokens['refresh_token'],
                    expires_at=tokens['expires_at'],
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number
                )
            else:
                account = app.state.store.add_account(
                    label=body.label,
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number,
                    **tokens
                )
            app.state.logins.pop(body.phone_number, None)
            return {'ok': True, 'account': account}

    @app.post('/admin/api/auth/google-verify', dependencies=[Depends(admin)])
    async def google_verify(body: GoogleVerify):
        async with app.state.login_lock:
            pending = None
            if body.phone_number:
                pending = app.state.logins.get(body.phone_number)
            if not pending and body.state:
                for v in app.state.logins.values():
                    if v.get('google_state') == body.state:
                        pending = v
                        break
            if not pending or pending['expires'] < time.time():
                raise HTTPException(400, 'Verification session expired; please request SMS code again')
            raw_code = body.code.strip()
            state = body.state.strip() or pending.get('google_state', '')
            code = raw_code
            if 'code=' in raw_code or 'state=' in raw_code:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(raw_code if '://' in raw_code else 'http://localhost/' + raw_code.lstrip('/?'))
                qs = parse_qs(parsed.query or parsed.fragment)
                if 'code' in qs:
                    code = qs['code'][0]
                if 'state' in qs and not state:
                    state = qs['state'][0]
            result = await app.state.wabi.google_verify(state, code, pending['verifier'])
            token = result.get('firebase_custom_token')
            if not token:
                raise UpstreamError('Google verification did not return a session token')
            firebase_key = body.firebase_api_key or pending.get('firebase_api_key') or DEFAULT_FIREBASE_API_KEY
            tokens = await app.state.wabi.exchange_custom(token, firebase_key)
            label = body.label or pending.get('label') or 'Google Phone Account'
            account_id = body.account_id or pending.get('account_id')
            if account_id:
                account = await reverify_credentials(
                    account_id,
                    id_token=tokens['id_token'],
                    refresh_token=tokens['refresh_token'],
                    expires_at=tokens['expires_at'],
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number
                )
            else:
                account = app.state.store.add_account(
                    label=label,
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number,
                    **tokens
                )
            if body.phone_number:
                app.state.logins.pop(body.phone_number, None)
            return {'ok': True, 'account': account}

    @app.post('/admin/api/auth/mailbox-verify', dependencies=[Depends(admin)])
    async def mailbox_verify(body: MailboxVerify):
        async with app.state.login_lock:
            pending = None
            if body.phone_number:
                pending = app.state.logins.get(body.phone_number)
            if not pending and body.state:
                for v in app.state.logins.values():
                    if v.get('mailbox_state') == body.state:
                        pending = v
                        break
            if not pending or pending['expires'] < time.time():
                raise HTTPException(400, 'Verification session expired; please request SMS code again')
            code = body.code.strip()
            state = body.state.strip() or pending.get('mailbox_state', '')
            result = await app.state.wabi.mailbox_code(state, code, pending['verifier'])
            token = result.get('firebase_custom_token')
            if not token:
                raise UpstreamError('Mailbox verification did not return a session token')
            firebase_key = body.firebase_api_key or pending.get('firebase_api_key') or DEFAULT_FIREBASE_API_KEY
            tokens = await app.state.wabi.exchange_custom(token, firebase_key)
            label = body.label or pending.get('label') or 'Phone Account'
            account_id = body.account_id or pending.get('account_id')
            if account_id:
                account = await reverify_credentials(
                    account_id,
                    id_token=tokens['id_token'],
                    refresh_token=tokens['refresh_token'],
                    expires_at=tokens['expires_at'],
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number
                )
            else:
                account = app.state.store.add_account(
                    label=label,
                    firebase_api_key=firebase_key,
                    phone_number=body.phone_number,
                    **tokens
                )
            if body.phone_number:
                app.state.logins.pop(body.phone_number, None)
            return {'ok': True, 'account': account}

    @app.post('/admin/api/auth/mailbox-resend', dependencies=[Depends(admin)])
    async def mailbox_resend(body: MailboxResend):
        async with app.state.login_lock:
            pending = None
            if body.phone_number:
                pending = app.state.logins.get(body.phone_number)
            if not pending and body.state:
                for v in app.state.logins.values():
                    if v.get('mailbox_state') == body.state:
                        pending = v
                        break
            if not pending or pending['expires'] < time.time():
                raise HTTPException(400, 'Verification session expired; please request SMS code again')
            state = body.state.strip() or pending.get('mailbox_state', '')
            await app.state.wabi.mailbox_code_resend(state)
            return {'ok': True}

    @app.get('/v1/models', dependencies=[Depends(inference)])
    async def models():
        return {
            'object': 'list',
            'data': [
                {'id': 'wabi', 'object': 'model', 'created': 0, 'owned_by': 'wabi',
                 'capability': 'chat', 'capability_alias': True, 'available': True},
                {'id': 'wabi-image', 'object': 'model', 'created': 0, 'owned_by': 'wabi',
                 'capability': 'image', 'capability_alias': True, 'available': True},
                {'id': 'wabi-video', 'object': 'model', 'created': 0, 'owned_by': 'wabi',
                 'capability': 'video', 'capability_alias': True, 'available': False,
                 'experimental': True, 'unavailable_reason': 'video_not_verified'},
            ]
        }

    def select_account():
        accounts = [account for account in app.state.store.list_accounts() if account['ready']]
        if not accounts:
            raise UpstreamError('No enabled account with usable credentials', 503, 'no_account')
        account = accounts[app.state.counter % len(accounts)]
        app.state.counter += 1
        return account['id']

    def video_unavailable():
        return error('Video generation has not yet been verified through this adapter',
                     501, 'video_not_verified')

    @app.post('/v1/images/generations', dependencies=[Depends(inference)])
    async def images(body: ImageGeneration):
        if body.model == 'wabi-video':
            return video_unavailable()
        if body.model != 'wabi-image':
            return error('Image generation requires the wabi-image capability alias', 400, 'model_not_found')
        urls = await app.state.media.generate_image(select_account(), body.prompt)
        return {'created': int(time.time()), 'data': [{'url': url} for url in urls]}

    @app.post('/v1/chat/completions', dependencies=[Depends(inference)])
    async def completion(body: Completion, request: Request):
        if body.model == 'wabi-video':
            return video_unavailable()
        if body.model == 'wabi-image':
            if body.reasoning_effort is not None:
                return error('Prompt effort control is supported for text chat only', 400, 'unsupported_media_reasoning')
            if (body.tools is not None or body.tool_choice is not None
                    or 'parallel_tool_calls' in body.model_fields_set
                    or any(message.role == 'tool' or message.tool_calls for message in body.messages)):
                return error('Image generation cannot be combined with client tools', 400, 'unsupported_media_tools')
            prompt = ToolAdapter().history_prompt(body.messages)
            if len(prompt) > 200000:
                return error('Combined prompt exceeds 200000 characters', 413, 'body_too_large')
            urls = await app.state.media.generate_image(select_account(), prompt)
            content = '\n'.join('![Generated image](<' + url + '>)' for url in urls)
            identity, created = 'chatcmpl-' + uuid.uuid4().hex, int(time.time())
            if not body.stream:
                return {'id': identity, 'object': 'chat.completion', 'created': created, 'model': 'wabi-image',
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}

            async def media_events():
                for delta, finish in [({'role': 'assistant'}, None), ({'content': content}, None), ({}, 'stop')]:
                    chunk = {'id': identity, 'object': 'chat.completion.chunk', 'created': created, 'model': 'wabi-image',
                             'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    yield 'data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(media_events(), media_type='text/event-stream', headers={
                'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache', 'X-WB2Api-Media-Stream': 'buffered'})
        if body.model not in ('wabi', 'default', 'sabi'):
            return error('Model not supported; use wabi, wabi-image or wabi-video (unavailable)', 400, 'model_not_found')
        adapter = ToolAdapter(body.tools, body.tool_choice, body.parallel_tool_calls)
        prompt = GATEWAY_PROMPT + effort_hint(body.reasoning_effort) + adapter.history_prompt(body.messages)
        reasoning_headers = {'X-WB2Api-Reasoning-Mode': 'prompt', 'X-WB2Api-Reasoning-Effort': body.reasoning_effort} if body.reasoning_effort is not None else {}
        ignored = sorted(field for field in ('temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'frequency_penalty', 'presence_penalty', 'seed', 'stream_options', 'stop', 'store', 'metadata') if getattr(body, field) is not None)
        if ignored:
            reasoning_headers['X-WB2Api-Ignored-Parameters'] = ','.join(ignored)
        tool_mode = bool(body.tools) or body.tool_choice is not None or any(
            message.role == 'tool' or message.tool_calls for message in body.messages)
        if tool_mode:
            prompt = adapter.instruction() + '<client_transcript>\n' + prompt + '\n</client_transcript>\nProduce the next external client response now. For an action, output the RPC envelope, not a promise.'
        if len(prompt) > 200000:
            return error('Combined prompt exceeds 200000 characters', 413, 'body_too_large')
        protocol = json.loads(app.state.store.get_setting('protocol', json.dumps(DEFAULT_PROTOCOL)))
        if not configured(protocol):
            return error('Configure the verified Wabi SSE event mapping in the management console first', 501, 'protocol_not_configured')
        accounts = [account for account in app.state.store.list_accounts() if account['ready']]
        if not accounts:
            return error('No enabled account with usable credentials', 503, 'no_account')
        account = accounts[app.state.counter % len(accounts)]
        app.state.counter += 1
        iterator = app.state.wabi.stream_chat(account['id'], prompt, protocol)
        if not tool_mode:
            iterator = neutral_text(iterator)
        completion_id = 'chatcmpl-' + uuid.uuid4().hex
        created = int(time.time())

        def chunk(delta: dict, finish=None):
            return {'id': completion_id, 'object': 'chat.completion.chunk', 'created': created,
                    'model': 'wabi', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

        async def collect_message():
            content = ''.join([text async for text in iterator])
            if not content:
                raise UpstreamError('Upstream completed without text', 502, 'empty_response')
            message, finish = adapter.parse(content) if tool_mode else ({'role': 'assistant', 'content': content}, 'stop')
            if tool_mode and message.get('content'):
                message['content'] = neutralize_brand(message['content'])
            return message, finish

        if not body.stream:
            try:
                message, finish = await collect_message()
                return JSONResponse({'id': completion_id, 'object': 'chat.completion', 'created': created, 'model': 'wabi',
                        'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]}, headers=reasoning_headers)
            finally:
                await iterator.aclose()

        async def events():
            def encode(data):
                return 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
            try:
                yield encode(chunk({'role': 'assistant'}))
                if tool_mode:
                    message, finish = await collect_message()
                    if finish == 'tool_calls':
                        for index, call in enumerate(message['tool_calls']):
                            yield encode(chunk({'tool_calls': [{'index': index, **call}]}))
                    else:
                        yield encode(chunk({'content': message['content']}))
                    yield encode(chunk({}, finish))
                    yield 'data: [DONE]\n\n'
                else:
                    seen = False
                    async for text in iterator:
                        seen = True
                        yield encode(chunk({'content': text}))
                    if not seen:
                        raise UpstreamError('Upstream completed without text', 502, 'empty_response')
                    yield encode(chunk({}, 'stop'))
                    yield 'data: [DONE]\n\n'
            except (UpstreamError, ToolProtocolError) as exc:
                request.state.gateway_error_code = exc.code
                yield encode({'error': {'message': str(exc), 'type': 'upstream_error', 'code': getattr(exc, 'code', 'upstream_error')}})
                # A failed stream has no successful finish_reason or [DONE].
            finally:
                await iterator.aclose()
        headers = {'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache', **reasoning_headers}
        if tool_mode:
            headers['X-WB2Api-Tool-Stream'] = 'buffered'
        return StreamingResponse(with_heartbeats(events()), media_type='text/event-stream', headers=headers)

    return app


app = create_app()
