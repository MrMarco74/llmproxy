"""Exercises the real dual-currency chargeback math in proxy/llmproxy.py:
_model_cost_native, _to_usd_eur, _model_cost_usd_eur.
"""
import pytest


@pytest.fixture(autouse=True)
def _pricing_fixture(llmproxy_module):
    """Swaps in a known pricing config for the duration of each test and
    restores the original afterwards -- these are shared module globals."""
    llmproxy = llmproxy_module
    original = llmproxy._pricing_cfg
    llmproxy._pricing_cfg = {
        "fx": {"usd_to_eur": 0.9, "updated": "2026-01-01", "source": "test"},
        "models": {
            "gpt-usd-model": {"currency": "USD", "input_per_1k": 0.01, "output_per_1k": 0.03},
            "claude-eur-model": {"currency": "EUR", "input_per_1k": 0.02, "output_per_1k": 0.06},
            "free-local-model": {"currency": "USD", "input_per_1k": 0.0, "output_per_1k": 0.0},
        },
        "default": {"currency": "USD", "input_per_1k": 0.0, "output_per_1k": 0.0},
    }
    try:
        yield
    finally:
        llmproxy._pricing_cfg = original


def test_model_cost_native_usd_model(llmproxy_module):
    amount, currency = llmproxy_module._model_cost_native("gpt-usd-model", 1000, 500)
    assert currency == "USD"
    assert amount == pytest.approx(0.01 + 0.015)


def test_model_cost_native_eur_model(llmproxy_module):
    amount, currency = llmproxy_module._model_cost_native("claude-eur-model", 2000, 1000)
    assert currency == "EUR"
    assert amount == pytest.approx(0.04 + 0.06)


def test_unpriced_model_falls_back_to_default_zero_cost(llmproxy_module):
    amount, currency = llmproxy_module._model_cost_native("unknown-model-xyz", 5000, 5000)
    assert amount == 0.0
    assert currency == "USD"


def test_local_model_with_zero_pricing_is_free(llmproxy_module):
    amount, currency = llmproxy_module._model_cost_native("free-local-model", 100000, 100000)
    assert amount == 0.0


def test_to_usd_eur_from_usd_amount(llmproxy_module):
    usd, eur = llmproxy_module._to_usd_eur(10.0, "USD")
    assert usd == 10.0
    assert eur == pytest.approx(9.0)  # 10 * 0.9 fx rate


def test_to_usd_eur_from_eur_amount_converts_back_to_usd(llmproxy_module):
    usd, eur = llmproxy_module._to_usd_eur(9.0, "EUR")
    assert eur == 9.0
    assert usd == pytest.approx(10.0)  # 9 / 0.9 fx rate


def test_model_cost_usd_eur_end_to_end_for_usd_model(llmproxy_module):
    usd, eur = llmproxy_module._model_cost_usd_eur("gpt-usd-model", 1000, 500)
    assert usd == pytest.approx(0.025)
    assert eur == pytest.approx(0.025 * 0.9)


def test_model_cost_usd_eur_end_to_end_for_eur_model(llmproxy_module):
    usd, eur = llmproxy_module._model_cost_usd_eur("claude-eur-model", 2000, 1000)
    assert eur == pytest.approx(0.10)
    assert usd == pytest.approx(0.10 / 0.9)


def test_zero_tokens_yields_zero_cost(llmproxy_module):
    amount, currency = llmproxy_module._model_cost_native("gpt-usd-model", 0, 0)
    assert amount == 0.0
