from __future__ import annotations

import io
import logging
import threading
import time
import zipfile
from pathlib import Path

import boto3
from botocore.config import Config

from app.settings import Settings, get_settings
from app.users_store import users_db_path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_sync_at = 0.0
MIN_INTERVAL_SEC = 90.0


def _r2_settings(settings: Settings) -> tuple[str, str, str, str, str]:
    return (
        settings.backup_r2_account_id.strip(),
        settings.backup_r2_access_key_id.strip(),
        settings.backup_r2_secret_access_key.strip(),
        settings.backup_r2_bucket_name.strip(),
        settings.backup_r2_object_key.strip() or "relationship-expert-users.zip",
    )


def r2_sync_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    account, key_id, secret, bucket, _obj = _r2_settings(settings)
    return bool(account and key_id and secret and bucket)


def sync_secret(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return settings.users_db_sync_secret.strip() or settings.app_password.strip()


def _users_zip_bytes(db_path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, "users.db")
    return buf.getvalue()


def _r2_client(settings: Settings):
    account, key_id, secret, _bucket, _obj = _r2_settings(settings)
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def sync_users_db_to_r2(settings: Settings | None = None, *, force: bool = False) -> bool:
    """Upload users.db zip to R2. Returns True when upload succeeded."""
    global _last_sync_at
    settings = settings or get_settings()
    if not r2_sync_configured(settings):
        return False

    now = time.monotonic()
    if not force and now - _last_sync_at < MIN_INTERVAL_SEC:
        return False

    db_path = users_db_path(settings)
    if not db_path.is_file():
        return False

    _account, _key_id, _secret, bucket, obj_key = _r2_settings(settings)

    with _lock:
        now = time.monotonic()
        if not force and now - _last_sync_at < MIN_INTERVAL_SEC:
            return False
        try:
            payload = _users_zip_bytes(db_path)
            client = _r2_client(settings)
            client.put_object(
                Bucket=bucket,
                Key=obj_key,
                Body=payload,
                ContentType="application/zip",
            )
            _last_sync_at = time.monotonic()
            logger.info("Synced users.db to R2 (%s bytes)", len(payload))
            return True
        except Exception:
            logger.exception("Failed to sync users.db to R2")
            return False


def schedule_users_db_sync(settings: Settings | None = None, *, force: bool = False) -> None:
    """Fire-and-forget sync (for BackgroundTasks)."""
    sync_users_db_to_r2(settings, force=force)
