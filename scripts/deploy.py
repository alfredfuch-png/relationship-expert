#!/usr/bin/env python3
"""Deploy to ai-builders.space via POST /v1/deployments."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_URL = "https://space.ai-builders.com/backend/v1/deployments"


def _read_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _load_token() -> str:
    token = _read_dotenv().get("AI_BUILDER_TOKEN", "") or os.getenv("AI_BUILDER_TOKEN", "").strip()
    if not token:
        print("AI_BUILDER_TOKEN missing (.env or environment).", file=sys.stderr)
        sys.exit(1)
    return token


def _merge_env_vars(cfg: dict) -> dict[str, str]:
    env = dict(cfg.get("env_vars") or {})
    for key in (
        "APP_PASSWORD",
        "SESSION_SECRET",
        "INDEX_BUNDLE_URL",
        "USERS_DB_URL",
        "USERS_BOOTSTRAP",
        "ALLOW_REGISTRATION",
        "REGISTRATION_INVITE_CODE",
        "REGISTRATION_INVITE_MAX_USES",
        "HTTPS_PROXY",
        "HTTP_PROXY",
    ):
        val = _read_dotenv().get(key, "").strip()
        if val and key not in env:
            env[key] = val
    return env


def main() -> None:
    cfg_path = ROOT / "deploy-config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    token = _load_token()
    body = {
        "repo_url": cfg["repo_url"],
        "service_name": cfg["service_name"],
        "branch": cfg["branch"],
        "port": cfg.get("port", 8000),
    }
    env_vars = _merge_env_vars(cfg)
    if env_vars:
        body["env_vars"] = env_vars
    if not env_vars.get("APP_PASSWORD") and cfg.get("service_name"):
        print(
            "Warning: APP_PASSWORD not set — public site will stay open without login.",
            file=sys.stderr,
        )
    if cfg.get("streaming_log_timeout_seconds"):
        body["streaming_log_timeout_seconds"] = cfg["streaming_log_timeout_seconds"]

    print(f"Deploying {body['service_name']} from {body['repo_url']} ({body['branch']})…")
    with httpx.Client(timeout=httpx.Timeout(180.0)) as client:
        resp = client.post(
            DEPLOY_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
    print(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(resp.text[:2000])
    if resp.status_code >= 400:
        sys.exit(1)
    url = f"https://{cfg['service_name']}.ai-builders.space"
    print(f"\nWhen healthy, open: {url}")


if __name__ == "__main__":
    main()
