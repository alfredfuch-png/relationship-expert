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


def _load_token() -> str:
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AI_BUILDER_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    token = os.getenv("AI_BUILDER_TOKEN", "").strip()
    if not token:
        print("AI_BUILDER_TOKEN missing (.env or environment).", file=sys.stderr)
        sys.exit(1)
    return token


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
    if cfg.get("env_vars"):
        body["env_vars"] = cfg["env_vars"]
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
