from __future__ import annotations

import io
import zipfile
from urllib.request import urlopen

from app.indexing import read_index_meta
from app.settings import Settings, get_settings
from app.users_store import bootstrap_users, has_users, init_db, users_db_path


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
    with urlopen(url, timeout=120) as resp:  # noqa: S310
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(data_dir)


def ensure_users_db(settings: Settings | None = None) -> None:
    """Download users.db from USERS_DB_URL when the local file is missing."""
    settings = settings or get_settings()
    path = users_db_path(settings)
    if path.is_file():
        init_db(settings)
        return

    url = (settings.users_db_url or "").strip()
    if not url:
        init_db(settings)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url, timeout=120) as resp:  # noqa: S310
        payload = resp.read()
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for name in zf.namelist():
                if name.endswith("users.db"):
                    path.write_bytes(zf.read(name))
                    init_db(settings)
                    return
        raise RuntimeError("users.db not found inside zip from USERS_DB_URL")
    path.write_bytes(payload)
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
