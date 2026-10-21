import pytest
from wb2api.cleanup import cleanup_thread
from wb2api.wabi import UpstreamError


class Fixture:
    def __init__(self):
        self.account = {'id': 'fixture'}
        self._owned_threads = {'created-here': self.account}
        self.delete_completed_threads = True
        self.base_url = 'https://upstream.example.invalid'
        self.calls = []
    def _headers(self, account):
        return {}
    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == 'GET':
            raise UpstreamError('not found', 404)
        return {}


@pytest.mark.asyncio
async def test_only_owned_completed_threads_are_deleted_and_read_back():
    w = Fixture()
    assert not await cleanup_thread(w, w.account, 'foreign', completed=True)
    assert not w.calls
    assert await cleanup_thread(w, w.account, 'created-here', completed=True)
    assert [x[0] for x in w.calls] == ['DELETE', 'GET']
    assert not w._owned_threads


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['incomplete', 'disabled', 'wrong-account', 'failure'])
async def test_cleanup_failure_and_exclusions(mode):
    w = Fixture()
    if mode == 'disabled':w.delete_completed_threads = False
    if mode == 'failure':
        async def fail(*args, **kwargs):raise RuntimeError('private-token')
        w._json = fail
    assert not await cleanup_thread(w, {} if mode == 'wrong-account' else w.account, 'created-here', completed=mode != 'incomplete')
    assert not w._owned_threads
