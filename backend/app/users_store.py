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


def _memory_max_chars(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    cap = int(getattr(settings, "memory_max_chars", 4000) or 4000)
    return max(0, cap)


def _clip_memory(text: str, settings: Settings | None = None) -> str:
    t = (text or "").strip()
    cap = _memory_max_chars(settings)
    if cap > 0 and len(t) > cap:
        return t[:cap]
    return t


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
            CREATE TABLE IF NOT EXISTS user_profile_memory (
                user_id TEXT PRIMARY KEY,
                memory_text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS expert_advice_memory (
                user_id TEXT NOT NULL,
                expert_id TEXT NOT NULL,
                memory_text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, expert_id),
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
            CREATE TABLE IF NOT EXISTS risk_alerts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expert_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                categories TEXT NOT NULL DEFAULT '[]',
                snippet TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'open',
                keyword_hits TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_risk_alerts_status_created
                ON risk_alerts (status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_risk_alerts_user_status
                ON risk_alerts (user_id, status);
            """
        )
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        ue_cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()
        }
        if "expert_id" not in ue_cols:
            conn.execute(
                "ALTER TABLE usage_events ADD COLUMN expert_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_expert_created
                ON usage_events (expert_id, created_at)
            """
        )
        # One-time migrate legacy user_memory → profile (advice left empty until chat refreshes).
        try:
            legacy_rows = conn.execute(
                """
                SELECT um.user_id, um.memory_text, um.updated_at
                FROM user_memory um
                LEFT JOIN user_profile_memory pm ON pm.user_id = um.user_id
                WHERE (pm.user_id IS NULL OR pm.memory_text = '')
                  AND TRIM(um.memory_text) != ''
                """
            ).fetchall()
            for lr in legacy_rows:
                conn.execute(
                    """
                    INSERT INTO user_profile_memory (user_id, memory_text, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        memory_text = excluded.memory_text,
                        updated_at = excluded.updated_at
                    """,
                    (str(lr["user_id"]), str(lr["memory_text"]), str(lr["updated_at"])),
                )
                # Seed default expert advice from legacy blob so 阿FU keeps continuity.
                conn.execute(
                    """
                    INSERT INTO expert_advice_memory (user_id, expert_id, memory_text, updated_at)
                    VALUES (?, 'afu', ?, ?)
                    ON CONFLICT(user_id, expert_id) DO NOTHING
                    """,
                    (str(lr["user_id"]), str(lr["memory_text"]), str(lr["updated_at"])),
                )
        except sqlite3.Error:
            pass
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


def change_user_password(
    user_id: str,
    old_password: str,
    new_password: str,
    settings: Settings | None = None,
) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        raise ValueError("account required")
    if len(new_password) < 4:
        raise ValueError("password too short")
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("user not found")
        if not bcrypt.checkpw(
            old_password.encode("utf-8"),
            str(row["password_hash"]).encode("ascii"),
        ):
            raise ValueError("old password incorrect")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def user_usage_summary(user_id: str, settings: Settings | None = None) -> dict | None:
    """Self-serve usage: 7-day daily, 30d total, lifetime, calendar-month used."""
    if not user_id or user_id in ("anonymous", "shared"):
        return None
    settings = settings or get_settings()
    user = get_user_by_id(user_id, settings)
    if not user:
        return None

    _yesterday_start, today_start, now_bj = _beijing_day_bounds()
    start_7 = today_start - timedelta(days=6)
    start_30 = today_start - timedelta(days=29)
    month_start = now_bj.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    s7 = _to_utc_iso(start_7)
    s30 = _to_utc_iso(start_30)
    s_month = _to_utc_iso(month_start)
    allowance = max(0, int(settings.token_monthly_allowance))

    init_db(settings)
    conn = _connect(settings)
    try:
        bj_counts: dict[str, int] = {}
        for i in range(7):
            d = (start_7 + timedelta(days=i)).date().isoformat()
            bj_counts[d] = 0
        rows = conn.execute(
            """
            SELECT created_at, total_tokens
            FROM usage_events
            WHERE user_id = ? AND created_at >= ?
            """,
            (user_id, s7),
        ).fetchall()
        for r in rows:
            try:
                day_bj = _parse_created_at_beijing(str(r["created_at"])).date().isoformat()
            except Exception:  # noqa: BLE001
                continue
            if day_bj in bj_counts:
                bj_counts[day_bj] += int(r["total_tokens"] or 0)
        daily_out = [{"day": k, "tokens": bj_counts[k]} for k in sorted(bj_counts.keys())]

        t30 = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events WHERE user_id = ? AND created_at >= ?
            """,
            (user_id, s30),
        ).fetchone()
        total = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        month = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events WHERE user_id = ? AND created_at >= ?
            """,
            (user_id, s_month),
        ).fetchone()
        month_tokens = int(month["tokens"] or 0)
        return {
            "username": user.username,
            "timezone": "Asia/Shanghai",
            "daily_7d": daily_out,
            "tokens_30d": int(t30["tokens"] or 0),
            "tokens_total": int(total["tokens"] or 0),
            "tokens_month": month_tokens,
            "monthly_allowance": allowance,
            "month_progress": min(1.0, month_tokens / allowance) if allowance > 0 else 0.0,
            "is_admin": bool(user.is_admin),
        }
    finally:
        conn.close()



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


TOKEN_QUOTA_EXCEEDED_MESSAGE = "您的对话额度已用完。"


def _parse_created_at_beijing(created_at: str) -> datetime:
    raw = (created_at or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BEIJING_TZ)


def _months_inclusive_beijing(start_bj: datetime, end_bj: datetime) -> int:
    months = (end_bj.year - start_bj.year) * 12 + (end_bj.month - start_bj.month) + 1
    return max(1, months)


def get_token_quota(user_id: str, settings: Settings | None = None) -> dict:
    """
    Monthly grant accrues and unused balance carries forward.

    granted = months_since_registration_month * monthly_allowance
    remaining = granted - lifetime_tokens_used
    """
    settings = settings or get_settings()
    allowance = max(0, int(settings.token_monthly_allowance))
    if not user_id or user_id in ("anonymous", "shared"):
        return {
            "allowed": True,
            "unlimited": True,
            "monthly_allowance": allowance,
            "months_granted": 0,
            "granted_tokens": 0,
            "used_tokens": 0,
            "remaining_tokens": 0,
            "message": "",
        }
    user = get_user_by_id(user_id, settings)
    if not user:
        return {
            "allowed": False,
            "unlimited": False,
            "monthly_allowance": allowance,
            "months_granted": 0,
            "granted_tokens": 0,
            "used_tokens": 0,
            "remaining_tokens": 0,
            "message": TOKEN_QUOTA_EXCEEDED_MESSAGE,
        }
    if user.is_admin:
        return {
            "allowed": True,
            "unlimited": True,
            "monthly_allowance": allowance,
            "months_granted": 0,
            "granted_tokens": 0,
            "used_tokens": 0,
            "remaining_tokens": 0,
            "message": "",
        }

    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        created = str(row["created_at"]) if row else _utc_now()
        used_row = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        used = int(used_row["tokens"] or 0)
    finally:
        conn.close()

    start_bj = _parse_created_at_beijing(created)
    now_bj = datetime.now(BEIJING_TZ)
    months = _months_inclusive_beijing(start_bj, now_bj)
    granted = months * allowance
    remaining = granted - used
    allowed = remaining > 0
    return {
        "allowed": allowed,
        "unlimited": False,
        "monthly_allowance": allowance,
        "months_granted": months,
        "granted_tokens": granted,
        "used_tokens": used,
        "remaining_tokens": max(0, remaining),
        "message": "" if allowed else TOKEN_QUOTA_EXCEEDED_MESSAGE,
    }


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
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT memory_text FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return ""
        return _clip_memory(str(row["memory_text"] or ""), settings)
    finally:
        conn.close()


def save_user_memory(user_id: str, memory_text: str, settings: Settings | None = None) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    settings = settings or get_settings()
    init_db(settings)
    now = _utc_now()
    text = _clip_memory(memory_text, settings)
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
                (user_id, text, now),
            )
        conn.commit()
    finally:
        conn.close()


def clear_user_memory(user_id: str, settings: Settings | None = None) -> None:
    save_user_memory(user_id, "", settings)
    save_user_profile_memory(user_id, "", settings)
    if not user_id or user_id in ("anonymous", "shared"):
        return
    init_db(settings)
    conn = _connect(settings)
    try:
        conn.execute("DELETE FROM expert_advice_memory WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def load_user_profile_memory(user_id: str, settings: Settings | None = None) -> str:
    if not user_id or user_id in ("anonymous", "shared"):
        return ""
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT memory_text FROM user_profile_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row and str(row["memory_text"] or "").strip():
            return _clip_memory(str(row["memory_text"]), settings)
        # Fallback to legacy blob
        legacy = conn.execute(
            "SELECT memory_text FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not legacy:
            return ""
        return _clip_memory(str(legacy["memory_text"] or ""), settings)
    finally:
        conn.close()


def save_user_profile_memory(
    user_id: str, memory_text: str, settings: Settings | None = None
) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    settings = settings or get_settings()
    init_db(settings)
    now = _utc_now()
    text = _clip_memory(memory_text, settings)
    conn = _connect(settings)
    try:
        if not text:
            conn.execute("DELETE FROM user_profile_memory WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                """
                INSERT INTO user_profile_memory (user_id, memory_text, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    memory_text = excluded.memory_text,
                    updated_at = excluded.updated_at
                """,
                (user_id, text, now),
            )
            # Keep legacy table in sync for older clients / admin views.
            conn.execute(
                """
                INSERT INTO user_memory (user_id, memory_text, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    memory_text = excluded.memory_text,
                    updated_at = excluded.updated_at
                """,
                (user_id, text, now),
            )
        conn.commit()
    finally:
        conn.close()


def load_expert_advice_memory(
    user_id: str, expert_id: str, settings: Settings | None = None
) -> str:
    if not user_id or user_id in ("anonymous", "shared"):
        return ""
    settings = settings or get_settings()
    eid = (expert_id or "afu").strip() or "afu"
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            """
            SELECT memory_text FROM expert_advice_memory
            WHERE user_id = ? AND expert_id = ?
            """,
            (user_id, eid),
        ).fetchone()
        if not row:
            return ""
        return _clip_memory(str(row["memory_text"] or ""), settings)
    finally:
        conn.close()


def save_expert_advice_memory(
    user_id: str,
    expert_id: str,
    memory_text: str,
    settings: Settings | None = None,
) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    settings = settings or get_settings()
    eid = (expert_id or "afu").strip() or "afu"
    init_db(settings)
    now = _utc_now()
    text = _clip_memory(memory_text, settings)
    conn = _connect(settings)
    try:
        if not text:
            conn.execute(
                "DELETE FROM expert_advice_memory WHERE user_id = ? AND expert_id = ?",
                (user_id, eid),
            )
        else:
            conn.execute(
                """
                INSERT INTO expert_advice_memory (user_id, expert_id, memory_text, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, expert_id) DO UPDATE SET
                    memory_text = excluded.memory_text,
                    updated_at = excluded.updated_at
                """,
                (user_id, eid, text, now),
            )
        conn.commit()
    finally:
        conn.close()


def record_usage_event(
    *,
    user_id: str,
    kind: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    estimated: bool = False,
    expert_id: str = "",
    settings: Settings | None = None,
) -> None:
    if not user_id or user_id in ("anonymous", "shared"):
        return
    settings = settings or get_settings()
    pt = max(0, int(prompt_tokens))
    ct = max(0, int(completion_tokens))
    tt = int(total_tokens) if total_tokens is not None else pt + ct
    cost = estimate_cost_cny(pt, ct, settings)
    eid = (expert_id or "").strip()[:64]
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
                prompt_tokens, completion_tokens, total_tokens, estimated, cost_cny,
                expert_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                eid,
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


def _beijing_day_bounds() -> tuple[datetime, datetime, datetime]:
    """Return (yesterday_start, today_start, now) in Beijing tz."""
    now_bj = datetime.now(BEIJING_TZ)
    today_start = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start, today_start, now_bj


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def admin_overview(settings: Settings | None = None) -> dict:
    """Ops overview: registered users, chat-active fans, tokens/cost, invite slots."""
    settings = settings or get_settings()
    init_db(settings)
    yesterday_start, today_start, now_bj = _beijing_day_bounds()
    start_7 = today_start - timedelta(days=6)
    start_30 = today_start - timedelta(days=29)
    y_start = _to_utc_iso(yesterday_start)
    y_end = _to_utc_iso(today_start)
    s7 = _to_utc_iso(start_7)
    s30 = _to_utc_iso(start_30)
    now_iso = _to_utc_iso(now_bj.astimezone(UTC) if now_bj.tzinfo else now_bj)

    conn = _connect(settings)
    try:
        user_count = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])

        def _chat_active(since_iso: str, until_iso: str | None = None) -> int:
            if until_iso:
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT user_id) AS c
                    FROM usage_events
                    WHERE kind = 'chat' AND created_at >= ? AND created_at < ?
                    """,
                    (since_iso, until_iso),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT user_id) AS c
                    FROM usage_events
                    WHERE kind = 'chat' AND created_at >= ?
                    """,
                    (since_iso,),
                ).fetchone()
            return int(row["c"] or 0)

        def _token_cost(since_iso: str, until_iso: str | None = None) -> tuple[int, float]:
            if until_iso:
                row = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(total_tokens), 0) AS tokens,
                        COALESCE(SUM(cost_cny), 0) AS cost
                    FROM usage_events
                    WHERE created_at >= ? AND created_at < ?
                    """,
                    (since_iso, until_iso),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(total_tokens), 0) AS tokens,
                        COALESCE(SUM(cost_cny), 0) AS cost
                    FROM usage_events
                    WHERE created_at >= ?
                    """,
                    (since_iso,),
                ).fetchone()
            return int(row["tokens"] or 0), round(float(row["cost"] or 0), 4)

        active_yesterday = _chat_active(y_start, y_end)
        active_7 = _chat_active(s7)
        active_30 = _chat_active(s30)
        tokens_yesterday, cost_yesterday = _token_cost(y_start, y_end)
        tokens_30, cost_30 = _token_cost(s30)

        used, limit = registration_invite_usage(settings)
        slots = registration_slots_remaining(settings)
        open_risk_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM risk_alerts WHERE status = 'open'"
            ).fetchone()["c"]
            or 0
        )
        from app.experts import list_expert_packs

        packs = list_expert_packs(settings, enabled_only=False)
        enabled_packs = [p for p in packs if p.enabled]
        return {
            "timezone": "Asia/Shanghai",
            "user_count": user_count,
            "expert_count": len(enabled_packs),
            "expert_pack_count": len(packs),
            "active_users_yesterday": active_yesterday,
            "active_users_7d": active_7,
            "active_users_30d": active_30,
            "tokens_yesterday": tokens_yesterday,
            "cost_cny_yesterday": cost_yesterday,
            "tokens_30d": tokens_30,
            "cost_cny_30d": cost_30,
            "open_risk_count": open_risk_count,
            "invite_use_count": used,
            "invite_max_uses": limit,
            "registration_slots_remaining": slots,
            "price_input_cny_per_1m": float(settings.llm_price_input_cny_per_1m),
            "price_output_cny_per_1m": float(settings.llm_price_output_cny_per_1m),
            "cost_note": (
                "费用为按官方单价估算（输入按 cache miss），非账单原件；"
                "活跃=区间内至少发过一次聊天；时间按北京时间"
            ),
            "as_of": now_iso,
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
        risk_rows = conn.execute(
            """
            SELECT user_id, COUNT(*) AS c
            FROM risk_alerts
            WHERE status = 'open'
            GROUP BY user_id
            """
        ).fetchall()
        open_risk_map = {str(r["user_id"]): int(r["c"] or 0) for r in risk_rows}
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
            uid = str(r["id"])
            open_n = open_risk_map.get(uid, 0)
            out.append(
                {
                    "id": uid,
                    "username": str(r["username"]),
                    "created_at": str(r["created_at"]),
                    "is_admin": bool(r["is_admin"]),
                    "thread_count": threads,
                    "message_count": messages,
                    "last_active_at": last_active,
                    "tokens_total": int(r["tokens_total"] or 0),
                    "cost_cny_total": round(float(r["cost_cny_total"] or 0), 4),
                    "usage_events": int(r["usage_events"] or 0),
                    "has_open_risk": open_n > 0,
                    "open_risk_count": open_n,
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
        detail = {
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
        risk_rows = conn.execute(
            """
            SELECT id, user_id, expert_id, created_at, categories, snippet, confidence,
                   status, keyword_hits, reason
            FROM risk_alerts
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()
        detail["recent_risk_alerts"] = [_risk_alert_row_to_dict(r) for r in risk_rows]
        detail["has_open_risk"] = any(
            a.get("status") == "open" for a in detail["recent_risk_alerts"]
        )
        detail["open_risk_count"] = sum(
            1 for a in detail["recent_risk_alerts"] if a.get("status") == "open"
        )
    finally:
        conn.close()
    detail["token_quota"] = get_token_quota(user_id, settings)
    return detail


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


def _thread_expert_id(thread: dict, default_expert_id: str) -> str:
    raw = thread.get("expertId") or thread.get("expert_id") or ""
    eid = str(raw).strip()
    return eid or default_expert_id


def _thread_belongs_to_expert(
    thread: dict,
    *,
    pack_id: str,
    pack_slug: str,
    default_expert_id: str,
) -> bool:
    eid = _thread_expert_id(thread, default_expert_id)
    aliases = {pack_id, pack_slug}
    return eid in aliases


def _parse_state_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _expert_chat_stats_from_states(
    rows: list,
    *,
    pack_id: str,
    pack_slug: str,
    default_expert_id: str,
) -> tuple[int, int]:
    """Return (distinct_user_count, thread_count) for threads belonging to an expert."""
    users: set[str] = set()
    thread_count = 0
    for r in rows:
        state = _parse_state_json(str(r["state_json"] or "") or None)
        if not state:
            continue
        threads = state.get("threads") or []
        if not isinstance(threads, list):
            continue
        uid = str(r["user_id"])
        for t in threads:
            if not isinstance(t, dict):
                continue
            if not _thread_belongs_to_expert(
                t,
                pack_id=pack_id,
                pack_slug=pack_slug,
                default_expert_id=default_expert_id,
            ):
                continue
            msgs = t.get("messages") or []
            if not isinstance(msgs, list) or len(msgs) == 0:
                continue
            thread_count += 1
            users.add(uid)
    return len(users), thread_count


def _expert_token_window(
    conn: sqlite3.Connection,
    *,
    expert_keys: list[str],
    since_iso: str,
) -> tuple[int, float]:
    keys = [k for k in expert_keys if k]
    if not keys:
        return 0, 0.0
    placeholders = ",".join("?" for _ in keys)
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(total_tokens), 0) AS tokens,
            COALESCE(SUM(cost_cny), 0) AS cost
        FROM usage_events
        WHERE expert_id IN ({placeholders}) AND created_at >= ?
        """,
        (*keys, since_iso),
    ).fetchone()
    return int(row["tokens"] or 0), round(float(row["cost"] or 0), 4)


def admin_list_experts(settings: Settings | None = None) -> list[dict]:
    settings = settings or get_settings()
    from app.experts import list_expert_packs
    from app.indexing import read_index_meta
    from app.experts import expert_data_dir, expert_has_pack_knowledge

    init_db(settings)
    packs = list_expert_packs(settings, enabled_only=False)
    default_id = settings.default_expert_id or "afu"
    _y, today_start, _now = _beijing_day_bounds()
    start_7 = today_start - timedelta(days=6)
    start_30 = today_start - timedelta(days=29)
    s7 = _to_utc_iso(start_7)
    s30 = _to_utc_iso(start_30)

    conn = _connect(settings)
    try:
        states = conn.execute(
            "SELECT user_id, state_json FROM user_chat_state"
        ).fetchall()
        out: list[dict] = []
        for pack in packs:
            keys = list({pack.id, pack.slug})
            users_n, threads_n = _expert_chat_stats_from_states(
                states,
                pack_id=pack.id,
                pack_slug=pack.slug,
                default_expert_id=default_id,
            )
            t7, c7 = _expert_token_window(conn, expert_keys=keys, since_iso=s7)
            t30, c30 = _expert_token_window(conn, expert_keys=keys, since_iso=s30)
            meta = read_index_meta(expert_data_dir(pack.slug, settings))
            out.append(
                {
                    "id": pack.id,
                    "slug": pack.slug,
                    "display_name": pack.display_name,
                    "avatar_label": pack.avatar_label,
                    "short_bio": pack.short_bio,
                    "enabled": pack.enabled,
                    "chat_user_count": users_n,
                    "thread_count": threads_n,
                    "tokens_7d": t7,
                    "cost_cny_7d": c7,
                    "tokens_30d": t30,
                    "cost_cny_30d": c30,
                    "index_ready": bool(meta.get("ready")),
                    "index_chunk_count": int(meta.get("chunk_count") or 0),
                    "has_pack_knowledge": expert_has_pack_knowledge(pack),
                }
            )
        return out
    finally:
        conn.close()


def admin_expert_detail(expert_id: str, settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    from app.experts import (
        expert_data_dir,
        expert_has_pack_knowledge,
        load_expert_pack,
    )
    from app.indexing import read_index_meta

    pack = load_expert_pack(expert_id, settings)
    if not pack:
        return None

    default_id = settings.default_expert_id or "afu"
    _yesterday_start, today_start, now_bj = _beijing_day_bounds()
    start_7 = today_start - timedelta(days=6)
    start_30 = today_start - timedelta(days=29)
    s7 = _to_utc_iso(start_7)
    s30 = _to_utc_iso(start_30)
    keys = list({pack.id, pack.slug})

    init_db(settings)
    conn = _connect(settings)
    try:
        states = conn.execute(
            "SELECT user_id, state_json FROM user_chat_state"
        ).fetchall()
        users_n, threads_n = _expert_chat_stats_from_states(
            states,
            pack_id=pack.id,
            pack_slug=pack.slug,
            default_expert_id=default_id,
        )
        t7, c7 = _expert_token_window(conn, expert_keys=keys, since_iso=s7)
        t30, c30 = _expert_token_window(conn, expert_keys=keys, since_iso=s30)
    finally:
        conn.close()

    data_dir = expert_data_dir(pack.slug, settings)
    meta = read_index_meta(data_dir)
    return {
        "id": pack.id,
        "slug": pack.slug,
        "display_name": pack.display_name,
        "avatar_label": pack.avatar_label,
        "short_bio": pack.short_bio,
        "enabled": pack.enabled,
        "scope": pack.scope,
        "chat_user_count": users_n,
        "thread_count": threads_n,
        "usage": {
            "timezone": "Asia/Shanghai",
            "tokens_7d": t7,
            "cost_cny_7d": c7,
            "tokens_30d": t30,
            "cost_cny_30d": c30,
        },
        "index": {
            "ready": bool(meta.get("ready")),
            "chunk_count": int(meta.get("chunk_count") or 0),
            "vector_enabled": bool(meta.get("vector_enabled")),
            "tag_count": int(meta.get("tag_count") or 0),
            "tag_routing_ready": bool(meta.get("tag_routing_ready")),
            "last_indexed_at": meta.get("last_indexed_at"),
            "error": meta.get("error"),
            "data_dir": str(data_dir),
            "has_pack_knowledge": expert_has_pack_knowledge(pack),
            "knowledge_source": (
                "pack" if expert_has_pack_knowledge(pack) else ("vault" if pack.slug == "afu" else "none")
            ),
        },
        "price_input_cny_per_1m": float(settings.llm_price_input_cny_per_1m),
        "price_output_cny_per_1m": float(settings.llm_price_output_cny_per_1m),
        "cost_note": (
            "智能体 token 仅统计带 expert_id 的用量事件（升级后的新对话）；"
            "对话人数/会话数来自云端聊天状态中的 expertId"
        ),
        "as_of": _to_utc_iso(now_bj.astimezone(UTC) if now_bj.tzinfo else now_bj),
    }


RISK_ALERT_THROTTLE_MINUTES = 30


def _parse_json_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _risk_alert_row_to_dict(r: sqlite3.Row, *, username: str | None = None) -> dict:
    out: dict = {
        "id": str(r["id"]),
        "user_id": str(r["user_id"]) if "user_id" in r.keys() else "",
        "expert_id": str(r["expert_id"] or ""),
        "created_at": str(r["created_at"]),
        "categories": _parse_json_list(r["categories"]),
        "snippet": str(r["snippet"] or ""),
        "confidence": str(r["confidence"] or "medium"),
        "status": str(r["status"] or "open"),
        "keyword_hits": _parse_json_list(r["keyword_hits"]),
        "reason": str(r["reason"] or "") if "reason" in r.keys() else "",
    }
    if username is not None:
        out["username"] = username
    elif "username" in r.keys() and r["username"] is not None:
        out["username"] = str(r["username"])
    return out


def list_risk_alerts(
    settings: Settings | None = None,
    *,
    status: str | None = "open",
    limit: int = 50,
) -> list[dict]:
    settings = settings or get_settings()
    init_db(settings)
    lim = max(1, min(int(limit), 200))
    conn = _connect(settings)
    try:
        if status:
            rows = conn.execute(
                """
                SELECT a.id, a.user_id, a.expert_id, a.created_at, a.categories,
                       a.snippet, a.confidence, a.status, a.keyword_hits, a.reason,
                       u.username
                FROM risk_alerts a
                LEFT JOIN users u ON u.id = a.user_id
                WHERE a.status = ?
                ORDER BY a.created_at DESC
                LIMIT ?
                """,
                (status, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.id, a.user_id, a.expert_id, a.created_at, a.categories,
                       a.snippet, a.confidence, a.status, a.keyword_hits, a.reason,
                       u.username
                FROM risk_alerts a
                LEFT JOIN users u ON u.id = a.user_id
                ORDER BY a.created_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        return [_risk_alert_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def ack_risk_alert(alert_id: str, settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    init_db(settings)
    conn = _connect(settings)
    try:
        row = conn.execute(
            """
            SELECT a.id, a.user_id, a.expert_id, a.created_at, a.categories,
                   a.snippet, a.confidence, a.status, a.keyword_hits, a.reason,
                   u.username
            FROM risk_alerts a
            LEFT JOIN users u ON u.id = a.user_id
            WHERE a.id = ?
            """,
            (alert_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE risk_alerts SET status = 'acked' WHERE id = ?",
            (alert_id,),
        )
        conn.commit()
        out = _risk_alert_row_to_dict(row)
        out["status"] = "acked"
        return out
    finally:
        conn.close()


def try_create_risk_alert(
    *,
    user_id: str,
    expert_id: str,
    categories: list[str],
    snippet: str,
    confidence: str,
    keyword_hits: list[str],
    reason: str = "",
    settings: Settings | None = None,
) -> dict | None:
    """
    Insert an open risk alert unless a similar open alert exists within the throttle window.
    Returns the created alert dict, or None if throttled / skipped.
    """
    settings = settings or get_settings()
    cats = [str(c).strip() for c in categories if str(c).strip()]
    if not cats:
        cats = ["other_emergency"]
    init_db(settings)
    since = (datetime.now(UTC) - timedelta(minutes=RISK_ALERT_THROTTLE_MINUTES)).isoformat()
    conn = _connect(settings)
    try:
        recent = conn.execute(
            """
            SELECT categories FROM risk_alerts
            WHERE user_id = ? AND status = 'open' AND created_at >= ?
            """,
            (user_id, since),
        ).fetchall()
        cat_set = set(cats)
        for r in recent:
            existing = set(_parse_json_list(r["categories"]))
            if existing & cat_set:
                return None
        alert_id = uuid.uuid4().hex
        created_at = _utc_now()
        conn.execute(
            """
            INSERT INTO risk_alerts (
                id, user_id, expert_id, created_at, categories, snippet,
                confidence, status, keyword_hits, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                alert_id,
                user_id,
                (expert_id or "").strip(),
                created_at,
                json.dumps(cats, ensure_ascii=False),
                (snippet or "")[:200],
                (confidence or "medium").strip() or "medium",
                json.dumps([str(k) for k in keyword_hits], ensure_ascii=False),
                (reason or "")[:300],
            ),
        )
        conn.commit()
        return {
            "id": alert_id,
            "user_id": user_id,
            "expert_id": (expert_id or "").strip(),
            "created_at": created_at,
            "categories": cats,
            "snippet": (snippet or "")[:200],
            "confidence": (confidence or "medium").strip() or "medium",
            "status": "open",
            "keyword_hits": [str(k) for k in keyword_hits],
            "reason": (reason or "")[:300],
        }
    finally:
        conn.close()