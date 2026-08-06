"""Exercises the real /maintenance/* endpoints in proxy/llmproxy.py that
don't touch subprocess/sockets (those are explicitly out of scope, see
the test-suite-expansion plan) -- logging toggle, resume, ollama-lock/
-unlock (respx-mocked model eviction), gaming_override (respx-mocked
gpu-agent call), cleanup, and strip-prompts.

Also covers _log_admin_action and _set_ollama_lock directly: both had
real bugs (a 6-column/5-placeholder INSERT, and a token_name kwarg the
function didn't accept) found and fixed while writing these tests --
every /maintenance/ollama-lock|-unlock call was previously crashing with
a 500, and the admin_actions audit trail was silently never written.
"""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(llmproxy_module):
    return TestClient(llmproxy_module.app), {"X-Admin-Token": llmproxy_module._ADMIN_TOKEN}


@pytest.fixture(autouse=True)
def _cleanup(llmproxy_module):
    yield
    llmproxy = llmproxy_module
    llmproxy._db().execute("DELETE FROM admin_actions")
    llmproxy._db().execute("DELETE FROM requests")
    llmproxy._db().execute("DELETE FROM notifications")
    llmproxy._db().commit()
    llmproxy._stop_all = False
    llmproxy._ollama_locked = False
    llmproxy._loaded_models = []


# ── _log_admin_action (regression test for the 6-col/5-placeholder bug) ──

def test_log_admin_action_writes_all_columns(llmproxy_module):
    llmproxy_module._log_admin_action("test-action", "admin", "1.2.3.4", "alice", "some detail")
    row = llmproxy_module._db().execute(
        "SELECT action, source, client_ip, token_name, detail FROM admin_actions WHERE action='test-action'"
    ).fetchone()
    assert row == ("test-action", "admin", "1.2.3.4", "alice", "some detail")


# ── /maintenance/logging ─────────────────────────────────────────────────

def test_get_logging_config(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._logging_cfg["enabled"] = True
    r = client.get("/maintenance/logging", headers=headers)
    assert r.json() == {"enabled": True}


def test_set_logging_config_persists_and_logs_action(llmproxy_module, admin_client):
    client, headers = admin_client
    r = client.post("/maintenance/logging", headers=headers, json={"enabled": False})
    assert r.json() == {"ok": True, "enabled": False}
    assert llmproxy_module._logging_cfg["enabled"] is False
    row = llmproxy_module._db().execute("SELECT action FROM admin_actions WHERE action='logging'").fetchone()
    assert row is not None
    llmproxy_module._logging_cfg["enabled"] = True  # restore for other tests


def test_logging_requires_admin(llmproxy_module):
    client = TestClient(llmproxy_module.app)
    r = client.get("/maintenance/logging")
    assert r.status_code == 403


# ── /maintenance/resume ──────────────────────────────────────────────────

def test_resume_clears_stop_all_and_logs(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._stop_all = True
    r = client.post("/maintenance/resume", headers=headers)
    assert r.json() == {"ok": True, "stop_all": False}
    assert llmproxy_module._stop_all is False
    row = llmproxy_module._db().execute("SELECT action FROM admin_actions WHERE action='resume'").fetchone()
    assert row is not None


# ── /maintenance/ollama-lock / -unlock ───────────────────────────────────

def test_ollama_lock_evicts_models_and_persists_state(llmproxy_module, admin_client, tmp_path):
    client, headers = admin_client
    llmproxy_module._loaded_models = [{"name": "qwen3:8b", "upstream": 0}]

    with respx.mock(base_url=llmproxy_module.OLLAMA_UPSTREAM_0) as mock:
        mock.post("/api/generate").mock(return_value=httpx.Response(200, json={}))
        r = client.post("/maintenance/ollama-lock", headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ollama_locked"] is True
    assert body["evicted"] == ["qwen3:8b"]
    assert llmproxy_module._ollama_locked is True
    row = llmproxy_module._db().execute(
        "SELECT action, detail FROM admin_actions WHERE action='ollama-lock'"
    ).fetchone()
    assert row is not None
    assert "evicted=" in row[1]  # detail column, not token_name -- the bug this regresses


def test_ollama_unlock_does_not_evict_and_persists_state(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._ollama_locked = True
    r = client.post("/maintenance/ollama-unlock", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "ollama_locked": False}
    assert llmproxy_module._ollama_locked is False
    row = llmproxy_module._db().execute(
        "SELECT detail FROM admin_actions WHERE action='ollama-unlock'"
    ).fetchone()
    assert row is not None
    assert row[0]  # detail column populated (was landing in token_name before the fix)


# ── /maintenance/gaming_override ─────────────────────────────────────────

def test_gaming_override_forwards_to_gpu_agent(llmproxy_module, admin_client):
    client, headers = admin_client
    with respx.mock(base_url=llmproxy_module.GPU_AGENT_URL) as mock:
        route = mock.post("/gaming_override").mock(
            return_value=httpx.Response(200, json={"override": "on"})
        )
        r = client.post("/maintenance/gaming_override", headers=headers, json={"override": "on"})
    assert r.status_code == 200
    assert r.json() == {"override": "on"}
    import json as _json
    assert _json.loads(route.calls.last.request.content) == {"override": "on"}


# ── /maintenance/cleanup & /maintenance/strip-prompts ────────────────────

def test_cleanup_full_deletes_all_rows(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._db().execute(
        "INSERT INTO requests (ts, date, model) VALUES ('2020-01-01T00:00:00', '2020-01-01', 'm')"
    )
    llmproxy_module._db().commit()
    r = client.post("/maintenance/cleanup", headers=headers, params={"full": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"]["requests"] >= 1
    remaining = llmproxy_module._db().execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert remaining == 0


def test_strip_prompts_clears_old_text_but_keeps_metrics(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._db().execute(
        "INSERT INTO requests (ts, date, model, total_tokens, prompt_text, response_text) "
        "VALUES ('2020-01-01T00:00:00', '2020-01-01', 'm', 42, 'old prompt', 'old response')"
    )
    llmproxy_module._db().commit()
    r = client.post("/maintenance/strip-prompts", headers=headers, params={"older_than_days": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stripped_rows"] >= 1
    row = llmproxy_module._db().execute(
        "SELECT total_tokens, prompt_text, response_text FROM requests WHERE date='2020-01-01'"
    ).fetchone()
    assert row[0] == 42  # metrics untouched
    assert row[1] is None
    assert row[2] is None
