#!/usr/bin/env python3
"""Upload relationship-expert-index.zip as a GitHub Release asset (not committed to git)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ZIP_PATH = ROOT / "relationship-expert-index.zip"
REPO = "alfredfuch-png/relationship-expert"
TAG = "index-v1"


def _github_token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("No GitHub token from git credential.")


def main() -> None:
    if not ZIP_PATH.is_file():
        print(f"Missing {ZIP_PATH}; run scripts/package_index.ps1 first.", file=sys.stderr)
        sys.exit(1)

    token = _github_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(timeout=120.0) as client:
        existing = client.get(
            f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}",
            headers=headers,
        )
        if existing.status_code == 200:
            release = existing.json()
            release_id = release["id"]
            print(f"Release {TAG} already exists (id={release_id}).")
        else:
            resp = client.post(
                f"https://api.github.com/repos/{REPO}/releases",
                headers=headers,
                json={
                    "tag_name": TAG,
                    "name": "RAG index bundle (production)",
                    "body": "Pre-built index for hosted Romance Expert. Not Obsidian source files.",
                    "draft": False,
                    "prerelease": True,
                },
            )
            resp.raise_for_status()
            release = resp.json()
            release_id = release["id"]
            print(f"Created release {TAG} (id={release_id}).")

        for asset in release.get("assets") or []:
            if asset.get("name") == ZIP_PATH.name:
                print(asset["browser_download_url"])
                return

        upload_headers = {
            **headers,
            "Content-Type": "application/zip",
        }
        data = ZIP_PATH.read_bytes()
        up = client.post(
            f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets",
            params={"name": ZIP_PATH.name},
            headers=upload_headers,
            content=data,
        )
        up.raise_for_status()
        url = up.json()["browser_download_url"]
        print(url)


if __name__ == "__main__":
    main()
