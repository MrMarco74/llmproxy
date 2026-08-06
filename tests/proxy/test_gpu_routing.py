"""Exercises the real _model_present() fix in proxy/llmproxy.py: a
permanently-unreachable upstream (e.g. a second GPU/Ollama instance that
no longer exists) must NOT be treated as having every model just because
its catalog is empty -- that used to route requests for models missing
from upstream 0 straight into a dead upstream 1 instead of a clean
"model not found" from the upstream that's actually alive.
"""
import pytest


@pytest.fixture
def catalog_and_health(llmproxy_module):
    llmproxy = llmproxy_module
    original_catalog = dict(llmproxy._model_catalog)
    original_health = dict(llmproxy._ollama_healthy)
    yield llmproxy
    llmproxy._model_catalog = original_catalog
    llmproxy._ollama_healthy = original_health


def test_model_present_permissive_before_first_poll(llmproxy_module, catalog_and_health):
    llmproxy = llmproxy_module
    llmproxy._model_catalog = {}
    llmproxy._ollama_healthy = {0: True, 1: True}  # cold-start default
    assert llmproxy._model_present(1, "any-model") is True


def test_model_present_false_for_permanently_unreachable_upstream(llmproxy_module, catalog_and_health):
    llmproxy = llmproxy_module
    llmproxy._model_catalog = {0: {"qwen3:8b"}, 1: {}}
    llmproxy._ollama_healthy = {0: True, 1: False}  # upstream 1 has failed at least one poll
    assert llmproxy._model_present(1, "qwen3:8b") is False


def test_model_present_true_when_model_actually_in_catalog(llmproxy_module, catalog_and_health):
    llmproxy = llmproxy_module
    llmproxy._model_catalog = {0: {"qwen3:8b"}}
    llmproxy._ollama_healthy = {0: True, 1: True}
    assert llmproxy._model_present(0, "qwen3:8b") is True
    assert llmproxy._model_present(0, "not-installed") is False


def test_select_upstream_does_not_route_to_dead_gpu1_for_missing_model(llmproxy_module, catalog_and_health):
    """Regression test for the actual bug: model exists on GPU 0's real
    catalog under a different name, isn't found there, and GPU 1 is
    permanently dead -- must not blindly route to GPU 1."""
    llmproxy = llmproxy_module
    llmproxy._model_catalog = {0: {"qwen3:8b"}, 1: {}}
    llmproxy._ollama_healthy = {0: True, 1: False}
    upstream = llmproxy._select_upstream("some-model-not-pulled-anywhere", target_gpu=None)
    # Falls through past the present_on/gpu-count branches to the safe
    # default -- never OLLAMA_UPSTREAM_1, which is what used to happen.
    assert upstream == llmproxy.OLLAMA_UPSTREAM_0
