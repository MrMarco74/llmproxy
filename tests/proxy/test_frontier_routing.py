"""Exercises the real frontier/fallback routing resolvers in
proxy/llmproxy.py: _get_frontier_target and _get_fallback_frontier. Both
read config globals and resolve the provider's API key through the
encrypted secrets store (_get_frontier_api_key -> _get_secret), so these
aren't pure -- they need the `frontier_model` fixture's real _set_secret
entry (see tests/proxy/conftest.py) to resolve a usable key end to end.
"""
import pytest


def test_get_frontier_target_resolves_configured_model(llmproxy_module, frontier_model):
    result = llmproxy_module._get_frontier_target(frontier_model)
    assert result is not None
    base_url, api_key = result
    assert base_url == "https://frontier.test"
    assert api_key == "test-frontier-api-key"


def test_get_frontier_target_unknown_model_returns_none(llmproxy_module, frontier_model):
    assert llmproxy_module._get_frontier_target("not-a-configured-model") is None


def test_get_frontier_target_disabled_provider_returns_none(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._frontier_cfg
    llmproxy._frontier_cfg = {"enabled": False, "providers": {}}
    try:
        assert llmproxy._get_frontier_target("anything") is None
    finally:
        llmproxy._frontier_cfg = original


@pytest.fixture
def fallback_cfg(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._fallback_cfg
    yield llmproxy
    llmproxy._fallback_cfg = original


def test_get_fallback_frontier_disabled_returns_none(llmproxy_module, fallback_cfg, frontier_model):
    llmproxy_module._fallback_cfg = {"enabled": False, "mapping": {"qwen3:8b": frontier_model}}
    assert llmproxy_module._get_fallback_frontier("qwen3:8b") is None


def test_get_fallback_frontier_no_mapping_returns_none(llmproxy_module, fallback_cfg, frontier_model):
    llmproxy_module._fallback_cfg = {"enabled": True, "mapping": {}}
    assert llmproxy_module._get_fallback_frontier("qwen3:8b") is None


def test_get_fallback_frontier_exact_match(llmproxy_module, fallback_cfg, frontier_model):
    llmproxy_module._fallback_cfg = {"enabled": True, "mapping": {"qwen3:8b": frontier_model}}
    result = llmproxy_module._get_fallback_frontier("qwen3:8b")
    assert result is not None
    fb_model, base_url, api_key = result
    assert fb_model == frontier_model
    assert base_url == "https://frontier.test"


def test_get_fallback_frontier_catchall_used_when_no_exact_match(llmproxy_module, fallback_cfg, frontier_model):
    llmproxy_module._fallback_cfg = {"enabled": True, "mapping": {"*": frontier_model}}
    result = llmproxy_module._get_fallback_frontier("some-other-model")
    assert result is not None
    assert result[0] == frontier_model


def test_get_fallback_frontier_exact_match_takes_precedence_over_catchall(llmproxy_module, fallback_cfg, frontier_model):
    llmproxy_module._fallback_cfg = {"enabled": True, "mapping": {
        "qwen3:8b": "should-not-be-used", "*": frontier_model,
    }}
    # "should-not-be-used" isn't a configured frontier model, so if the
    # catchall wrongly won, _get_frontier_target would fail to resolve
    # it and this would return None instead of a result.
    result = llmproxy_module._get_fallback_frontier("qwen3:8b")
    assert result is None


def test_get_fallback_frontier_mapped_model_without_frontier_provider_returns_none(llmproxy_module, fallback_cfg):
    llmproxy = llmproxy_module
    original = llmproxy._frontier_cfg
    llmproxy._frontier_cfg = {"enabled": True, "providers": {}}
    llmproxy._fallback_cfg = {"enabled": True, "mapping": {"qwen3:8b": "unconfigured-frontier-model"}}
    try:
        assert llmproxy._get_fallback_frontier("qwen3:8b") is None
    finally:
        llmproxy._frontier_cfg = original
