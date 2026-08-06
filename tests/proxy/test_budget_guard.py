"""Exercises the real budget-guard read/enforce path in proxy/llmproxy.py:
_get_client_config, _get_budget (pure config lookups) and _check_budget/
_guard_budget_sync (DB-backed). Complements test_spend_guardrail.py, which
covers the write side (_add_budget_usage/_get_spend_usd_today).
"""
import pytest
from fastapi import HTTPException


@pytest.fixture
def client_cfg(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._client_cfg
    yield llmproxy
    llmproxy._client_cfg = original


@pytest.fixture(autouse=True)
def _clean_budgets(llmproxy_module):
    yield
    llmproxy_module._db().execute("DELETE FROM budgets")
    llmproxy_module._db().commit()


# ── _get_client_config / _get_budget ─────────────────────────────────────

def test_get_client_config_returns_named_client(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {"limit_local": 100, "blocked": True}}}
    cfg = llmproxy_module._get_client_config("alice")
    assert cfg == {"limit_local": 100, "blocked": True}


def test_get_client_config_falls_back_to_default(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"default": {"limit_local": 5}}}
    cfg = llmproxy_module._get_client_config("someone-not-configured")
    assert cfg == {"limit_local": 5}


def test_get_client_config_hardcoded_fallback_when_no_default_either(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {}}
    cfg = llmproxy_module._get_client_config("nobody")
    assert cfg == {"limit": 5_000_000, "models": "*", "blocked": False}


def test_get_budget_local_unlimited_by_default(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {}}}
    assert llmproxy_module._get_budget("alice", is_frontier=False) == -1


def test_get_budget_frontier_default_limit(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {}}}
    assert llmproxy_module._get_budget("alice", is_frontier=True) == 1_000_000


def test_get_budget_respects_configured_limits(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {"limit_local": 42, "limit_frontier": 7}}}
    assert llmproxy_module._get_budget("alice", is_frontier=False) == 42
    assert llmproxy_module._get_budget("alice", is_frontier=True) == 7


# ── _check_budget ─────────────────────────────────────────────────────────

def test_check_budget_allowed_when_unlimited(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {"limit_local": -1}}}
    allowed, used, limit = llmproxy_module._check_budget("alice", is_frontier=False)
    assert allowed is True
    assert limit == -1


def test_check_budget_allowed_under_limit(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {"limit_local": 1000}}}
    llmproxy_module._add_budget_usage("alice", 100, is_frontier=False)
    allowed, used, limit = llmproxy_module._check_budget("alice", is_frontier=False)
    assert allowed is True
    assert used == 100
    assert limit == 1000


def test_check_budget_blocked_over_limit(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"alice": {"limit_local": 100}}}
    llmproxy_module._add_budget_usage("alice", 150, is_frontier=False)
    allowed, used, limit = llmproxy_module._check_budget("alice", is_frontier=False)
    assert allowed is False
    assert used == 150


def test_check_budget_no_usage_yet_defaults_to_zero(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"brand-new-client": {"limit_local": 100}}}
    allowed, used, limit = llmproxy_module._check_budget("brand-new-client", is_frontier=False)
    assert used == 0
    assert allowed is True


# ── _guard_budget_sync ────────────────────────────────────────────────────

def test_guard_budget_sync_raises_403_when_blocked(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"blocked-client": {"blocked": True}}}
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._guard_budget_sync("blocked-client")
    assert exc.value.status_code == 403


def test_guard_budget_sync_raises_429_over_budget_with_retry_after(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"over-budget": {"limit_local": 10}}}
    llmproxy_module._add_budget_usage("over-budget", 20, is_frontier=False)
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._guard_budget_sync("over-budget", is_frontier=False)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
    assert exc.value.detail["used"] == 20
    assert exc.value.detail["limit"] == 10


def test_guard_budget_sync_passes_when_within_budget(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"fine-client": {"limit_local": 1000}}}
    llmproxy_module._add_budget_usage("fine-client", 10, is_frontier=False)
    llmproxy_module._guard_budget_sync("fine-client", is_frontier=False)  # must not raise


def test_guard_budget_sync_unlimited_never_blocks_even_with_high_usage(llmproxy_module, client_cfg):
    llmproxy_module._client_cfg = {"clients": {"unlimited-client": {"limit_local": -1}}}
    llmproxy_module._add_budget_usage("unlimited-client", 10_000_000, is_frontier=False)
    llmproxy_module._guard_budget_sync("unlimited-client", is_frontier=False)  # must not raise
