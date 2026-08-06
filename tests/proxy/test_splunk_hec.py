"""Exercises the real Splunk HEC export path in proxy/llmproxy.py:
_push_to_splunk_hec() directly, and the /admin/splunk/config + /test
endpoints via TestClient (respx-mocked against the fake HEC URL)."""
import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_splunk_state(llmproxy_module):
    llmproxy = llmproxy_module
    original_cfg = dict(llmproxy._splunk_cfg)
    yield
    llmproxy._splunk_cfg = original_cfg
    llmproxy._delete_secret("splunk.hec_token")


def test_push_to_hec_returns_false_when_unconfigured(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._splunk_cfg = {"enabled": True, "url": "", "index": "", "verify_tls": True}
    ok, detail = asyncio.run(llmproxy._push_to_splunk_hec({"event_type": "x"}))
    assert ok is False


def test_push_to_hec_sends_authorization_and_event_body(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._splunk_cfg = {"enabled": True, "url": "https://splunk.test:8088",
                             "index": "llmproxy", "verify_tls": True}
    llmproxy._set_secret("splunk.hec_token", "test-hec-token")

    with respx.mock(base_url="https://splunk.test:8088") as mock:
        route = mock.post("/services/collector/event").mock(
            return_value=httpx.Response(200, json={"text": "Success", "code": 0})
        )
        ok, detail = asyncio.run(llmproxy._push_to_splunk_hec({"event_type": "guardrail_redirected"}))

    assert ok is True
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Splunk test-hec-token"
    import json
    body = json.loads(sent.content)
    assert body["event"]["event_type"] == "guardrail_redirected"
    assert body["index"] == "llmproxy"


def test_push_to_hec_reports_failure_on_http_error(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._splunk_cfg = {"enabled": True, "url": "https://splunk.test:8088",
                             "index": "", "verify_tls": True}
    llmproxy._set_secret("splunk.hec_token", "bad-token")

    with respx.mock(base_url="https://splunk.test:8088") as mock:
        mock.post("/services/collector/event").mock(return_value=httpx.Response(403, json={"code": 4}))
        ok, detail = asyncio.run(llmproxy._push_to_splunk_hec({"event_type": "x"}))

    assert ok is False


@pytest.fixture
def admin_client(llmproxy_module):
    return TestClient(llmproxy_module.app), {"X-Admin-Token": llmproxy_module._ADMIN_TOKEN}


def test_get_splunk_config_masks_token(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._set_secret("splunk.hec_token", "real-token-value")
    r = client.get("/admin/splunk/config", headers=headers)
    assert r.status_code == 200
    assert r.json()["token"] == llmproxy_module._FRONTIER_KEY_MASK


def test_set_splunk_config_persists_url_and_token(llmproxy_module, admin_client):
    client, headers = admin_client
    r = client.post("/admin/splunk/config", headers=headers, json={
        "enabled": True, "url": "https://splunk.test:8088", "index": "llmproxy",
        "verify_tls": True, "token": "new-hec-token",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["url"] == "https://splunk.test:8088"
    assert body["token"] == llmproxy_module._FRONTIER_KEY_MASK
    assert llmproxy_module._get_splunk_hec_token() == "new-hec-token"


def test_set_splunk_config_with_masked_token_leaves_secret_unchanged(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._set_secret("splunk.hec_token", "existing-token")
    r = client.post("/admin/splunk/config", headers=headers, json={
        "enabled": True, "url": "https://splunk.test:8088", "index": "",
        "token": llmproxy_module._FRONTIER_KEY_MASK,
    })
    assert r.status_code == 200
    assert llmproxy_module._get_splunk_hec_token() == "existing-token"


def test_splunk_config_requires_admin(llmproxy_module):
    client = TestClient(llmproxy_module.app)
    r = client.get("/admin/splunk/config")
    assert r.status_code == 403


def test_splunk_test_endpoint_reports_success(llmproxy_module, admin_client):
    client, headers = admin_client
    llmproxy_module._splunk_cfg = {"enabled": True, "url": "https://splunk.test:8088",
                                    "index": "", "verify_tls": True}
    llmproxy_module._set_secret("splunk.hec_token", "test-token")
    with respx.mock(base_url="https://splunk.test:8088") as mock:
        mock.post("/services/collector/event").mock(
            return_value=httpx.Response(200, json={"text": "Success", "code": 0})
        )
        r = client.post("/admin/splunk/test", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
