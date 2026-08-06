"""Exercises the real _record_violation() per-rule strike_threshold/
strike_window_s/ban_duration_s overrides -- lets a severe rule ban a
client faster than the default 10-strikes/300s/3600s behavior.
"""
import pytest


@pytest.fixture(autouse=True)
def _clean_fail2ban_state(llmproxy_module):
    llmproxy = llmproxy_module
    original = dict(llmproxy._fail2ban_cfg)
    if hasattr(llmproxy._record_violation, "strikes"):
        llmproxy._record_violation.strikes.clear()
    yield
    llmproxy._fail2ban_cfg = original
    if hasattr(llmproxy._record_violation, "strikes"):
        llmproxy._record_violation.strikes.clear()


def test_default_thresholds_match_previous_hardcoded_behavior(llmproxy_module):
    llmproxy = llmproxy_module
    rule = {"trigger": "keyword", "pattern": "x", "action": "deny"}  # no overrides
    for _ in range(9):
        llmproxy._record_violation("clientA", "1.2.3.4", rule, "bad prompt")
    assert "clientA" not in llmproxy._fail2ban_cfg.get("bans", {})
    llmproxy._record_violation("clientA", "1.2.3.4", rule, "bad prompt")  # 10th
    assert "clientA" in llmproxy._fail2ban_cfg.get("bans", {})


def test_severe_rule_bans_after_one_strike(llmproxy_module):
    llmproxy = llmproxy_module
    rule = {"trigger": "keyword", "pattern": "secret-leak", "action": "deny",
            "strike_threshold": 1, "ban_duration_s": 7200}
    llmproxy._record_violation("clientB", "1.2.3.4", rule, "leaked secret")
    assert "clientB" in llmproxy._fail2ban_cfg["bans"]
    import time
    ban_until = llmproxy._fail2ban_cfg["bans"]["clientB"]
    assert ban_until > time.time() + 7000  # ~7200s, not the default 3600s


def test_mild_rule_still_needs_custom_higher_threshold(llmproxy_module):
    llmproxy = llmproxy_module
    rule = {"trigger": "keyword", "pattern": "minor", "action": "warn", "strike_threshold": 20}
    for _ in range(15):
        llmproxy._record_violation("clientC", "1.2.3.4", rule, "minor issue")
    assert "clientC" not in llmproxy._fail2ban_cfg.get("bans", {})


def test_strike_window_expires_old_strikes(llmproxy_module):
    llmproxy = llmproxy_module
    rule = {"trigger": "keyword", "pattern": "x", "action": "deny",
            "strike_threshold": 3, "strike_window_s": 0.01}
    import time
    llmproxy._record_violation("clientD", "1.2.3.4", rule, "x")
    llmproxy._record_violation("clientD", "1.2.3.4", rule, "x")
    time.sleep(0.05)  # strike_window_s elapses -- earlier strikes drop off
    llmproxy._record_violation("clientD", "1.2.3.4", rule, "x")
    assert "clientD" not in llmproxy._fail2ban_cfg.get("bans", {})
