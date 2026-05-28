from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import bcrypt

from app.settings import Settings, get_settings


class RegistrationInviteLimitError(Exception):
    """Raised when the invite code has reached its maximum number of registrations."""


@dataclass(frozen=True)
class User:
    id: str
    username: str


def users_db_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir.resolve() / "users.db"


def _connect(settings: Settings | None = None) -> sqlite3.Connection:
    path = users_db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(settings: Settings | None = None) -> None:
    conn = _connect(settings)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_chat_state (
                user_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS registration_invite_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                invite_code_hash TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _invite_code_hash(settings: Settings) -> str:
    code = settings.registration_invite_code.strip()
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _sync_invite_state_row(conn: sqlite3.Connection, code_hash: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT invite_code_hash, use_count FROM registration_invite_state WHERE id = 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO registration_invite_state (id, invite_code_hash, use_count) VALUES (1, ?, 0)",
            (code_hash,),
        )
        row = conn.execute(
            "SELECT invite_code_hash, use_count FROM registration_invite_state WHERE id = 1"
        ).fetchone()
    elif str(row["invite_code_hash"]) != code_hash:
        conn.execute(
            "UPDATE registration_invite_state SET invite_code_hash = ?, use_count = 0 WHERE id = 1",
            (code_hash,),
        )
        row = conn.execute(
            "SELECT invite_code_hash, use_count FROM registration_invite_state WHERE id = 1"
        ).fetchone()
    return row


def registration_invite_usage(settings: Settings | None = None) -> tuple[int, int | None]:
    """Return (use_count, max_uses). max_uses is None when unlimited."""
    settings = settings or get_settings()
    max_uses = settings.registration_invite_max_uses
    limit: int | None = max_uses if max_uses > 0 else None
    init_db(settings)
    conn = _connect(settings)
    try:
        row = _sync_invite_state_row(conn, _invite_code_hash(settings))
        conn.commit()
        return int(row["use_count"]), limit
    finally:
        conn.close()


def registration_slots_remaining(settings: Settings | None = None) -> int | None:
    used, limit = registration_invite_usage(settings)
    if limit is None:
        return None
    return max(0, limit - used)


def consume_registration_slot(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    max_uses = settings.registration_invite_max_uses
    if max_uses <= 0:
        return
    init_db(settings)
    code_hash = _invite_code_hash(settings)
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _sync_invite_state_row(conn, code_hash)
        used = int(row["use_count"])
        if used >= max_uses:
            conn.rollback()
            raise RegistrationInviteLimitError()
        conn.execute(
            "UPDATE registration_invite_state SET use_count = use_count + 1 WHERE id = 1"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_registration_slot(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.registration_invite_max_uses <= 0:
        return
    init_db(settings)
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT use_count FROM registration_invite_state WHERE id = 1"
        ).fetchone()
        if row and int(row["use_count"]) > 0:
            conn.execute(
                "UPDATE registration_invite_state SET use_count = use_count - 1 WHERE id = 1"
            )
        conn.commit()
    finally:
        conn.close()


def has_users(settings: Settings | None = None) -> bool:
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return bool(row and int(row["c"]) > 0)
    finally:
        conn.close()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_user_password(username: str, password: str, settings: Settings | None = None) -> User | None:
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
        if not row:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("ascii")):
            return None
        return User(id=str(row["id"]), username=str(row["username"]))
    finally:
        conn.close()


def create_user(username: str, password: str, settings: Settings | None = None) -> User:
    username = username.strip()
    if not username:
        raise ValueError("username required")
    if len(password) < 4:
        raise ValueError("password too short")
    init_db(settings)
    user_id = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn = _connect(settings)
    try:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, hash_password(password), now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"username already exists: {username}") from e
    finally:
        conn.close()
    return User(id=user_id, username=username)


def delete_user(username: str, settings: Settings | None = None) -> bool:
    init_db(settings)
    conn = _connect(settings)
    try:
        cur = conn.execute("DELETE FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_users(settings: Settings | None = None) -> list[str]:
    init_db(settings)
    conn = _connect(settings)
    try:
        rows = conn.execute("SELECT username FROM users ORDER BY username COLLATE NOCASE").fetchall()
        return [str(r["username"]) for r in rows]
    finally:
        conn.close()


def get_user_by_id(user_id: str, settings: Settings | None = None) -> User | None:
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return User(id=str(row["id"]), username=str(row["username"]))
    finally:
        conn.close()


def bootstrap_users(spec: str, settings: Settings | None = None) -> int:
    """spec: 'user:pass,user2:pass2' — only creates users when table is empty."""
    if has_users(settings):
        return 0
    created = 0
    for part in spec.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        username, password = part.split(":", 1)
        username = username.strip()
        password = password.strip()
        if not username or not password:
            continue
        create_user(username, password, settings)
        created += 1
    return created


def load_chat_state(user_id: str, settings: Settings | None = None) -> dict | None:
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT state_json FROM user_chat_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["state_json"])
    finally:
        conn.close()


def save_chat_state(user_id: str, state: dict, settings: Settings | None = None) -> None:
    init_db(settings)
    now = datetime.now(UTC).isoformat()
    payload = json.dumps(state, ensure_ascii=False)
    conn = _connect(settings)
    try:
        conn.execute(
            """
            INSERT INTO user_chat_state (user_id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (user_id, payload, now),
        )
        conn.commit()
    finally:
        conn.close()
