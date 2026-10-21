"""Evidence-based Wabi HTTP authentication and one-shot POST + SSE chat.

No request is retried, especially message POSTs. A disconnect is an explicit
failure: event-position resumption/Last-Event-ID is deliberately not implemented.
Store methods may be synchronous or asynchronous; token expiry is epoch seconds.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
import base64
import codecs
import hashlib
import inspect
import json
import math
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

import httpx

from .protocol import ProtocolError, ProtocolMapper, parse_sse


class UpstreamError(Exception):
    def __init__(
        self, message: str, status: int = 502, code: str = "upstream_error"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class Wabi:
    def __init__(self, store: Any, config: dict[str, Any]) -> None:
        self.store = store
        self.config = dict(config)
        self.timeout = self._positive_float(config.get("request_timeout", 120), "request_timeout")
        self.max_event_bytes = self._positive_int(config.get("max_sse_event_bytes", 1024 * 1024), "max_sse_event_bytes")
        self.max_text_bytes = self._positive_int(config.get("max_response_text_bytes", 8 * 1024 * 1024), "max_response_text_bytes")
        self.queue_size = self._positive_int(config.get("sse_queue_size", 64), "sse_queue_size")
        self.max_json_bytes = self._positive_int(config.get("max_json_bytes", 1024 * 1024), "max_json_bytes")
        base_url = config.get("base_url", "https://app.getwabi.ai")
        if not isinstance(base_url, str):
            raise ValueError("base_url must be a root HTTP(S) URL")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be a root HTTP(S) URL without credentials")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, trust_env=False
        )
        # Refcount includes waiters, so a released lock is never replaced while
        # another caller is waiting. Entries disappear when their last user exits.
        self._locks: dict[Any, tuple[asyncio.Lock, int]] = {}
        self._readers: set[asyncio.Task[None]] = set()
        self._closed = False

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _positive_float(value: Any, name: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be positive and finite") from exc
        if isinstance(value, bool) or not math.isfinite(result) or result <= 0:
            raise ValueError(f"{name} must be positive and finite")
        return result

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._readers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise UpstreamError("Wabi client is closed", 503, "client_closed")

    def _headers(self, account: dict[str, Any] | None = None, *, sse: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if sse else "application/json",
            "User-Agent": "okhttp/4.12.0",
            "X-Device-Type": "android",
            "X-Client-Features": "large_attachments,poll_card,mailbox_code,google_product_cards",
            "X-App-Version": str(self.config.get("app_version", "2.0.2")),
            "X-App-Build": str(self.config.get("app_build", "1010")),
            "X-Time-Zone": str(self.config.get("timezone", "UTC")),
        }
        if account:
            headers["Authorization"] = "Bearer " + account["id_token"]
            if account.get("user_id"):
                headers["X-User-Id"] = str(account["user_id"])
            if account.get("device_id"):
                headers["X-Device-Id"] = str(account["device_id"])
        return headers

    @staticmethod
    def _http_error(status: int) -> UpstreamError:
        if status in (401, 403):
            return UpstreamError("Upstream authentication was rejected; request was not retried", status, "upstream_authentication_failed")
        if status == 429:
            return UpstreamError("Upstream rate limit reached", 429, "upstream_rate_limited")
        return UpstreamError(f"Upstream returned HTTP {status}; request was not retried", status if 400 <= status <= 599 else 502, "upstream_http_error")

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self._ensure_open()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(method, url, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise self._http_error(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > self.max_json_bytes:
                            raise UpstreamError("Upstream JSON exceeds the size limit", code="upstream_response_too_large")
                    # Treat an empty 204 as an empty object, useful for send-code.
                    if response.status_code == 204 and not body:
                        return {}
                    try:
                        data = json.loads(body)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise UpstreamError("Upstream returned invalid JSON", code="invalid_upstream_response") from exc
                    if not isinstance(data, dict):
                        raise UpstreamError("Upstream JSON must be an object", code="invalid_upstream_response")
                    return data
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError("Upstream request timed out; request was not retried", 504, "upstream_timeout") from exc
        except httpx.HTTPError as exc:
            # Never expose exception strings: Firebase URLs contain API keys.
            raise UpstreamError("Upstream transport failed; request was not retried", code="upstream_transport_error") from exc

    async def send_code(self, phone: str) -> dict[str, Any]:
        return await self._json(
            "POST", self.base_url + "/api/v1/auth/phone/send-code",
            headers=self._headers(), json={"phone_number": phone},
        )

    async def check_code(self, phone: str, code: str, verifier: str) -> dict[str, Any]:
        # Preserve all challenge-response flags for the caller. Do not interpret
        # a successful SMS check as an already established Firebase session.
        if not isinstance(verifier, str) or not verifier:
            raise UpstreamError("A PKCE verifier is required", 400, "invalid_verifier")
        try:
            verifier_bytes = verifier.encode("ascii")
        except UnicodeError as exc:
            raise UpstreamError("PKCE verifier must be ASCII", 400, "invalid_verifier") from exc
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest()).rstrip(b"=").decode("ascii")
        return await self._json(
            "POST", self.base_url + "/api/v1/auth/phone/check-code",
            headers=self._headers(),
            json={"phone_number": phone, "code": code, "code_challenge": challenge},
        )

    async def google_verify(self, state: str, code: str, verifier: str) -> dict[str, Any]:
        if not isinstance(verifier, str) or not verifier:
            raise UpstreamError("A PKCE verifier is required", 400, "invalid_verifier")
        return await self._json(
            "POST", self.base_url + "/api/v1/auth/phone/google-verify",
            headers=self._headers(),
            json={"state": state, "code": code, "code_verifier": verifier},
        )

    async def mailbox_code(self, state: str, code: str, verifier: str) -> dict[str, Any]:
        if not isinstance(verifier, str) or not verifier:
            raise UpstreamError("A PKCE verifier is required", 400, "invalid_verifier")
        return await self._json(
            "POST", self.base_url + "/api/v1/auth/phone/mailbox-code",
            headers=self._headers(),
            json={"state": state, "code": code, "code_verifier": verifier},
        )

    async def mailbox_code_resend(self, state: str) -> dict[str, Any]:
        return await self._json(
            "POST", self.base_url + "/api/v1/auth/phone/mailbox-code/resend",
            headers=self._headers(),
            json={"state": state},
        )

    @staticmethod
    def _tokens(data: dict[str, Any], *, refresh: bool = False) -> dict[str, Any]:
        token_key = "id_token" if refresh else "idToken"
        refresh_key = "refresh_token" if refresh else "refreshToken"
        expiry_key = "expires_in" if refresh else "expiresIn"
        token, refresh_token = data.get(token_key), data.get(refresh_key)
        if not isinstance(token, str) or not token or not isinstance(refresh_token, str) or not refresh_token:
            raise UpstreamError("Firebase did not return session tokens", code="invalid_firebase_response")
        try:
            lifetime = float(data[expiry_key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise UpstreamError("Firebase returned invalid token expiry", code="invalid_firebase_response") from exc
        if isinstance(data[expiry_key], bool) or not math.isfinite(lifetime) or lifetime <= 0:
            raise UpstreamError("Firebase returned invalid token expiry", code="invalid_firebase_response")
        expires_at = time.time() + lifetime
        if not math.isfinite(expires_at):
            raise UpstreamError("Firebase returned invalid token expiry", code="invalid_firebase_response")
        return {"id_token": token, "refresh_token": refresh_token, "expires_at": expires_at}

    async def exchange_custom(self, token: str, firebase_api_key: str) -> dict[str, Any]:
        if not token or not firebase_api_key:
            raise UpstreamError("Custom token and Firebase API key are required", 400, "missing_credentials")
        data = await self._json(
            "POST", "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyCustomToken",
            params={"key": firebase_api_key}, json={"token": token, "returnSecureToken": True},
        )
        return self._tokens(data)

    @asynccontextmanager
    async def _account_lock(self, account_id: Any) -> AsyncIterator[None]:
        lock, users = self._locks.get(account_id, (asyncio.Lock(), 0))
        self._locks[account_id] = (lock, users + 1)
        acquired = False
        try:
            try:
                await asyncio.wait_for(lock.acquire(), self.timeout)
            except TimeoutError as exc:
                raise UpstreamError("Timed out waiting for this account", 504, "account_busy") from exc
            acquired = True
            self._ensure_open()
            yield
        finally:
            if acquired:
                lock.release()
            _, users = self._locks[account_id]
            if users == 1:
                del self._locks[account_id]
            else:
                self._locks[account_id] = (lock, users - 1)

    async def _account(self, account_id: Any) -> dict[str, Any]:
        account = await _resolve(self.store.get_account(account_id))
        if not account:
            raise UpstreamError("Account not found", 404, "account_not_found")
        account = dict(account)
        try:
            expires = float(account.get("expires_at") or 0)
        except (TypeError, ValueError, OverflowError):
            expires = 0
        if not account.get("id_token") or not math.isfinite(expires) or expires <= time.time() + 60:
            if not account.get("refresh_token") or not account.get("firebase_api_key"):
                raise UpstreamError("Account requires a refresh token and Firebase API key", 401, "account_requires_login")
            data = await self._json(
                "POST", "https://securetoken.googleapis.com/v1/token",
                params={"key": account["firebase_api_key"]},
                data={"grant_type": "refresh_token", "refresh_token": account["refresh_token"]},
            )
            tokens = self._tokens(data, refresh=True)
            await _resolve(self.store.update_tokens(account_id, tokens["id_token"], tokens["refresh_token"], tokens["expires_at"]))
            account.update(tokens)
        return account

    async def validate_account(self, account_id: Any) -> dict[str, Any]:
        async with self._account_lock(account_id):
            account = await self._account(account_id)
            data = await self._json("GET", self.base_url + "/api/v1/me", headers=self._headers(account))
            user_id = data.get("id")
            if user_id and not account.get("user_id"):
                await _resolve(self.store.update_user_id(account_id, user_id))
                account["user_id"] = user_id
            return {key: data[key] for key in ("id", "firebase_id", "username", "display_name") if key in data and isinstance(data[key], (str, type(None)))}

    async def _sse_lines(self, response: httpx.Response) -> AsyncIterator[str]:
        """Strict incremental UTF-8 with bounded buffering before line decoding."""
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        parts: list[str] = []
        line_bytes = 0
        swallow_lf = False
        # Do not set chunk_size: httpx would wait to fill it, withholding small
        # live events (and possibly deadlocking a low-volume response).
        async for chunk in response.aiter_bytes():
            try:
                text = decoder.decode(chunk)
            except UnicodeError as exc:
                raise UpstreamError("SSE contains invalid UTF-8", code="invalid_upstream_protocol") from exc
            for part in re.split(r"([\r\n])", text):
                if not part:
                    continue
                if swallow_lf:
                    swallow_lf = False
                    if part == "\n":
                        continue
                if part in ("\r", "\n"):
                    yield "".join(parts)
                    parts = []
                    line_bytes = 0
                    swallow_lf = part == "\r"
                else:
                    line_bytes += len(part.encode("utf-8"))
                    if line_bytes > self.max_event_bytes:
                        raise UpstreamError("SSE line exceeds the size limit", code="sse_event_too_large")
                    parts.append(part)
        try:
            decoder.decode(b"", final=True)
        except UnicodeError as exc:
            raise UpstreamError("SSE ended within a UTF-8 character", code="invalid_upstream_protocol") from exc
        if parts:
            yield "".join(parts)

    async def stream_chat(
        self, account_id: Any, prompt: str, protocol: dict[str, Any]
    ) -> AsyncIterator[str]:
        try:
            mapper = ProtocolMapper(protocol, max_text_bytes=self.max_text_bytes)
        except ProtocolError as exc:
            raise UpstreamError(exc.message, exc.status, exc.code) from exc
        if not isinstance(prompt, str) or not prompt:
            raise UpstreamError("A nonempty prompt is required", 400, "invalid_prompt")
        async with self._account_lock(account_id):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout

            async def bounded(awaitable: Awaitable[Any]) -> Any:
                # No asyncio.timeout context spans a yield to the consumer: such
                # a context could cancel unrelated caller work while suspended.
                return await asyncio.wait_for(awaitable, max(0, deadline - loop.time()))

            reader: asyncio.Task[None] | None = None
            try:
                account = await bounded(self._account(account_id))
                if not account.get("device_id"):
                    raise UpstreamError("Account has no persistent device ID", 400, "missing_device_id")
                queue: asyncio.Queue[dict[str, str] | UpstreamError] = asyncio.Queue(self.queue_size)
                ready: asyncio.Future[UpstreamError | None] = loop.create_future()
                terminal: asyncio.Future[UpstreamError] = loop.create_future()

                async def consume_events() -> None:
                    failure = UpstreamError("SSE disconnected before completion; resumption is not implemented and the message was not replayed", code="sse_disconnected")
                    try:
                        async with asyncio.timeout(max(0, deadline - loop.time())):
                            async with self.client.stream(
                                "GET", self.base_url + "/api/v1/chat/events",
                                headers=self._headers(account, sse=True),
                            ) as response:
                                if response.status_code != 200:
                                    raise self._http_error(response.status_code)
                                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                                if content_type != "text/event-stream":
                                    raise UpstreamError("Upstream did not provide an SSE response", code="invalid_upstream_protocol")
                                ready.set_result(None)
                                async for event in parse_sse(self._sse_lines(response), max_event_bytes=self.max_event_bytes):
                                    # Backpressure, not unbounded buffering or
                                    # event dropping, including before POST ack.
                                    await queue.put(event)
                    except asyncio.CancelledError:
                        raise
                    except ProtocolError as exc:
                        failure = UpstreamError(exc.message, exc.status, exc.code)
                    except UpstreamError as exc:
                        failure = exc
                    except (TimeoutError, httpx.TimeoutException):
                        failure = UpstreamError("SSE timed out; message was not replayed", 504, "upstream_timeout")
                    except httpx.HTTPError:
                        failure = UpstreamError("SSE transport failed; resumption is not implemented and the message was not replayed", code="sse_disconnected")
                    except Exception:
                        failure = UpstreamError("SSE reader failed; message was not replayed", code="invalid_upstream_protocol")
                    finally:
                        if not ready.done():
                            ready.set_result(failure)
                    if not terminal.done():
                        terminal.set_result(failure)
                    await queue.put(failure)

                reader = asyncio.create_task(consume_events(), name="wabi-sse-reader")
                self._readers.add(reader)
                failure = await bounded(asyncio.shield(ready))
                if failure:
                    raise failure
                if terminal.done():
                    raise terminal.result()
                # The stream is connected and independently consumed before any
                # thread/message POST. All early events stay in the bounded queue.
                thread = await bounded(self._json(
                    "POST", self.base_url + "/api/v1/threads", headers=self._headers(account),
                    json={"title": "WB2Api", "request_id": str(uuid.uuid4()), "kind": "group"},
                ))
                thread_id = (thread.get("thread") or {}).get("id") or (thread.get("state") or {}).get("thread_id") or thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise UpstreamError("Thread creation returned no id", code="invalid_upstream_response")
                if terminal.done():
                    raise terminal.result()
                accepted = await bounded(self._json(
                    "POST", self.base_url + "/api/v1/threads/" + quote(thread_id, safe="") + "/messages",
                    headers=self._headers(account),
                    json={
                        "client_id": str(uuid.uuid4()), "blocks": [{"type": "text", "text": prompt}],
                        "attachment_ids": [], "source": "typed", "page_context": {"app_surface": "chat_only"},
                    },
                ))
                turn_id = accepted.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise UpstreamError("Message acceptance returned no turn_id; message was not replayed", code="invalid_upstream_response")
                while True:
                    event = await bounded(queue.get())
                    if isinstance(event, UpstreamError):
                        raise event
                    try:
                        text, done = mapper.consume(event, thread_id, turn_id)
                    except ProtocolError as exc:
                        raise UpstreamError(exc.message, exc.status, exc.code) from exc
                    if text is not None:
                        yield text
                    if done:
                        return
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise UpstreamError("Chat request timed out; message was not replayed", 504, "upstream_timeout") from exc
            finally:
                if reader is not None:
                    reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader
                    self._readers.discard(reader)
