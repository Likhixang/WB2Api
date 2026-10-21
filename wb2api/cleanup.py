"""Only request-owned, completed upstream threads are eligible for cleanup."""
import logging
from urllib.parse import quote

import anyio

log = logging.getLogger(__name__)


async def cleanup_thread(wabi, account, thread_id, *, completed):
    # This map is populated only from create-thread responses in this process.
    owner = wabi._owned_threads.pop(thread_id, None)
    if not completed or not wabi.delete_completed_threads or owner is not account:
        return False
    # Completion/disconnect cancellation must not abandon a deletion in flight.
    # A cleanup failure cannot replace an already successful inference result.
    with anyio.move_on_after(15, shield=True):
        try:
            root = wabi.base_url + '/api/v1/threads/' + quote(thread_id, safe='')
            await wabi._json('DELETE', root, headers=wabi._headers(account))
            try:
                await wabi._json('GET', root, headers=wabi._headers(account))
            except Exception as exc:
                if getattr(exc, 'status', None) == 404:
                    return True
            log.warning('Completed upstream thread cleanup was not confirmed')
        except Exception:
            log.warning('Completed upstream thread cleanup failed; response retained')
    return False
