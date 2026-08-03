"""Smoke-test expert packs + public list (no LLM calls)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.experts import (  # noqa: E402
    expert_data_dir,
    expert_has_pack_knowledge,
    experts_public_list,
    list_expert_packs,
    load_expert_pack,
    resolve_expert,
)
from app.settings import get_settings  # noqa: E402


def main() -> int:
    s = get_settings()
    packs = list_expert_packs(s, enabled_only=False)
    enabled = list_expert_packs(s, enabled_only=True)
    public = experts_public_list(s)
    afu = load_expert_pack("afu", s)
    socio = load_expert_pack("prof_socio", s)
    by_manifest = load_expert_pack(socio.id, s) if socio else None
    resolved = resolve_expert("afu", s)
    data = expert_data_dir("afu", s)
    socio_data = expert_data_dir(socio.id, s) if socio else None

    print(f"packs_total={len(packs)} enabled={len(enabled)} public={len(public)}")
    print(f"afu_ok={bool(afu and afu.enabled and afu.persona and afu.questions_guide)}")
    print(
        f"prof_socio_present={bool(socio)} id={getattr(socio, 'id', None)} "
        f"enabled={bool(socio and socio.enabled)} "
        f"has_knowledge={bool(socio and expert_has_pack_knowledge(socio))}"
    )
    print(f"resolve_afu={resolved.id} data_dir={data}")
    print(f"prof_socio_data_dir={socio_data}")
    print(f"public_ids={[p['id'] for p in public]}")

    ok = bool(afu and afu.enabled and afu.persona and afu.questions_guide)
    ok = ok and resolved.id == "afu"
    ok = ok and any(p["id"] == "afu" for p in public)
    ok = ok and bool(socio) and socio.id == "prof_socio" and socio.enabled
    ok = ok and by_manifest is not None and by_manifest.slug == "prof_socio"
    ok = ok and socio_data is not None and socio_data.name == "prof_socio"
    ok = ok and expert_has_pack_knowledge(socio)
    ok = ok and any(p["id"] == "prof_socio" for p in public)
    # Indexes must stay isolated.
    from app.indexing import read_index_meta

    socio_meta = read_index_meta(socio_data)
    afu_meta = read_index_meta(data)
    ok = ok and bool(socio_meta.get("ready")) and int(socio_meta.get("chunk_count") or 0) > 0
    ok = ok and bool(afu_meta.get("ready"))
    ok = ok and socio_data.resolve() != data.resolve()
    if not ok:
        print("SMOKE FAIL", file=sys.stderr)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
