"""Exercises the real smart-feature helpers in proxy/llmproxy.py: the
complexity scorer, duration predictor, auto-router, TPS anomaly
detector, and GPU-overload check. All pure functions -- only touch
in-memory module globals, monkeypatched per test.
"""
import pytest


# ── _text_content_len / _compute_complexity ─────────────────────────────────

def test_text_content_len_string(llmproxy_module):
    assert llmproxy_module._text_content_len("hello world") == 11


def test_text_content_len_list_excludes_images(llmproxy_module):
    content = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 50000}},
    ]
    assert llmproxy_module._text_content_len(content) == 2


def test_text_content_len_unknown_type_returns_zero(llmproxy_module):
    assert llmproxy_module._text_content_len(12345) == 0


def test_compute_complexity_empty_body(llmproxy_module):
    assert llmproxy_module._compute_complexity({}) == 0.0


def test_compute_complexity_scales_with_messages_and_tokens(llmproxy_module):
    body = {"messages": [{"role": "user", "content": "x" * 400}] * 3}
    score = llmproxy_module._compute_complexity(body)
    # 3 msgs * 0.02 + (3*400//4)/50000 = 0.06 + 0.006 = 0.066
    assert score == pytest.approx(0.066, abs=1e-4)


def test_compute_complexity_tools_add_flat_bonus(llmproxy_module):
    body = {"messages": [], "tools": [{"name": "x"}]}
    assert llmproxy_module._compute_complexity(body) == 0.3


def test_compute_complexity_clamped_to_one(llmproxy_module):
    body = {"messages": [{"role": "user", "content": "x" * 4_000_000}]}
    assert llmproxy_module._compute_complexity(body) == 1.0


# ── _predict_duration ────────────────────────────────────────────────────

@pytest.fixture
def baselines(llmproxy_module):
    llmproxy = llmproxy_module
    original = dict(llmproxy._model_baselines)
    llmproxy._model_baselines.clear()
    yield llmproxy._model_baselines
    llmproxy._model_baselines.clear()
    llmproxy._model_baselines.update(original)


def test_predict_duration_no_baseline_returns_none(llmproxy_module, baselines):
    assert llmproxy_module._predict_duration("unknown-model", 0.5) is None


def test_predict_duration_zero_baseline_returns_none(llmproxy_module, baselines):
    baselines["qwen3:8b"] = 0
    assert llmproxy_module._predict_duration("qwen3:8b", 0.5) is None


def test_predict_duration_computes_from_baseline(llmproxy_module, baselines):
    baselines["qwen3:8b"] = 100.0  # tokens/s
    # complexity 0.5 -> est_tokens 25000, / 100 tps = 250.0s
    assert llmproxy_module._predict_duration("qwen3:8b", 0.5) == 250.0


# ── _apply_router ────────────────────────────────────────────────────────

@pytest.fixture
def routing_cfg(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._routing_cfg
    yield llmproxy
    llmproxy._routing_cfg = original


def test_apply_router_disabled_passes_through(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": False, "routes": [{"if_complexity_below": 1, "route_to": "small"}]}
    target, original, gpu = llmproxy_module._apply_router("big-model", 0.1)
    assert target == "big-model"
    assert original is None


def test_apply_router_no_matching_route_passes_through(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [{"if_complexity_below": 0.1, "route_to": "small"}]}
    target, original, gpu = llmproxy_module._apply_router("big-model", 0.9)
    assert target == "big-model"
    assert original is None


def test_apply_router_routes_below_threshold(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [
        {"if_complexity_below": 0.5, "model_pattern": "*", "route_to": "small-model"}
    ]}
    target, original, gpu = llmproxy_module._apply_router("big-model", 0.1)
    assert target == "small-model"
    assert original == "big-model"


def test_apply_router_threshold_zero_means_always_match(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [
        {"if_complexity_below": 0, "model_pattern": "*", "route_to": "small-model"}
    ]}
    target, original, gpu = llmproxy_module._apply_router("big-model", 0.99)
    assert target == "small-model"


def test_apply_router_model_pattern_prefix_match(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [
        {"if_complexity_below": 1.0, "model_pattern": "qwen3:", "route_to": "small-model"}
    ]}
    target, _, _ = llmproxy_module._apply_router("qwen3:8b", 0.1)
    assert target == "small-model"
    target2, _, _ = llmproxy_module._apply_router("llama3:8b", 0.1)
    assert target2 == "llama3:8b"


def test_apply_router_exact_model_match(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [
        {"if_complexity_below": 1.0, "model_pattern": "specific-model", "route_to": "small-model"}
    ]}
    target, _, _ = llmproxy_module._apply_router("specific-model", 0.1)
    assert target == "small-model"
    target2, _, _ = llmproxy_module._apply_router("other-model", 0.1)
    assert target2 == "other-model"


def test_apply_router_returns_target_gpu(llmproxy_module, routing_cfg):
    llmproxy_module._routing_cfg = {"enabled": True, "routes": [
        {"if_complexity_below": 1.0, "model_pattern": "*", "route_to": "model", "target_gpu": 1}
    ]}
    target, original, gpu = llmproxy_module._apply_router("model", 0.1)
    assert gpu == 1


# ── _check_tps_anomaly ───────────────────────────────────────────────────

def test_tps_anomaly_short_completion_ignored(llmproxy_module, baselines):
    baselines["m"] = 100.0
    assert llmproxy_module._check_tps_anomaly("m", 1.0, completion_tokens=5) is False


def test_tps_anomaly_none_tps_ignored(llmproxy_module, baselines):
    baselines["m"] = 100.0
    assert llmproxy_module._check_tps_anomaly("m", None, completion_tokens=100) is False


def test_tps_anomaly_no_baseline_ignored(llmproxy_module, baselines):
    assert llmproxy_module._check_tps_anomaly("unknown-model", 1.0, completion_tokens=100) is False


def test_tps_anomaly_detected_below_half_baseline(llmproxy_module, baselines):
    baselines["m"] = 100.0
    assert llmproxy_module._check_tps_anomaly("m", 40.0, completion_tokens=100) is True


def test_tps_anomaly_not_detected_above_half_baseline(llmproxy_module, baselines):
    baselines["m"] = 100.0
    assert llmproxy_module._check_tps_anomaly("m", 60.0, completion_tokens=100) is False


# ── _gpu_overloaded ───────────────────────────────────────────────────────

@pytest.fixture
def hw_stats(llmproxy_module):
    llmproxy = llmproxy_module
    original = llmproxy._hw_stats
    yield llmproxy
    llmproxy._hw_stats = original


def test_gpu_overloaded_no_gpus(llmproxy_module, hw_stats):
    llmproxy_module._hw_stats = {"gpus": []}
    assert llmproxy_module._gpu_overloaded() is False


def test_gpu_overloaded_under_threshold(llmproxy_module, hw_stats):
    llmproxy_module._hw_stats = {"gpus": [{"load_pct": 80}]}
    assert llmproxy_module._gpu_overloaded() is False


def test_gpu_overloaded_over_threshold(llmproxy_module, hw_stats):
    llmproxy_module._hw_stats = {"gpus": [{"load_pct": 96}]}
    assert llmproxy_module._gpu_overloaded() is True


def test_gpu_overloaded_any_gpu_triggers(llmproxy_module, hw_stats):
    llmproxy_module._hw_stats = {"gpus": [{"load_pct": 10}, {"load_pct": 99}]}
    assert llmproxy_module._gpu_overloaded() is True
