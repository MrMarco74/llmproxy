"""Exercises the real chargeback-linked guardrail: _add_budget_usage's
spend_usd_frontier tracking, _get_spend_usd_today, and the
spend_threshold trigger in _apply_rules.
"""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def _pricing_and_cleanup(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._pricing_cfg
    llmproxy._pricing_cfg = {
        "fx": {"usd_to_eur": 0.9, "updated": "", "source": "test"},
        "models": {"gpt-frontier": {"currency": "USD", "input_per_1k": 1.0, "output_per_1k": 1.0}},
        "default": {"currency": "USD", "input_per_1k": 0.0, "output_per_1k": 0.0},
    }
    yield
    llmproxy._pricing_cfg = original
    llmproxy._db().execute("DELETE FROM budgets")
    llmproxy._db().commit()


def test_add_budget_usage_tracks_frontier_spend(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._add_budget_usage("spendy-client", 2000, is_frontier=True, model="gpt-frontier",
                                prompt_tokens=1000, completion_tokens=1000)
    # 1000/1000 tokens * $1/1k in + $1/1k out = $1 + $1 = $2
    assert llmproxy._get_spend_usd_today("spendy-client") == pytest.approx(2.0)


def test_add_budget_usage_accumulates_across_calls(llmproxy_module):
    llmproxy = llmproxy_module
    for _ in range(3):
        llmproxy._add_budget_usage("multi-client", 2000, is_frontier=True, model="gpt-frontier",
                                    prompt_tokens=1000, completion_tokens=1000)
    assert llmproxy._get_spend_usd_today("multi-client") == pytest.approx(6.0)


def test_local_usage_never_tracked_as_spend(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._add_budget_usage("local-client", 5000, is_frontier=False, model="qwen3:8b",
                                prompt_tokens=2500, completion_tokens=2500)
    assert llmproxy._get_spend_usd_today("local-client") == 0.0


def test_spend_threshold_trigger_fires_once_over_budget(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._add_budget_usage("over-budget-client", 2000, is_frontier=True, model="gpt-frontier",
                                prompt_tokens=1000, completion_tokens=1000)  # $2 spent today
    rules = [{"trigger": "spend_threshold", "max_usd_daily": 1.0, "action": "deny"}]
    body = {"model": "gpt-frontier", "prompt": "anything at all"}
    modified, violation, action, new_body, rule = asyncio.run(
        llmproxy._apply_rules("anything at all", "over-budget-client", "1.2.3.4", body, rules, record=False)
    )
    assert action == "deny"


def test_spend_threshold_trigger_noop_under_budget(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._add_budget_usage("under-budget-client", 100, is_frontier=True, model="gpt-frontier",
                                prompt_tokens=50, completion_tokens=50)  # $0.10 spent today
    rules = [{"trigger": "spend_threshold", "max_usd_daily": 5.0, "action": "deny"}]
    body = {"model": "gpt-frontier", "prompt": "anything at all"}
    modified, violation, action, new_body, rule = asyncio.run(
        llmproxy._apply_rules("anything at all", "under-budget-client", "1.2.3.4", body, rules, record=False)
    )
    assert action == "pass"


def test_spend_threshold_can_redirect_internal_instead_of_deny(llmproxy_module):
    llmproxy = llmproxy_module
    llmproxy._add_budget_usage("redirect-client", 2000, is_frontier=True, model="gpt-frontier",
                                prompt_tokens=1000, completion_tokens=1000)
    rules = [{"trigger": "spend_threshold", "max_usd_daily": 1.0, "action": "redirect_internal",
              "target_model": "qwen3:8b"}]
    body = {"model": "gpt-frontier", "prompt": "anything"}
    modified, violation, action, new_body, rule = asyncio.run(
        llmproxy._apply_rules("anything", "redirect-client", "1.2.3.4", body, rules, record=False)
    )
    assert modified is True
    assert new_body["model"] == "qwen3:8b"
