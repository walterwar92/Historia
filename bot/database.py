"""
Hysteria 2 Telegram Bot — SQLite database
Tables: users, keys, invite_codes
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
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id  INTEGER PRIMARY KEY,
                    username     TEXT    DEFAULT '',
                    role         TEXT    DEFAULT 'user',
                    max_keys     INTEGER DEFAULT 1,
                    created_at   REAL    NOT NULL,
                    invited_by   INTEGER DEFAULT 0
                )
            """)
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
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code        TEXT    PRIMARY KEY,
                    role        TEXT    DEFAULT 'user',
                    max_keys    INTEGER DEFAULT 1,
                    created_by  INTEGER DEFAULT 0,
                    created_at  REAL    NOT NULL,
                    used_by     INTEGER DEFAULT 0,
                    used_at     REAL    DEFAULT 0
                )
            """)
            # Migrate: drop old auth_sessions if exists
            conn.execute("DROP TABLE IF EXISTS auth_sessions")

    # ── Users ───────────────────────────────────────────────────────────

    def create_user(self, telegram_id: int, username: str = "",
                    role: str = "user", max_keys: int = 1,
                    invited_by: int = 0) -> dict:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users "
                "(telegram_id, username, role, max_keys, created_at, invited_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_id, username, role, max_keys, now, invited_by),
            )
        return self.get_user(telegram_id)

    def get_user(self, telegram_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_user(self, telegram_id: int) -> bool:
        with self._conn() as conn:
            # Also delete user's keys
            conn.execute("DELETE FROM keys WHERE created_by = ?", (telegram_id,))
            cur = conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        return cur.rowcount > 0

    def update_user_role(self, telegram_id: int, role: str) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET role = ? WHERE telegram_id = ?",
                (role, telegram_id),
            )
        return self.get_user(telegram_id)

    def update_user_max_keys(self, telegram_id: int, max_keys: int) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET max_keys = ? WHERE telegram_id = ?",
                (max_keys, telegram_id),
            )
        return self.get_user(telegram_id)

    def is_registered(self, telegram_id: int) -> bool:
        return self.get_user(telegram_id) is not None

    def count_users(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]
        return {"total": total, "admins": admins, "users": total - admins}

    # ── Invite codes ────────────────────────────────────────────────────

    def create_invite(self, role: str = "user", max_keys: int = 1,
                      created_by: int = 0) -> dict:
        code = secrets.token_urlsafe(8)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO invite_codes "
                "(code, role, max_keys, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, role, max_keys, created_by, now),
            )
        return self.get_invite(code)

    def get_invite(self, code: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def use_invite(self, code: str, telegram_id: int,
                   username: str = "") -> Optional[dict]:
        """Use an invite code to register a user. Returns user dict or None."""
        invite = self.get_invite(code)
        if not invite:
            return None
        if invite["used_by"] != 0:
            return None  # Already used

        # Register user
        user = self.create_user(
            telegram_id=telegram_id,
            username=username,
            role=invite["role"],
            max_keys=invite["max_keys"],
            invited_by=invite["created_by"],
        )

        # Mark invite as used
        with self._conn() as conn:
            conn.execute(
                "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ?",
                (telegram_id, time.time(), code),
            )

        return user

    def list_invites(self, unused_only: bool = False) -> list[dict]:
        with self._conn() as conn:
            if unused_only:
                rows = conn.execute(
                    "SELECT * FROM invite_codes WHERE used_by = 0 ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM invite_codes ORDER BY created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_invite(self, code: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM invite_codes WHERE code = ?", (code,))
        return cur.rowcount > 0

    # ── Keys ────────────────────────────────────────────────────────────

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

    def list_keys_by_user(self, telegram_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM keys WHERE created_by = ? ORDER BY id",
                (telegram_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_user_keys(self, telegram_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM keys WHERE created_by = ?",
                (telegram_id,),
            ).fetchone()
        return row[0]

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

    def regenerate_key(self, key_id: int) -> Optional[dict]:
        """Delete old key and create a new one owned by the same user."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
            if not row:
                return None
            owner = row["created_by"]
            label = row["label"]
            conn.execute("DELETE FROM keys WHERE id = ?", (key_id,))
        return self.create_key(label=label, created_by=owner)
