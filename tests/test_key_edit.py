import pytest
from fastapi.testclient import TestClient
from wb2api.main import create_app

AH = {'Authorization': 'Bearer key-edit-admin'}
IH = {'Authorization': 'Bearer key-edit-client'}


@pytest.fixture
def client(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'admin_key': 'key-edit-admin', 'api_key': 'key-edit-client'})
    with TestClient(app) as session:
        yield session


def test_key_rename_preserves_secret_enabled_and_creation(client):
    original = client.post('/admin/api/keys', headers=AH, json={'name': 'Old name'}).json()
    url = '/admin/api/keys/' + original['id']
    client.patch(url, headers=AH, json={'enabled': False})
    response = client.patch(url, headers=AH, json={'name': '  新名称 <safe>  '})
    assert response.status_code == 200
    result = response.json()
    assert result['name'] == '新名称 <safe>'
    assert result['key'] == original['key']
    assert result['created_at'] == original['created_at']
    assert result['id'] == original['id'] and result['enabled'] is False
    saved = next(k for k in client.get('/admin/api/keys', headers=AH).json() if k['id'] == original['id'])
    assert saved == result
    assert client.get('/v1/models', headers={'Authorization': 'Bearer ' + original['key']}).status_code == 401
    assert client.patch(url, headers=AH, json={'enabled': True}).status_code == 200
    assert client.get('/v1/models', headers={'Authorization': 'Bearer ' + original['key']}).status_code == 200


@pytest.mark.parametrize('payload', [{}, {'name': None}, {'enabled': None}, {'name': ''}, {'name': '   '}, {'name': 'x' * 101}, {'key': 'must-not-rotate'}])
def test_key_update_invalid_payload_no_side_effect(client, payload):
    original = client.post('/admin/api/keys', headers=AH, json={'name': 'Original'}).json()
    r = client.patch('/admin/api/keys/' + original['id'], headers=AH, json=payload)
    assert r.status_code == 422
    assert client.get('/admin/api/keys', headers=AH).json() == [original]


def test_key_update_auth_missing_and_atomic_fields(client):
    original = client.post('/admin/api/keys', headers=AH, json={'name': 'Original'}).json()
    url = '/admin/api/keys/' + original['id']
    assert client.patch(url, json={'name': 'Unauthorized'}).status_code == 401
    assert client.patch(url, headers=IH, json={'name': 'Unauthorized'}).status_code == 401
    assert client.patch('/admin/api/keys/missing', headers=AH, json={'name': 'Missing'}).status_code == 404
    r = client.patch(url, headers=AH, json={'name': 'Renamed', 'enabled': False})
    assert r.status_code == 200 and r.json()['name'] == 'Renamed' and not r.json()['enabled']
