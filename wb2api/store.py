from __future__ import annotations

import os
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
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
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
        for field in self.secret_fields:
            result[field] = self.cipher.decrypt(result[field].encode()).decode() if result[field] else ''
        return result

    @staticmethod
    def public(account: dict) -> dict:
        refreshable = bool(account['refresh_token'] and account['firebase_api_key'])
        valid_id = bool(account['id_token'] and account['expires_at'] > time.time() + 60)
        return {
            'id': account['id'], 'label': account['label'], 'enabled': bool(account['enabled']),
            'has_refresh_token': bool(account['refresh_token']),
            'has_id_token': bool(account['id_token']), 'expires_at': account['expires_at'],
            'ready': bool(account['enabled'] and (refreshable or valid_id)),
        }

    def list_accounts(self) -> list[dict]:
        rows = self.db.execute('SELECT id FROM accounts ORDER BY created_at').fetchall()
        return [self.public(self.get_account(row['id'])) for row in rows]

    def add_account(self, *, label: str, firebase_api_key: str = '', refresh_token: str = '',
                    id_token: str = '', user_id: str = '', expires_at: float = 0) -> dict:
        account_id = str(uuid.uuid4())
        self.db.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?)', (
            account_id, label, 1, self.seal(id_token), self.seal(refresh_token),
            self.seal(firebase_api_key), user_id, str(uuid.uuid4()), expires_at, time.time(),
        ))
        self.db.commit()
        return self.public(self.get_account(account_id))

    def set_enabled(self, account_id: str, enabled: bool) -> dict:
        self.get_account(account_id)
        self.db.execute('UPDATE accounts SET enabled=? WHERE id=?', (int(enabled), account_id))
        self.db.commit()
        return self.public(self.get_account(account_id))

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
