"""Exercises the dashboard's /api/logging passthrough to the real proxy
backend, respx-mocked -- the same style used in mcp_server/tests. Serves
as the template the new /api/splunk passthrough's tests will follow.
"""
import httpx
import respx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(dashboard_module):
    return TestClient(dashboard_module.app, base_url="http://testserver")


def _login_admin(client, dashboard_module):
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/auth/verify").mock(
            return_value=httpx.Response(200, json={"ok": True, "role": "admin"})
        )
        client.post("/login", data={"username": "u", "password": "p", "next": "/"},
                     follow_redirects=False)


def test_get_logging_config_passes_through(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.get("/maintenance/logging").mock(
            return_value=httpx.Response(200, json={"enabled": True})
        )
        r = client.get("/api/logging")
    assert r.status_code == 200
    assert r.json() == {"enabled": True}


def test_set_logging_config_forwards_body(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        route = mock.post("/maintenance/logging").mock(
            return_value=httpx.Response(200, json={"enabled": False})
        )
        r = client.post("/api/logging", json={"enabled": False})
    assert r.status_code == 200
    assert r.json() == {"enabled": False}
    import json as _json
    assert _json.loads(route.calls.last.request.content) == {"enabled": False}


def test_get_logging_config_degrades_gracefully_on_backend_error(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.get("/maintenance/logging").mock(side_effect=httpx.ConnectError("refused"))
        r = client.get("/api/logging")
    assert r.status_code == 200
    assert r.json() == {"enabled": True}


def test_logging_api_requires_admin_role(client, dashboard_module):
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/auth/verify").mock(
            return_value=httpx.Response(200, json={"ok": True, "role": "viewer"})
        )
        client.post("/login", data={"username": "u", "password": "p", "next": "/"},
                     follow_redirects=False)
    r = client.get("/api/logging")
    assert r.status_code == 403


def test_get_splunk_config_passes_through(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.get("/admin/splunk/config").mock(
            return_value=httpx.Response(200, json={"enabled": False, "url": "", "index": "", "token": ""})
        )
        r = client.get("/api/splunk")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_set_splunk_config_forwards_body(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        route = mock.post("/admin/splunk/config").mock(
            return_value=httpx.Response(200, json={"enabled": True, "url": "https://splunk.test:8088"})
        )
        r = client.post("/api/splunk", json={"enabled": True, "url": "https://splunk.test:8088"})
    assert r.status_code == 200
    import json as _json
    assert _json.loads(route.calls.last.request.content) == {"enabled": True, "url": "https://splunk.test:8088"}


def test_splunk_test_passes_through(client, dashboard_module):
    _login_admin(client, dashboard_module)
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/splunk/test").mock(return_value=httpx.Response(200, json={"ok": True, "detail": "ok"}))
        r = client.post("/api/splunk/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_splunk_api_requires_admin_role(client, dashboard_module):
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/auth/verify").mock(
            return_value=httpx.Response(200, json={"ok": True, "role": "finance"})
        )
        client.post("/login", data={"username": "u", "password": "p", "next": "/"},
                     follow_redirects=False)
    r = client.get("/api/splunk")
    assert r.status_code == 403
