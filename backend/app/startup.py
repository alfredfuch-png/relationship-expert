from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

from app.indexing import read_index_meta
from app.settings import Settings, get_settings
from app.users_store import bootstrap_users, has_users, init_db, users_db_path


def _fetch_url(url: str, settings: Settings, *, timeout: int = 120) -> bytes:
    headers: dict[str, str] = {}
    token = (settings.users_db_bearer_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def ensure_index_bundle(settings: Settings | None = None) -> None:
    """If INDEX_BUNDLE_URL is set and data/ is empty, download a zip of the index."""
    settings = settings or get_settings()
    url = (settings.index_bundle_url or "").strip()
    if not url:
        return

    data_dir = settings.data_dir.resolve()
    meta = read_index_meta(data_dir)
    if meta.get("ready"):
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    payload = _fetch_url(url, settings)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(data_dir)


def _extract_users_db_from_zip(payload: bytes, dest: Path) -> bool:
    if payload[:2] != b"PK":
        return False
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for name in zf.namelist():
            if name.endswith("users.db"):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
                return True
    return False


def _restore_users_db_from_index_bundle(settings: Settings) -> bool:
    """Legacy: only if an old public index zip still contains users.db."""
    path = users_db_path(settings)
    if path.is_file():
        return False
    url = (settings.index_bundle_url or "").strip()
    if not url:
        return False
    payload = _fetch_url(url, settings)
    if _extract_users_db_from_zip(payload, path):
        init_db(settings)
        return True
    return False


def ensure_users_db(settings: Settings | None = None) -> None:
    """Restore users.db from USERS_DB_URL (private), not from public index bundle."""
    settings = settings or get_settings()
    path = users_db_path(settings)
    if path.is_file():
        init_db(settings)
        return

    url = (settings.users_db_url or "").strip()
    if url:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = _fetch_url(url, settings)
            if payload[:2] == b"PK":
                if not _extract_users_db_from_zip(payload, path):
                    logger.warning("users.db not found inside zip from USERS_DB_URL")
                else:
                    init_db(settings)
                    return
            else:
                path.write_bytes(payload)
                init_db(settings)
                return
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
            logger.warning(
                "Could not restore users.db from USERS_DB_URL (%s); starting with empty database.",
                exc,
            )

    if _restore_users_db_from_index_bundle(settings):
        return

    init_db(settings)


def bootstrap_accounts(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    init_db(settings)
    if has_users(settings):
        return
    spec = (settings.users_bootstrap or "").strip()
    if spec:
        bootstrap_users(spec, settings)


def prepare_runtime_data(settings: Settings | None = None) -> None:
    ensure_index_bundle(settings)
    ensure_users_db(settings)
    bootstrap_accounts(settings)
