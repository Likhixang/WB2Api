"""Bounded SSE framing and an explicitly configured Wabi event mapping.

The APK evidence establishes the transport, not the event DTOs. No event names
here are claimed to be Wabi defaults. ``parse_sse`` only frames events; JSON is
validated by ``ProtocolMapper`` before anything is correlated or emitted.
"""

from collections.abc import AsyncIterable, AsyncIterator, Mapping
import json
from typing import Any


DEFAULT_MAX_EVENT_BYTES = 1024 * 1024
DEFAULT_MAX_TEXT_BYTES = 8 * 1024 * 1024
_MISSING = object()


class ProtocolError(ValueError):
    def __init__(
        self, message: str, code: str = "invalid_upstream_protocol", status: int = 502
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def extract_path(value: Any, path: str, default: Any = None) -> Any:
    """Resolve dot-separated dictionary keys only (never attributes or lists)."""
    if not isinstance(path, str) or not path:
        return default
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


async def parse_sse(
    lines: AsyncIterable[str], *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES
) -> AsyncIterator[dict[str, str]]:
    """Frame SSE lines into ``{event, data, id}`` dictionaries.

    Comments and unknown fields are ignored. Multiline data is joined with LF;
    a blank line dispatches a data-bearing event. An unterminated final event is
    not dispatched (SSE semantics). IDs persist, but this module never reconnects.
    Callers must also bound their transport's line buffer; httpx.aiter_lines()
    alone does not protect against an unbounded, newline-free response.
    """
    if isinstance(max_event_bytes, bool) or max_event_bytes <= 0:
        raise ValueError("max_event_bytes must be positive")
    data: list[str] = []
    event = ""
    event_id = ""
    size = 0
    first = True
    async for line in lines:
        if not isinstance(line, str):
            raise ProtocolError("SSE lines must be decoded UTF-8 text")
        # Accept line iterators with or without their CR/LF terminators.
        line = line.rstrip("\r\n")
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        size += len(line.encode("utf-8")) + 1
        if size > max_event_bytes:
            raise ProtocolError("SSE event exceeds the size limit", "sse_event_too_large")
        if not line:
            if data:
                yield {"event": event or "message", "data": "\n".join(data), "id": event_id}
            data = []
            event = ""
            size = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value
        elif field == "id" and "\x00" not in value:
            event_id = value


def _reject_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


class ProtocolMapper:
    """A mapper belongs to exactly one accepted chat turn.

    ``consume(event, thread_id, turn_id)`` returns ``(delta_or_None, done)``.
    Missing turn IDs are allowed only with an explicitly matching thread ID.
    Snapshot mode supports append-only snapshots; revisions cannot be represented
    as OpenAI deltas and fail explicitly rather than duplicating/retracting text.
    """

    def __init__(
        self, config: Mapping[str, Any], *, max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES
    ) -> None:
        if not isinstance(config, Mapping):
            raise ProtocolError("Configure the SSE event mapping first", "protocol_not_configured", 501)
        self.paths: dict[str, str] = {}
        for key, default in (
            ("event_type_path", "type"),
            ("turn_id_path", "turn_id"),
            ("thread_id_path", "thread_id"),
            ("text_path", "delta.text"),
        ):
            path = config.get(key, default)
            if not isinstance(path, str) or not path or any(not p for p in path.split(".")):
                raise ProtocolError(f"Invalid {key}", "protocol_not_configured", 501)
            self.paths[key] = path
        self.events: dict[str, set[str]] = {}
        for key in ("delta_events", "done_events", "error_events"):
            names = config.get(key, [])
            if (
                not isinstance(names, list)
                or any(not isinstance(name, str) or not name for name in names)
                or (key != "error_events" and not names)
            ):
                raise ProtocolError(
                    "Configure nonempty delta_events and done_events lists",
                    "protocol_not_configured",
                    501,
                )
            self.events[key] = set(names)
        groups = list(self.events.values())
        if any(groups[i] & groups[j] for i in range(len(groups)) for j in range(i)):
            raise ProtocolError("SSE event categories must not overlap", "protocol_not_configured", 501)
        self.mode = config.get("mode", "delta")
        if self.mode not in ("delta", "snapshot"):
            raise ProtocolError("mode must be delta or snapshot", "protocol_not_configured", 501)
        if isinstance(max_text_bytes, bool) or max_text_bytes <= 0:
            raise ValueError("max_text_bytes must be positive")
        self.max_text_bytes = max_text_bytes
        self.text_bytes = 0
        self.snapshot = ""
        self.has_text = False

    def consume(
        self, event: dict[str, str], thread_id: str, turn_id: str
    ) -> tuple[str | None, bool]:
        try:
            payload = json.loads(
                event["data"], parse_constant=_reject_constant, object_pairs_hook=_unique_object
            )
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProtocolError("SSE data is not a strict JSON object") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("SSE data must be a JSON object")
        thread = extract_path(payload, self.paths["thread_id_path"])
        turn = extract_path(payload, self.paths["turn_id_path"])
        # Never admit global events, and never let a matching turn override a
        # conflicting thread. Do not coerce upstream IDs to another type.
        if thread is not None and thread != thread_id:
            return None, False
        if turn is not None:
            if turn != turn_id:
                return None, False
        elif thread != thread_id:
            return None, False
        kind = extract_path(payload, self.paths["event_type_path"], _MISSING)
        if kind is _MISSING:
            kind = event.get("event", "message")
        if not isinstance(kind, str):
            raise ProtocolError("SSE event type must be a string")
        if kind in self.events["error_events"]:
            # Untrusted upstream payloads may contain secrets or user text.
            raise ProtocolError("Wabi reported an error for this turn", "upstream_turn_error")
        if kind in self.events["done_events"]:
            if not self.has_text:
                raise ProtocolError("Wabi completed the turn without text", "empty_upstream_response")
            return None, True
        if kind not in self.events["delta_events"]:
            return None, False
        text = extract_path(payload, self.paths["text_path"], _MISSING)
        if not isinstance(text, str):
            raise ProtocolError("Configured SSE text field is missing or not a string")
        if self.mode == "snapshot":
            if not text.startswith(self.snapshot):
                raise ProtocolError("Upstream snapshot revised emitted text", "snapshot_diverged")
            delta = text[len(self.snapshot):]
            self.snapshot = text
        else:
            delta = text
        self.text_bytes += len(delta.encode("utf-8"))
        if self.text_bytes > self.max_text_bytes:
            raise ProtocolError("Generated text exceeds the size limit", "response_too_large")
        if delta:
            self.has_text = True
            return delta, False
        return None, False
