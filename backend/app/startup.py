from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

from app.indexing import read_index_meta
from app.settings import Settings, get_settings
from app.users_db_sync import restore_users_db_from_r2, r2_sync_configured
from app.users_store import apply_admin_usernames, bootstrap_users, has_users, init_db, users_db_path


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


def _parse_index_bundle_urls(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(obj, dict):
            return {}
        return {
            str(k).strip(): str(v).strip()
            for k, v in obj.items()
            if str(k).strip() and str(v).strip()
        }
    out: dict[str, str] = {}
    for part in text.split(","):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        eid, url = piece.split("=", 1)
        eid = eid.strip()
        url = url.strip()
        if eid and url:
            out[eid] = url
    return out


def ensure_expert_index_bundles(settings: Settings | None = None) -> None:
    """Download per-expert index zips into data/experts/{id} when missing."""
    settings = settings or get_settings()
    mapping = _parse_index_bundle_urls(settings.index_bundle_urls)
    if not mapping:
        return
    base = settings.data_dir.resolve()
    for eid, url in mapping.items():
        dest = base / "experts" / eid
        meta = read_index_meta(dest)
        if meta.get("ready") or (dest / "chunks.jsonl").is_file():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        try:
            payload = _fetch_url(url, settings)
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                zf.extractall(dest)
            logger.info("Restored expert index for %s from INDEX_BUNDLE_URLS", eid)
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
            logger.warning("Could not restore expert index for %s (%s)", eid, exc)


def seed_expert_indexes_from_packs(settings: Settings | None = None) -> None:
    """Copy experts/<id>/index/* into data/experts/<id> when the pack ships a local index."""
    settings = settings or get_settings()
    from app.experts import experts_root

    root = experts_root(settings)
    if not root.is_dir():
        return
    base = settings.data_dir.resolve()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        pack_index = child / "index"
        if not pack_index.is_dir():
            continue
        chunks_src = pack_index / "chunks.jsonl"
        if not chunks_src.is_file():
            continue
        dest = base / "experts" / child.name
        if (dest / "chunks.jsonl").is_file():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for item in pack_index.iterdir():
            if item.is_file():
                (dest / item.name).write_bytes(item.read_bytes())
        logger.info("Seeded expert index for %s from pack index/", child.name)


def prepare_runtime_data(settings: Settings | None = None) -> None:
    ensure_index_bundle(settings)
    ensure_expert_index_bundles(settings)
    seed_expert_indexes_from_packs(settings)
    ensure_users_db(settings)
    bootstrap_accounts(settings)
    apply_admin_usernames(settings)


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
    if path.is_file() and has_users(settings):
        return False
    url = (settings.index_bundle_url or "").strip()
    if not url:
        return False
    try:
        payload = _fetch_url(url, settings)
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
        logger.warning("Could not restore users.db from INDEX_BUNDLE_URL (%s)", exc)
        return False
    if _extract_users_db_from_zip(payload, path):
        init_db(settings)
        return has_users(settings)
    return False


def _restore_users_db_from_url(settings: Settings, path: Path) -> bool:
    url = (settings.users_db_url or "").strip()
    if not url:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _fetch_url(url, settings)
        if payload[:2] == b"PK":
            if not _extract_users_db_from_zip(payload, path):
                logger.warning("users.db not found inside zip from USERS_DB_URL")
                return False
        else:
            path.write_bytes(payload)
        init_db(settings)
        return has_users(settings)
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
        logger.warning(
            "Could not restore users.db from USERS_DB_URL (%s); will try R2 API if configured.",
            exc,
        )
        return False


def ensure_users_db(settings: Settings | None = None) -> None:
    """Restore users.db: keep non-empty local DB, else R2 S3 → USERS_DB_URL → legacy."""
    settings = settings or get_settings()
    path = users_db_path(settings)

    if path.is_file():
        init_db(settings)
        if has_users(settings):
            return
        logger.warning("Local users.db exists but has no accounts; attempting restore from backup.")

    # Prefer authenticated R2 download (public USERS_DB_URL often 403 on private buckets).
    if r2_sync_configured(settings):
        if restore_users_db_from_r2(settings):
            init_db(settings)
            if has_users(settings):
                return
            logger.warning("R2 restore succeeded but users.db still has no accounts.")

    if _restore_users_db_from_url(settings, path):
        return

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
