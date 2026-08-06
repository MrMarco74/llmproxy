"""Thin HTTP client for llmproxy's admin API.

Authenticates with `Authorization: Bearer <key_id>.<secret>` against the
`api_keys` table's RBAC roles (admin/finance/viewer/automation) -- create a
key via `POST /admin/api_keys` with `role=automation`, `owner_type=service`.
The `automation` role is deliberately narrower than admin: guardrails,
client management, bans, log/chargeback reads. It cannot touch
/maintenance/* (stop-all, reboot, evict, etc.) or chargeback pricing.
"""

import os

import httpx


class LLMProxyConfigError(RuntimeError):
    pass


def client() -> httpx.Client:
    url = os.environ.get("LLMPROXY_URL")
    api_key = os.environ.get("LLMPROXY_API_KEY")
    if not url:
        raise LLMProxyConfigError("LLMPROXY_URL is not set (e.g. https://llmproxy.internal.example.com:11435)")
    if not api_key:
        raise LLMProxyConfigError(
            "LLMPROXY_API_KEY is not set (create one via POST /admin/api_keys, role=automation)"
        )
    return httpx.Client(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
