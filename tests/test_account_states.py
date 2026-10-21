import time
import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app
from wb2api.store import Store
from wb2api.wabi import Wabi, UpstreamError


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', [{}, {'activation_status': None}, {'activation_status': ''}])
async def test_missing_status_never_promotes_pending(tmp_path, profile):
    s = Store(str(tmp_path))
    a = s.add_account(label='fixture', id_token='fixture', expires_at=time.time() + 3600)
    s.update_activation_status(a['id'], 'pending')
    w = Wabi(s, {})
    async def reply(*args, **kwargs):
        return profile
    w._json = reply
    try:
        await w.validate_account(a['id'])
        assert s.list_accounts()[0]['activation_status'] == 'pending'
        assert not s.list_accounts()[0]['ready']
    finally:
        await w.close()
        s.close()


@pytest.mark.parametrize('outcome', ['active', 'pending', 'network_error', 'unauthorized'])
def test_reauth_probes_new_credentials_without_false_ready(tmp_path, outcome):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'account-admin', 'api_key': 'account-client'})
    with TestClient(app) as c:
        a = app.state.store.add_account(label='fixture', id_token='old', expires_at=time.time() + 3600)
        app.state.store.update_activation_status(a['id'], 'invalid')
        async def reply(*args, **kwargs):
            if outcome == 'network_error':
                raise UpstreamError('Transport failed', 502, 'upstream_transport_error')
            if outcome == 'unauthorized':
                raise UpstreamError('Rejected', 401, 'upstream_authentication_failed')
            return {'activation_status': outcome}
        app.state.wabi._json = reply
        # A valid future-expiry JWT-shaped fixture avoids Firebase network refresh.
        from base64 import urlsafe_b64encode
        import json
        token = 'fixture.' + urlsafe_b64encode(json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=') + '.fixture'
        r = c.put('/admin/api/accounts/' + a['id'] + '/reauth', headers={'Authorization': 'Bearer account-admin'}, json={'label': 'fixture', 'id_token': token})
        assert r.status_code == 200
        saved = c.get('/admin/api/status', headers={'Authorization': 'Bearer account-admin'}).json()['accounts'][0]
        assert saved['ready'] is (outcome == 'active')
        assert saved['activation_status'] == {'network_error': 'unknown', 'unauthorized': 'invalid'}.get(outcome, outcome)
        assert app.state.store.get_account(a['id'])['id_token'] == token
        assert r.json().get('verification_pending', False) is (outcome in ('network_error', 'unauthorized'))
