"""Exercises the real `_apply_rules` guardrail engine (proxy/llmproxy.py)
against every action type, including the redirect_internal/redirect_external/
reduce_effort_external actions and their order-sensitivity with each other.
"""
import asyncio

import pytest


def _run(llmproxy, *args, **kwargs):
    return asyncio.run(llmproxy._apply_rules(*args, **kwargs))


def test_deny_blocks_and_does_not_modify_body(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "forbidden", "action": "deny"}]
    body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "this is forbidden"}]}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "this is forbidden", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is False
    assert action == "deny"
    assert violation is not None
    assert rule is not None


def test_silent_blocks_like_deny_but_is_a_distinct_action(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "quiet", "action": "silent"}]
    body = {"model": "qwen3:8b", "prompt": "quiet please"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "quiet please", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert action == "silent"
    assert modified is False


def test_redirect_sets_target_model(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "route-me", "action": "redirect", "target_model": "qwen3:8b"}]
    body = {"model": "big-model:70b", "prompt": "please route-me somewhere"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "please route-me somewhere", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is True
    assert new_body["model"] == "qwen3:8b"
    assert action == "pass"


def test_redirect_without_target_model_falls_back_to_default(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "fallback", "action": "redirect"}]
    body = {"model": "big-model:70b", "prompt": "fallback case"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "fallback case", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert new_body["model"] == llmproxy.DEFAULT_REDIRECT_MODEL


def test_redirect_internal_sets_target_model(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "internal-please", "action": "redirect_internal",
              "target_model": "qwen3:8b"}]
    body = {"model": "big-model:70b", "prompt": "internal-please"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "internal-please", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is True
    assert new_body["model"] == "qwen3:8b"


def test_redirect_external_sets_target_model(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "external-please", "action": "redirect_external",
              "target_model": "gpt-test-external"}]
    body = {"model": "qwen3:8b", "prompt": "external-please"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "external-please", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is True
    assert new_body["model"] == "gpt-test-external"


def test_reduce_effort_external_applies_when_current_model_is_frontier(llmproxy_module, frontier_model):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "think-hard", "action": "reduce_effort_external"}]
    body = {"model": frontier_model, "prompt": "think-hard about this"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "think-hard about this", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is True
    assert new_body["reasoning_effort"] == "low"


def test_reduce_effort_external_noop_when_current_model_is_local(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "think-hard", "action": "reduce_effort_external"}]
    body = {"model": "qwen3:8b", "prompt": "think-hard about this"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "think-hard about this", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert modified is False
    assert "reasoning_effort" not in new_body


def test_reduce_effort_external_honors_custom_field_and_value(llmproxy_module, frontier_model):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "think-hard", "action": "reduce_effort_external",
              "effort_field": "verbosity", "effort_value": "terse"}]
    body = {"model": frontier_model, "prompt": "think-hard about this"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "think-hard about this", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert new_body["verbosity"] == "terse"
    assert "reasoning_effort" not in new_body


def test_order_sensitivity_redirect_external_then_reduce_effort_fires(llmproxy_module, frontier_model):
    """A prior rule in the same list redirecting to a frontier model must
    make a later reduce_effort_external rule see the NEW model, not the
    original one the request came in with."""
    llmproxy = llmproxy_module
    rules = [
        {"trigger": "keyword", "pattern": "hello", "action": "redirect_external", "target_model": frontier_model},
        {"trigger": "keyword", "pattern": "hello", "action": "reduce_effort_external"},
    ]
    body = {"model": "qwen3:8b", "prompt": "hello there"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "hello there", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert new_body["model"] == frontier_model
    assert new_body["reasoning_effort"] == "low"


def test_order_sensitivity_redirect_internal_then_reduce_effort_noops(llmproxy_module, frontier_model):
    """Mirror of the above: redirecting to a LOCAL model first must make a
    later reduce_effort_external rule see it as local and no-op."""
    llmproxy = llmproxy_module
    rules = [
        {"trigger": "keyword", "pattern": "hello", "action": "redirect_internal", "target_model": "qwen3:8b"},
        {"trigger": "keyword", "pattern": "hello", "action": "reduce_effort_external"},
    ]
    body = {"model": frontier_model, "prompt": "hello there"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "hello there", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert new_body["model"] == "qwen3:8b"
    assert "reasoning_effort" not in new_body


def test_shadow_mode_does_not_apply_action(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "shadow-me", "action": "deny", "mode": "shadow"}]
    body = {"model": "qwen3:8b", "prompt": "shadow-me test"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "shadow-me test", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert action == "pass"
    assert modified is False
    assert new_body["model"] == "qwen3:8b"


def test_no_rules_match_passes_through_unchanged(llmproxy_module):
    llmproxy = llmproxy_module
    rules = [{"trigger": "keyword", "pattern": "nope", "action": "deny"}]
    body = {"model": "qwen3:8b", "prompt": "totally unrelated text"}
    modified, violation, action, new_body, rule = _run(
        llmproxy, "totally unrelated text", "clientA", "1.2.3.4", body, rules, record=False
    )
    assert action == "pass"
    assert modified is False
    assert new_body == body


@pytest.fixture
def frontier_model(llmproxy_module):
    """Registers a fake enabled frontier provider/model on the shared
    module so _get_frontier_target(...) resolves it, then restores the
    original config -- these globals are shared across the whole test
    session via the session-scoped llmproxy_module fixture."""
    llmproxy = llmproxy_module
    original = llmproxy._frontier_cfg
    llmproxy._frontier_cfg = {
        "enabled": True,
        "providers": {"testprovider": {"base_url": "https://frontier.test", "models": ["gpt-test-external"]}},
    }
    try:
        yield "gpt-test-external"
    finally:
        llmproxy._frontier_cfg = original
