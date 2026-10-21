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
    Turn IDs must match, either at the configured path or ``message.turn_id``.
    Thread-only deltas are ignored, never attributed by guesswork; use correlated
    final message blocks as a fallback when the upstream omits delta turn IDs.
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
        self.emitted = ""
        self.has_text = False

    @staticmethod
    def _payload(event: dict[str, str]) -> dict[str, Any]:
        try:
            payload = json.loads(
                event["data"], parse_constant=_reject_constant, object_pairs_hook=_unique_object
            )
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProtocolError("SSE data is not a strict JSON object") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("SSE data must be a JSON object")
        return payload

    def opening_turn(self, event: dict[str, str], thread_id: str) -> str | None:
        """Observe completion, not text, before the first user POST.

        An initial idle snapshot can race with scheduling the automatic group
        opening. Require a positively identified completed assistant message first;
        callers must then confirm authoritative turn-state is idle. Wabi can mark
        this message is_final_message=False even when the opening has completed.
        """
        payload = self._payload(event)
        if extract_path(payload, self.paths["thread_id_path"]) != thread_id:
            return None
        nested_thread = extract_path(payload, "message.thread_id")
        if nested_thread is not None and nested_thread != thread_id:
            return None
        kind = extract_path(payload, self.paths["event_type_path"], event.get("event", "message"))
        if not isinstance(kind, str):
            raise ProtocolError("SSE event type must be a string")
        if kind in self.events["error_events"]:
            raise ProtocolError("Wabi reported an error during thread opening", "upstream_turn_error")
        if kind not in self.events["done_events"]:
            return None
        message = payload.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("status") != "completed"):
            return None
        turn = message.get("turn_id")
        outer_turn = extract_path(payload, self.paths["turn_id_path"])
        if not isinstance(turn, str) or not turn or (outer_turn is not None and outer_turn != turn):
            return None
        # Empty message_finished initialization events are not completion proof.
        blocks = message.get("blocks")
        return turn if isinstance(blocks, list) and blocks else None

    def consume(
        self, event: dict[str, str], thread_id: str, turn_id: str
    ) -> tuple[str | None, bool]:
        payload = self._payload(event)
        thread = extract_path(payload, self.paths["thread_id_path"])
        turn = extract_path(payload, self.paths["turn_id_path"])
        nested_turn = extract_path(payload, "message.turn_id")
        nested_thread = extract_path(payload, "message.thread_id")
        if nested_thread is not None and nested_thread != thread_id:
            return None, False
        if nested_turn is not None and nested_turn != turn_id:
            return None, False
        if turn is None:
            turn = nested_turn
        # A thread alone is not sufficient: greetings, replayed previous turns,
        # and empty message_finished events can all share its ID. Turnless deltas
        # are deliberately ignored; correlated final blocks provide a safe fallback.
        if thread is not None and thread != thread_id:
            return None, False
        if turn != turn_id:
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
            if extract_path(payload, "message.status") in ("failed", "error", "cancelled"):
                raise ProtocolError("Upstream message did not complete successfully", "upstream_turn_error")
            blocks = extract_path(payload, "message.blocks")
            final_text = ""
            if isinstance(blocks, list):
                texts = [block.get("text") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
                if any(not isinstance(text, str) for text in texts):
                    raise ProtocolError("Final text block is not a string")
                final_text = "".join(texts)
            if final_text:
                if not final_text.startswith(self.emitted):
                    raise ProtocolError("Final message revised emitted text", "snapshot_diverged")
                delta = final_text[len(self.emitted):]
                if len(final_text.encode("utf-8")) > self.max_text_bytes:
                    raise ProtocolError("Generated text exceeds the size limit", "response_too_large")
                self.has_text = True
                return delta or None, True
            if isinstance(blocks, list) and not final_text:
                return None, False
            if not self.has_text:
                # Empty initialization events are not evidence that our response
                # completed. EOF/timeout still produces a real upstream error.
                return None, False
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
            self.emitted += delta
            self.has_text = True
            return delta, False
        return None, False
