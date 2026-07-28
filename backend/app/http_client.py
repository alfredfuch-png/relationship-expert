from __future__ import annotations

import httpx

from app.settings import Settings


def gateway_proxy(settings: Settings) -> str | None:
    p = (settings.https_proxy or settings.http_proxy or "").strip()
    return p or None


def async_gateway_client(settings: Settings, *, timeout_seconds: float) -> httpx.AsyncClient:
    proxy = gateway_proxy(settings)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        proxy=proxy,
        trust_env=proxy is None,
    )


def format_gateway_connect_error(exc: BaseException, *, host_hint: str = "AI API") -> str:
    detail = str(exc).strip() or repr(exc)
    return (
        f"无法连接 {host_hint}。"
        "若本机无法直连，请开启 VPN，或在 .env 中设置 HTTPS_PROXY"
        "（例如 HTTPS_PROXY=http://127.0.0.1:7890）。"
        f" 技术信息：{detail}"
    )
