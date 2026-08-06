"""Exercises the real `_required_roles` role-mapping function and the
`AuthGateMiddleware` request-gating behavior (dashboard/app.py) -- not
authenticated -> redirect/401, wrong role -> 403 (not a redirect, which
would loop against /login), correct role -> the route runs.
"""
import respx
import httpx
import pytest
from fastapi.testclient import TestClient


def test_required_roles_admin_only_paths(dashboard_module):
    assert dashboard_module._required_roles("/admin") == {"admin"}
    assert dashboard_module._required_roles("/settings") == {"admin"}
    assert dashboard_module._required_roles("/api/admin/guardrails") == {"admin"}


def test_required_roles_finance_or_admin_paths(dashboard_module):
    assert dashboard_module._required_roles("/chargeback") == {"admin", "finance"}
    assert dashboard_module._required_roles("/api/admin/chargeback/summary") == {"admin", "finance"}


def test_required_roles_default_any_authenticated_role(dashboard_module):
    assert dashboard_module._required_roles("/") == {"admin", "finance", "viewer"}
    assert dashboard_module._required_roles("/inference") == {"admin", "finance", "viewer"}


def test_safe_next_blocks_protocol_relative_urls(dashboard_module):
    assert dashboard_module._safe_next("//evil.com/phish") == "/"
    assert dashboard_module._safe_next("/\\evil.com") == "/"
    assert dashboard_module._safe_next("not-a-path") == "/"
    assert dashboard_module._safe_next("") == "/"
    assert dashboard_module._safe_next("/chargeback") == "/chargeback"


@pytest.fixture
def client(dashboard_module):
    return TestClient(dashboard_module.app, base_url="http://testserver")


def test_healthz_is_public_without_login(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unauthenticated_html_route_redirects_to_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_unauthenticated_api_route_returns_401_json(client):
    r = client.get("/api/admin/clients")
    assert r.status_code == 401
    assert r.json() == {"error": "Not authenticated"}


def _login(client, dashboard_module, role: str):
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/auth/verify").mock(
            return_value=httpx.Response(200, json={"ok": True, "role": role})
        )
        r = client.post("/login", data={"username": "u", "password": "p", "next": "/"},
                         follow_redirects=False)
        assert r.status_code == 303
    return client


def test_viewer_role_forbidden_on_admin_only_route(client, dashboard_module):
    _login(client, dashboard_module, "viewer")
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 403
    assert "Kein Zugriff" in r.text


def test_viewer_role_forbidden_on_admin_only_api_route_returns_json(client, dashboard_module):
    _login(client, dashboard_module, "viewer")
    r = client.get("/api/admin/guardrails")
    assert r.status_code == 403
    assert r.json() == {"error": "Forbidden"}


def test_admin_role_allowed_on_admin_only_route(client, dashboard_module):
    _login(client, dashboard_module, "admin")
    r = client.get("/admin")
    assert r.status_code == 200


def test_finance_role_allowed_on_chargeback_but_not_admin_route(client, dashboard_module):
    _login(client, dashboard_module, "finance")
    r_chargeback = client.get("/chargeback")
    assert r_chargeback.status_code == 200
    r_admin = client.get("/admin", follow_redirects=False)
    assert r_admin.status_code == 403


def test_viewer_role_allowed_on_default_route(client, dashboard_module):
    _login(client, dashboard_module, "viewer")
    r = client.get("/inference")
    assert r.status_code == 200
