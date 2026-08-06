"""Exercises the real /admin/audit/run fast-fail paths -- this is the
endpoint behind the dashboard's "Audit starten" button, which the user
reported finishing "instantly". The three fast-fail causes (audit
disabled, database logging disabled, no matching rows) all now return
distinct, actionable error messages instead of one generic
"no rows" message that sent people chasing the wrong fix.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(llmproxy_module):
    return TestClient(llmproxy_module.app), {"X-Admin-Token": llmproxy_module._ADMIN_TOKEN}


@pytest.fixture(autouse=True)
def _restore_audit_and_logging_cfg(llmproxy_module):
    llmproxy = llmproxy_module
    orig_audit = dict(llmproxy._audit_cfg)
    orig_logging = dict(llmproxy._logging_cfg)
    yield
    llmproxy._audit_cfg = orig_audit
    llmproxy._logging_cfg = orig_logging
    llmproxy._db().execute("DELETE FROM requests")
    llmproxy._db().commit()


def test_audit_disabled_returns_distinct_error(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._audit_cfg["enabled"] = False
    r = client.post("/admin/audit/run", headers=headers, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "deaktiviert" in body["error"]
    assert "audit.yaml" in body["error"]


def test_logging_disabled_returns_distinct_error_not_confused_with_empty_filter(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._audit_cfg["enabled"] = True
    llmproxy_module._logging_cfg["enabled"] = False
    r = client.post("/admin/audit/run", headers=headers, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Database Logging" in body["error"]


def test_empty_filter_with_logging_enabled_returns_no_rows_error(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._audit_cfg["enabled"] = True
    llmproxy_module._logging_cfg["enabled"] = True
    r = client.post("/admin/audit/run", headers=headers, json={"token_name": "nobody-has-this-name"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Zeitraum" in body["error"]


def test_audit_requires_admin(llmproxy_module):
    client = TestClient(llmproxy_module.app)
    r = client.post("/admin/audit/run", json={})
    assert r.status_code == 403
