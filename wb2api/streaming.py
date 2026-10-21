"""Keep SSE connections alive without exposing unvalidated model output."""
import asyncio
import anyio
from contextlib import suppress


async def with_heartbeats(source, interval=10.0):
    pending = None
    try:
        # Initial bytes precede upstream connection/queue/model delays.
        yield ': connected\n\n'
        while True:
            pending = asyncio.create_task(anext(source))
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield ': keep-alive\n\n'
            try:
                value = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            yield value
    finally:
        # Starlette cancels a whole AnyIO scope on disconnect. Shield awaited
        # cleanup so repeated cancellation cannot interrupt upstream release.
        with anyio.CancelScope(shield=True):
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pending
            await source.aclose()
