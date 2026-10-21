from __future__ import annotations

import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    """One-process SQLite store; secrets are encrypted before persistence."""

    secret_fields = ('id_token', 'refresh_token', 'firebase_api_key')

    def __init__(self, directory: str, encryption_key: str | None = None):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = root / 'encryption.key'
        if encryption_key:
            key = encryption_key.encode()
        else:
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(key)
        self.cipher = Fernet(key)
        self.db = sqlite3.connect(root / 'wb2api.db', check_same_thread=False)
        os.chmod(root / 'wb2api.db', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL,
                id_token TEXT NOT NULL, refresh_token TEXT NOT NULL,
                firebase_api_key TEXT NOT NULL, user_id TEXT NOT NULL,
                device_id TEXT NOT NULL, expires_at REAL NOT NULL,
                created_at REAL NOT NULL, phone_number TEXT DEFAULT '',
                activation_status TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
            );
        ''')
        try:
            self.db.execute('ALTER TABLE accounts ADD COLUMN phone_number TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute('ALTER TABLE accounts ADD COLUMN activation_status TEXT DEFAULT "active"')
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def close(self):
        self.db.close()

    def seal(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode() if value else ''

    def get_account(self, account_id: str) -> dict:
        row = self.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        result = dict(row)
        result['enabled'] = bool(result['enabled'])
        result['phone_number'] = result.get('phone_number', '') or ''
        result['activation_status'] = result.get('activation_status', 'active') or 'active'
        for field in self.secret_fields:
            result[field] = self.cipher.decrypt(result[field].encode()).decode() if result[field] else ''
        return result

    @staticmethod
    def public(account: dict) -> dict:
        refreshable = bool(account['refresh_token'] and account['firebase_api_key'])
        valid_id = bool(account['id_token'] and account['expires_at'] > time.time() + 60)
        activation = account.get('activation_status', 'active') or 'active'
        is_ready = bool(account['enabled'] and (refreshable or valid_id) and activation == 'active')
        return {
            'id': account['id'], 'label': account['label'], 'enabled': bool(account['enabled']),
            'has_refresh_token': bool(account['refresh_token']),
            'has_id_token': bool(account['id_token']), 'expires_at': account['expires_at'],
            'phone_number': account.get('phone_number', ''),
            'activation_status': activation,
            'ready': is_ready,
        }

    def list_accounts(self) -> list[dict]:
        rows = self.db.execute('SELECT id FROM accounts ORDER BY created_at').fetchall()
        return [self.public(self.get_account(row['id'])) for row in rows]

    def export_accounts(self) -> list[dict]:
        rows = self.db.execute('SELECT id FROM accounts ORDER BY created_at').fetchall()
        result = []
        for row in rows:
            acc = self.get_account(row['id'])
            result.append({
                'id': acc['id'],
                'label': acc['label'],
                'enabled': acc['enabled'],
                'phone_number': acc.get('phone_number', ''),
                'refresh_token': acc['refresh_token'],
                'id_token': acc['id_token'],
                'firebase_api_key': acc['firebase_api_key'],
                'user_id': acc['user_id'],
                'activation_status': acc.get('activation_status', 'active'),
                'created_at': acc['created_at'],
                'expires_at': acc['expires_at'],
            })
        return result

    def add_account(self, *, label: str, firebase_api_key: str = '', refresh_token: str = '',
                    id_token: str = '', user_id: str = '', expires_at: float = 0, phone_number: str = '') -> dict:
        account_id = str(uuid.uuid4())
        self.db.execute('''
            INSERT INTO accounts (id, label, enabled, id_token, refresh_token, firebase_api_key, user_id, device_id, expires_at, created_at, phone_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            account_id, label, 1, self.seal(id_token), self.seal(refresh_token),
            self.seal(firebase_api_key), user_id, str(uuid.uuid4()), expires_at, time.time(), phone_number
        ))
        self.db.commit()
        return self.public(self.get_account(account_id))

    def update_account_credentials(self, account_id: str, id_token: str, refresh_token: str,
                                   expires_at: float, firebase_api_key: str = '', phone_number: str = '') -> dict:
        row = self.get_account(account_id)
        fb_key = self.seal(firebase_api_key) if firebase_api_key else self.seal(row['firebase_api_key'])
        phone = phone_number or row.get('phone_number', '')
        self.db.execute('''
            UPDATE accounts 
            SET id_token=?, refresh_token=?, expires_at=?, firebase_api_key=?, phone_number=?, enabled=1
            WHERE id=?
        ''', (self.seal(id_token), self.seal(refresh_token), expires_at, fb_key, phone, account_id))
        self.db.commit()
        return self.public(self.get_account(account_id))

    def find_account_by_phone(self, phone: str) -> dict | None:
        if not phone:
            return None
        row = self.db.execute('SELECT id FROM accounts WHERE phone_number=?', (phone,)).fetchone()
        return self.get_account(row['id']) if row else None

    def set_enabled(self, account_id: str, enabled: bool) -> dict:
        self.get_account(account_id)
        self.db.execute('UPDATE accounts SET enabled=? WHERE id=?', (int(enabled), account_id))
        self.db.commit()
        return self.public(self.get_account(account_id))

    def delete_account(self, account_id: str):
        self.db.execute('DELETE FROM accounts WHERE id=?', (account_id,))
        self.db.commit()

    def update_user_id(self, account_id: str, user_id: str):
        self.db.execute('UPDATE accounts SET user_id=? WHERE id=?', (user_id, account_id))
        self.db.commit()

    def update_activation_status(self, account_id: str, status: str):
        self.db.execute('UPDATE accounts SET activation_status=? WHERE id=?', (status, account_id))
        self.db.commit()

    def update_tokens(self, account_id: str, id_token: str, refresh_token: str, expires_at: float):
        self.db.execute('UPDATE accounts SET id_token=?,refresh_token=?,expires_at=? WHERE id=?', (
            self.seal(id_token), self.seal(refresh_token), expires_at, account_id,
        ))
        self.db.commit()

    def get_setting(self, key: str, default: str = '') -> str:
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str):
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, value))
        self.db.commit()

    def list_api_keys(self) -> list[dict]:
        rows = self.db.execute('SELECT id, name, key, enabled, created_at FROM api_keys ORDER BY created_at DESC').fetchall()
        return [dict(r, enabled=bool(r['enabled'])) for r in rows]

    def add_api_key(self, name: str, key: str = '') -> dict:
        key_id = str(uuid.uuid4())
        token = (key.strip() if key else '') or ('sk-wb-' + secrets.token_urlsafe(24))
        now = time.time()
        self.db.execute('INSERT INTO api_keys VALUES (?,?,?,1,?)', (key_id, name, token, now))
        self.db.commit()
        return {'id': key_id, 'name': name, 'key': token, 'enabled': True, 'created_at': now}

    def toggle_api_key(self, key_id: str, enabled: bool) -> dict:
        self.db.execute('UPDATE api_keys SET enabled=? WHERE id=?', (int(enabled), key_id))
        self.db.commit()
        row = self.db.execute('SELECT id, name, key, enabled, created_at FROM api_keys WHERE id=?', (key_id,)).fetchone()
        if not row:
            raise KeyError(key_id)
        return dict(row, enabled=bool(row['enabled']))

    def delete_api_key(self, key_id: str):
        self.db.execute('DELETE FROM api_keys WHERE id=?', (key_id,))
        self.db.commit()

    def verify_api_key(self, key: str) -> bool:
        if not key:
            return False
        row = self.db.execute('SELECT 1 FROM api_keys WHERE key=? AND enabled=1', (key,)).fetchone()
        return row is not None
