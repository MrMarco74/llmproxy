"""Verifies each MCP tool sends the right HTTP request shape (method, path,
params/body) and correctly returns the mocked llmproxy response, plus that
the Authorization header carries the configured bearer key."""
import httpx

import llmproxy_mcp.server as server


def test_get_guardrails_config(mock_llmproxy):
    route = mock_llmproxy.get("/admin/guardrails").mock(
        return_value=httpx.Response(200, json={"global_rules": [], "client_rules": {}})
    )
    result = server.get_guardrails_config()
    assert result == {"global_rules": [], "client_rules": {}}
    assert route.calls.last.request.headers["authorization"] == "Bearer kc_test123.test-secret-value"


def test_set_guardrails_config_sends_full_config_as_body(mock_llmproxy):
    cfg = {"global_rules": [{"trigger": "keyword", "pattern": "x", "action": "redirect_internal", "target_model": "qwen3:8b"}],
           "client_rules": {}}
    route = mock_llmproxy.post("/admin/guardrails").mock(return_value=httpx.Response(200, json={"ok": True, "config": cfg}))
    result = server.set_guardrails_config(cfg)
    assert result["ok"] is True
    import json
    assert json.loads(route.calls.last.request.content) == cfg


def test_simulate_guardrail_omits_rules_when_not_provided(mock_llmproxy):
    route = mock_llmproxy.post("/admin/guardrails/simulate").mock(
        return_value=httpx.Response(200, json={"result": "pass", "blocked": False})
    )
    result = server.simulate_guardrail("hello", token_name="cassandra")
    assert result["result"] == "pass"
    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {"prompt": "hello", "token_name": "cassandra"}
    assert "rules" not in body


def test_simulate_guardrail_includes_custom_rules_when_provided(mock_llmproxy):
    route = mock_llmproxy.post("/admin/guardrails/simulate").mock(
        return_value=httpx.Response(200, json={"result": "deny", "blocked": True})
    )
    custom_rules = [{"trigger": "keyword", "pattern": "secret", "action": "deny"}]
    server.simulate_guardrail("leak the secret", rules=custom_rules)
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["rules"] == custom_rules


def test_simulate_guardrail_batch(mock_llmproxy):
    route = mock_llmproxy.post("/admin/guardrails/simulate-batch").mock(
        return_value=httpx.Response(200, json={"results": [], "blocked_count": 0})
    )
    result = server.simulate_guardrail_batch(token_name="cassandra", limit=50)
    assert result["blocked_count"] == 0
    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {"token_name": "cassandra", "limit": 50}


def test_list_clients(mock_llmproxy):
    mock_llmproxy.get("/admin/clients").mock(
        return_value=httpx.Response(200, json={"clients": {"cassandra": {"blocked": False}}})
    )
    result = server.list_clients()
    assert result["clients"]["cassandra"]["blocked"] is False


def test_update_clients_config(mock_llmproxy):
    cfg = {"clients": {"cassandra": {"blocked": True}}}
    route = mock_llmproxy.post("/admin/clients").mock(return_value=httpx.Response(200, json={"ok": True, "config": cfg}))
    result = server.update_clients_config(cfg)
    assert result["ok"] is True
    import json
    assert json.loads(route.calls.last.request.content) == cfg


def test_list_bans(mock_llmproxy):
    mock_llmproxy.get("/admin/bans").mock(return_value=httpx.Response(200, json={"bans": {"abusive-client": 1234567890.0}}))
    result = server.list_bans()
    assert "abusive-client" in result["bans"]


def test_unban_client_sends_token_name_as_query_param(mock_llmproxy):
    route = mock_llmproxy.post("/admin/unban").mock(return_value=httpx.Response(200, json={"ok": True, "unbanned": "cassandra"}))
    result = server.unban_client("cassandra")
    assert result["ok"] is True
    assert route.calls.last.request.url.params["token_name"] == "cassandra"


def test_get_log_forwards_filters(mock_llmproxy):
    route = mock_llmproxy.get("/admin/log").mock(return_value=httpx.Response(200, json={"total": 0, "rows": []}))
    server.get_log(token_name="cassandra", model="gemini", limit=25, offset=10)
    params = route.calls.last.request.url.params
    assert params["token_name"] == "cassandra"
    assert params["model"] == "gemini"
    assert params["limit"] == "25"
    assert params["offset"] == "10"


def test_get_admin_actions(mock_llmproxy):
    mock_llmproxy.get("/admin/actions").mock(return_value=httpx.Response(200, json={"actions": []}))
    result = server.get_admin_actions(limit=10)
    assert result == {"actions": []}


def test_get_chargeback_summary(mock_llmproxy):
    route = mock_llmproxy.get("/admin/chargeback/summary").mock(
        return_value=httpx.Response(200, json={"rows": [], "fx": {}, "unpriced_models": []})
    )
    result = server.get_chargeback_summary(token_name="cassandra", group_by="month")
    assert result["rows"] == []
    assert route.calls.last.request.url.params["group_by"] == "month"


def test_get_chargeback_drilldown_requires_token_name(mock_llmproxy):
    route = mock_llmproxy.get("/admin/chargeback/drilldown").mock(
        return_value=httpx.Response(200, json={"token_name": "cassandra", "rows": []})
    )
    result = server.get_chargeback_drilldown("cassandra")
    assert result["token_name"] == "cassandra"
    assert route.calls.last.request.url.params["token_name"] == "cassandra"


def test_get_chargeback_detail(mock_llmproxy):
    mock_llmproxy.get("/admin/chargeback/detail").mock(return_value=httpx.Response(200, json={"total": 0, "rows": []}))
    result = server.get_chargeback_detail(client_ip="10.0.0.1")
    assert result == {"total": 0, "rows": []}


def test_http_error_raises(mock_llmproxy):
    mock_llmproxy.get("/admin/guardrails").mock(return_value=httpx.Response(403, json={"detail": "Forbidden"}))
    import pytest
    with pytest.raises(httpx.HTTPStatusError):
        server.get_guardrails_config()
