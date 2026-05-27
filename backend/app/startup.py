from __future__ import annotations

import io
import zipfile
from pathlib import Path
from urllib.request import urlopen

from app.indexing import read_index_meta
from app.settings import Settings, get_settings


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
