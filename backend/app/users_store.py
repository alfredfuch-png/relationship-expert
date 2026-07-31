from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import bcrypt

from app.settings import Settings, get_settings

BEIJING_TZ = timezone(timedelta(hours=8))


class RegistrationInviteLimitError(Exception):
    """Raised when the invite code has reached its maximum number of registrations."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    is_admin: bool = False


def users_db_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir.resolve() / "users.db"


def _connect(settings: Settings | None = None) -> sqlite3.Connection:
    path = users_db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def estimate_cost_cny(
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings | None = None,
) -> float:
    settings = settings or get_settings()
    inp = max(0, int(prompt_tokens)) / 1_000_000.0 * float(settings.llm_price_input_cny_per_1m)
    out = max(0, int(completion_tokens)) / 1_000_000.0 * float(settings.llm_price_output_cny_per_1m)
    return round(inp + out, 6)


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
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id TEXT PRIMARY KEY,
                memory_text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS registration_invite_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                invite_code_hash TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated INTEGER NOT NULL DEFAULT 0,
                cost_cny REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_usage_events_user_created
                ON usage_events (user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_events_created
                ON usage_events (created_at);
            """
        )
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
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
            "SELECT id, username, password_hash, is_admin FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
        if not row:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("ascii")):
            return None
        return User(
            id=str(row["id"]),
            username=str(row["username"]),
            is_admin=bool(row["is_admin"]),
        )
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
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at, is_admin) VALUES (?, ?, ?, ?, 0)",
            (user_id, username, hash_password(password), now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"username already exists: {username}") from e
    finally:
        conn.close()
    return User(id=user_id, username=username, is_admin=False)



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
        row = conn.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return User(
            id=str(row["id"]),
            username=str(row["username"]),
            is_admin=bool(row["is_admin"]),
        )
    finally:
        conn.close()


def set_user_admin(username: str, is_admin: bool, settings: Settings | None = None) -> bool:
    init_db(settings)
    conn = _connect(settings)
    try:
        cur = conn.execute(
            "UPDATE users SET is_admin = ? WHERE username = ? COLLATE NOCASE",
            (1 if is_admin else 0, username.strip()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_admin_usernames(settings: Settings | None = None) -> int:
    """Promote usernames listed in ADMIN_USERNAMES (does not demote others)."""
    settings = settings or get_settings()
    names = [
        p.strip()
        for p in (settings.admin_usernames or "").split(",")
        if p.strip()
    ]
    if not names:
        return 0
    init_db(settings)
    promoted = 0
    for name in names:
        if set_user_admin(name, True, settings):
            promoted += 1
    return promoted


def is_user_admin(user_id: str, settings: Settings | None = None) -> bool:
    user = get_user_by_id(user_id, settings)
    return bool(user and user.is_admin)


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
    now = _utc_now()
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


def load_user_memory(user_id: str, settings: Settings | None = None) -> str:
    """Return cross-thread long-term memory text for an account user."""
    if not user_id or user_id in ("anonymous", "shared"):
        return ""
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT memory_text FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return ""
        return str(row["memory_text"] or "").strip()
    finally:
        conn.close()


def save_user_memory(user_id: str, memory_text: str, settings: Settings | None = None) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    init_db(settings)
    now = _utc_now()
    text = (memory_text or "").strip()
    conn = _connect(settings)
    try:
        if not text:
            conn.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                """
                INSERT INTO user_memory (user_id, memory_text, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    memory_text = excluded.memory_text,
                    updated_at = excluded.updated_at
                """,
                (user_id, text[:12000], now),
            )
        conn.commit()
    finally:
        conn.close()


def clear_user_memory(user_id: str, settings: Settings | None = None) -> None:
    save_user_memory(user_id, "", settings)


def record_usage_event(
    *,
    user_id: str,
    kind: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    estimated: bool = False,
    settings: Settings | None = None,
) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    settings = settings or get_settings()
    pt = max(0, int(prompt_tokens))
    ct = max(0, int(completion_tokens))
    tt = int(total_tokens) if total_tokens is not None else pt + ct
    cost = estimate_cost_cny(pt, ct, settings)
    init_db(settings)
    conn = _connect(settings)
    try:
        # Only persist for real account rows (FK).
        row = conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return
        conn.execute(
            """
            INSERT INTO usage_events (
                id, user_id, created_at, kind, model,
                prompt_tokens, completion_tokens, total_tokens, estimated, cost_cny
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                user_id,
                _utc_now(),
                kind[:32],
                (model or "")[:128],
                pt,
                ct,
                max(0, tt),
                1 if estimated else 0,
                cost,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _chat_stats_from_state(state: dict | None) -> tuple[int, int, str | None]:
    """Return (thread_count, message_count, latest_updated_iso_or_ms)."""
    if not state:
        return 0, 0, None
    threads = state.get("threads") or []
    if not isinstance(threads, list):
        return 0, 0, None
    msg_count = 0
    latest: int | float | None = None
    for t in threads:
        if not isinstance(t, dict):
            continue
        msgs = t.get("messages") or []
        if isinstance(msgs, list):
            msg_count += len(msgs)
        upd = t.get("updatedAt")
        if isinstance(upd, (int, float)):
            latest = upd if latest is None else max(latest, upd)
    latest_s = None
    if latest is not None:
        try:
            latest_s = datetime.fromtimestamp(float(latest) / 1000.0, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            latest_s = str(latest)
    return len(threads), msg_count, latest_s


def admin_overview(settings: Settings | None = None, *, days: int = 7) -> dict:
    settings = settings or get_settings()
    init_db(settings)
    since = datetime.now(UTC).timestamp() - max(1, days) * 86400
    since_iso = datetime.fromtimestamp(since, tz=UTC).isoformat()
    conn = _connect(settings)
    try:
        user_count = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
        usage = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(SUM(cost_cny), 0) AS cost,
                COUNT(DISTINCT user_id) AS active_users,
                COUNT(*) AS events
            FROM usage_events
            WHERE created_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        chat_active = conn.execute(
            """
            SELECT COUNT(*) AS c FROM user_chat_state WHERE updated_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        used, limit = registration_invite_usage(settings)
        slots = registration_slots_remaining(settings)
        return {
            "user_count": user_count,
            "invite_use_count": used,
            "invite_max_uses": limit,
            "registration_slots_remaining": slots,
            "days": days,
            "tokens_total": int(usage["tokens"] or 0),
            "cost_cny_total": round(float(usage["cost"] or 0), 4),
            "usage_active_users": int(usage["active_users"] or 0),
            "usage_events": int(usage["events"] or 0),
            "chat_active_users": int(chat_active["c"] or 0),
            "price_input_cny_per_1m": float(settings.llm_price_input_cny_per_1m),
            "price_output_cny_per_1m": float(settings.llm_price_output_cny_per_1m),
            "cost_note": "费用为按官方单价估算（输入按 cache miss），非账单原件",
        }
    finally:
        conn.close()


def admin_list_users(settings: Settings | None = None) -> list[dict]:
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT
                u.id, u.username, u.created_at, u.is_admin,
                c.state_json, c.updated_at AS chat_updated_at,
                COALESCE(ue.tokens, 0) AS tokens_total,
                COALESCE(ue.cost, 0) AS cost_cny_total,
                COALESCE(ue.events, 0) AS usage_events
            FROM users u
            LEFT JOIN user_chat_state c ON c.user_id = u.id
            LEFT JOIN (
                SELECT user_id,
                       SUM(total_tokens) AS tokens,
                       SUM(cost_cny) AS cost,
                       COUNT(*) AS events
                FROM usage_events
                GROUP BY user_id
            ) ue ON ue.user_id = u.id
            ORDER BY u.created_at DESC
            """
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            state = None
            if r["state_json"]:
                try:
                    state = json.loads(r["state_json"])
                except json.JSONDecodeError:
                    state = None
            threads, messages, latest_msg = _chat_stats_from_state(state)
            last_active = r["chat_updated_at"] or latest_msg or r["created_at"]
            out.append(
                {
                    "id": str(r["id"]),
                    "username": str(r["username"]),
                    "created_at": str(r["created_at"]),
                    "is_admin": bool(r["is_admin"]),
                    "thread_count": threads,
                    "message_count": messages,
                    "last_active_at": last_active,
                    "tokens_total": int(r["tokens_total"] or 0),
                    "cost_cny_total": round(float(r["cost_cny_total"] or 0), 4),
                    "usage_events": int(r["usage_events"] or 0),
                }
            )
        return out
    finally:
        conn.close()


def _beijing_period_starts() -> tuple[str, str]:
    """Return (today_start_utc_iso, month_start_utc_iso) for Asia/Shanghai calendar."""
    now_bj = datetime.now(BEIJING_TZ)
    day_start = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now_bj.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        day_start.astimezone(UTC).isoformat(),
        month_start.astimezone(UTC).isoformat(),
    )


def _usage_bucket(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    since_iso: str | None = None,
    with_kind: bool = False,
) -> dict:
    where = "WHERE user_id = ?"
    params: list[object] = [user_id]
    if since_iso:
        where += " AND created_at >= ?"
        params.append(since_iso)
    usage = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_cny), 0) AS cost_cny,
            COUNT(*) AS events
        FROM usage_events {where}
        """,
        params,
    ).fetchone()
    out: dict = {
        "prompt_tokens": int(usage["prompt_tokens"] or 0),
        "completion_tokens": int(usage["completion_tokens"] or 0),
        "total_tokens": int(usage["total_tokens"] or 0),
        "cost_cny_total": round(float(usage["cost_cny"] or 0), 4),
        "events": int(usage["events"] or 0),
    }
    if with_kind:
        by_kind = conn.execute(
            f"""
            SELECT kind,
                   SUM(total_tokens) AS tokens,
                   SUM(cost_cny) AS cost,
                   COUNT(*) AS events
            FROM usage_events {where}
            GROUP BY kind
            ORDER BY tokens DESC
            """,
            params,
        ).fetchall()
        out["by_kind"] = [
            {
                "kind": str(k["kind"]),
                "tokens": int(k["tokens"] or 0),
                "cost_cny": round(float(k["cost"] or 0), 4),
                "events": int(k["events"] or 0),
            }
            for k in by_kind
        ]
    return out


def admin_user_detail(user_id: str, settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            """
            SELECT id, username, created_at, is_admin FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        chat_row = conn.execute(
            "SELECT state_json, updated_at FROM user_chat_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        state = {"threads": [], "active_id": None}
        if chat_row and chat_row["state_json"]:
            try:
                parsed = json.loads(chat_row["state_json"])
                if isinstance(parsed, dict):
                    state = parsed
            except json.JSONDecodeError:
                pass
        threads, messages, latest_msg = _chat_stats_from_state(state)
        today_start, month_start = _beijing_period_starts()
        usage_today = _usage_bucket(conn, user_id, since_iso=today_start)
        usage_month = _usage_bucket(conn, user_id, since_iso=month_start)
        usage_total = _usage_bucket(conn, user_id, with_kind=True)
        memory_row = conn.execute(
            "SELECT memory_text, updated_at FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "created_at": str(row["created_at"]),
            "is_admin": bool(row["is_admin"]),
            "thread_count": threads,
            "message_count": messages,
            "last_active_at": (chat_row["updated_at"] if chat_row else None)
            or latest_msg
            or row["created_at"],
            "chat_state": state,
            "memory_text": str(memory_row["memory_text"]) if memory_row else "",
            "memory_updated_at": str(memory_row["updated_at"]) if memory_row else None,
            "usage": {
                "timezone": "Asia/Shanghai",
                "today": usage_today,
                "month": usage_month,
                "total": usage_total,
            },
            "cost_note": "费用为按官方单价估算（输入按 cache miss），非账单原件；今日/本月按北京时间",
        }
    finally:
        conn.close()


def admin_user_usage(
    user_id: str,
    settings: Settings | None = None,
    *,
    days: int = 30,
) -> dict | None:
    settings = settings or get_settings()
    user = get_user_by_id(user_id, settings)
    if not user:
        return None
    since = datetime.now(UTC).timestamp() - max(1, days) * 86400
    since_iso = datetime.fromtimestamp(since, tz=UTC).isoformat()
    init_db(settings)
    conn = _connect(settings)
    try:
        daily = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day,
                   SUM(total_tokens) AS tokens,
                   SUM(cost_cny) AS cost,
                   COUNT(*) AS events
            FROM usage_events
            WHERE user_id = ? AND created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day DESC
            """,
            (user_id, since_iso),
        ).fetchall()
        by_kind = conn.execute(
            """
            SELECT kind,
                   SUM(total_tokens) AS tokens,
                   SUM(cost_cny) AS cost,
                   COUNT(*) AS events
            FROM usage_events
            WHERE user_id = ? AND created_at >= ?
            GROUP BY kind
            ORDER BY tokens DESC
            """,
            (user_id, since_iso),
        ).fetchall()
        return {
            "user_id": user.id,
            "username": user.username,
            "days": days,
            "daily": [
                {
                    "day": str(r["day"]),
                    "tokens": int(r["tokens"] or 0),
                    "cost_cny": round(float(r["cost"] or 0), 4),
                    "events": int(r["events"] or 0),
                }
                for r in daily
            ],
            "by_kind": [
                {
                    "kind": str(r["kind"]),
                    "tokens": int(r["tokens"] or 0),
                    "cost_cny": round(float(r["cost"] or 0), 4),
                    "events": int(r["events"] or 0),
                }
                for r in by_kind
            ],
        }
    finally:
        conn.close()
