"""
Hysteria 2 Telegram Bot — SQLite key database
"""
import os
import sqlite3
import secrets
import time
from contextlib import contextmanager
from typing import Optional


class KeyDatabase:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS keys (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    key         TEXT    UNIQUE NOT NULL,
                    label       TEXT    DEFAULT '',
                    active      INTEGER DEFAULT 1,
                    created_at  REAL    NOT NULL,
                    created_by  INTEGER DEFAULT 0,
                    expires_at  REAL    DEFAULT 0,
                    max_devices INTEGER DEFAULT 0,
                    tx_bytes    INTEGER DEFAULT 0,
                    rx_bytes    INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    authed_at   REAL NOT NULL
                )
            """)

    # ── Key CRUD ────────────────────────────────────────────────────────

    def create_key(self, label: str = "", created_by: int = 0,
                   expires_at: float = 0, max_devices: int = 0) -> dict:
        key = secrets.token_hex(16)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO keys (key, label, active, created_at, created_by, expires_at, max_devices) "
                "VALUES (?, ?, 1, ?, ?, ?, ?)",
                (key, label, now, created_by, expires_at, max_devices),
            )
        return self.get_key(key)

    def get_key(self, key: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM keys WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def get_key_by_id(self, key_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row) if row else None

    def list_keys(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM keys ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def delete_key(self, key_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM keys WHERE id = ?", (key_id,))
        return cur.rowcount > 0

    def toggle_key(self, key_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
            if not row:
                return None
            new_status = 0 if row["active"] else 1
            conn.execute("UPDATE keys SET active = ? WHERE id = ?", (new_status, key_id))
        return self.get_key_by_id(key_id)

    def validate_key(self, password: str) -> bool:
        """Check if a key exists, is active, and not expired."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM keys WHERE key = ? AND active = 1", (password,)
            ).fetchone()
        if not row:
            return False
        if row["expires_at"] > 0 and time.time() > row["expires_at"]:
            return False
        return True

    def count_keys(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM keys WHERE active = 1").fetchone()[0]
        return {"total": total, "active": active, "blocked": total - active}

    # ── Auth sessions ───────────────────────────────────────────────────

    def is_authed(self, telegram_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM auth_sessions WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return row is not None

    def set_authed(self, telegram_id: int):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO auth_sessions (telegram_id, authed_at) VALUES (?, ?)",
                (telegram_id, time.time()),
            )

    def revoke_auth(self, telegram_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE telegram_id = ?", (telegram_id,))
