#!/usr/bin/env python3
"""
llmproxy — Ollama + ComfyUI logging proxy for a remote GPU host.

Runs on the proxy host (systemd, always-on), listens on :11435 (Ollama) and
:18189 (ComfyUI). Forwards to the GPU host over the network — Ollama and
ComfyUI stay on the GPU host, only the proxy/dashboard/logging moved to the
always-on proxy host so the GPU host only needs to run for actual inference.

Hardware monitoring and gaming-mode detection (Steam runs on the GPU host,
not the proxy host) are sourced remotely from the `gpu-agent` companion
service (see GPU_AGENT_URL, gpu-agent.py) instead of local
psutil/nvidia-smi/pgrep.

Features:
  - Full request logging to SQLite (~/.llmproxy.db, WAL mode)
  - Gaming-mode detection (via gpu-agent) → HTTP 503 block on LLM endpoints
  - Failure content logging (last user message on errors/tool-ignore)
  - Hardware monitoring (CPU/RAM/GPU via gpu-agent, sourced from the GPU host)
  - SSE fan-out broadcaster for N concurrent monitor clients
  - Internal push notifications (gaming, budget, anomalies, evictions, ...)
  - Per-IP inference budget with daily reset
  - Complexity pre-scorer (predict duration before forwarding)
  - Model performance fingerprinting + anomaly detection
  - Model auto-router (complexity-based model substitution)
  - Client profiler (per-IP behavioral aggregation)
  - Load shedding (GPU overload → queue or 503)
  - Idle model eviction (free VRAM when models unused)
  - Round-Robin GPU routing for parallel inference across 2 GPUs
  - Stop-All maintenance mode (blocks all inference, force-purges GPU)
  - GET /status/gpu — live GPU process map (model, client, VRAM, load)
  - POST /maintenance/force-purge — soft evict + SIGKILL llama-server
  - Model-catalog-aware routing: merged /api/tags across both GPUs, and
    /api/chat|generate only route to an upstream that actually has the model
    (fixes vision/rare models silently 404ing on load-balanced round-robin)
"""

__version__ = "2.7.0"

import asyncio
import datetime
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
import websockets
import yaml
import bcrypt
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("llmproxy")

# Splunk JSON Logger
import logging.handlers

splunk_logger = logging.getLogger("llmproxy_splunk")
splunk_logger.setLevel(logging.INFO)
splunk_logger.propagate = False
try:
    splunk_handler = logging.handlers.WatchedFileHandler("/var/lib/llmproxy/audit.log")
    splunk_handler.setFormatter(logging.Formatter("%(message)s"))
    splunk_logger.addHandler(splunk_handler)
except Exception as e:
    logger.error(f"Failed to initialize Splunk logger: {e}")

def _log_to_splunk(event_type: str, data: dict):
    if not _logging_cfg.get("enabled", True):
        return
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": event_type,
        "app": "llmproxy",
    }
    payload.update(data)
    try:
        splunk_logger.info(json.dumps(payload))
    except Exception as e:
        logger.error(f"[splunk] log error: {e}")


# ── Constants ─────────────────────────────────────────────────────────────────

# Proxy läuft auf dem Proxy-Host, Ollama/ComfyUI bleiben auf dem GPU-Host —
# daher Forwarding über das Netz statt localhost. Hardware-/Gaming-Mode-Checks
# laufen ebenfalls remote über den gpu-agent (siehe GPU_AGENT_URL).
# Hostname des GPU-Hosts per Env-Var überschreibbar, z.B. für /etc/hosts-Eintrag
# oder direkte IP: LLMPROXY_GPU_HOST=192.168.1.50
GPU_HOST          = os.environ.get("LLMPROXY_GPU_HOST", "gpu-host")
OLLAMA_UPSTREAM_0 = f"http://{GPU_HOST}:11434"
OLLAMA_UPSTREAM_1 = f"http://{GPU_HOST}:11438"
LISTEN_PORT       = 11435
COMFYUI_UPSTREAM  = f"http://{GPU_HOST}:8188"
COMFYUI_HOST      = GPU_HOST
COMFYUI_PORT      = 18189
GPU_AGENT_URL     = f"http://{GPU_HOST}:11436"

DB_PATH           = Path("/var/lib/llmproxy/llmproxy.db")
CONFIG_DIR        = Path("/opt/llmproxy")

# Default für Guardrail-Regeln mit action=redirect, die kein eigenes
# target_model gesetzt haben (z.B. ältere Regeln aus der Zeit, bevor die UI
# ein target_model-Feld hatte). Muss ein tatsächlich lokal installiertes,
# günstiges Modell sein - "local-fallback" (der alte Default) war kein
# echter Modellname und führte bei Trigger zu einem fehlschlagenden Request.
DEFAULT_REDIRECT_MODEL = "qwen3:8b"

# Same wildcard cert already used by reverse-proxy for *.internal.familie-frischkorn.de,
# issued directly on this host (see reverse-proxy/scripts/issue_internal_cert.sh).
_SSL_CERT         = Path("/etc/letsencrypt/live/internal.familie-frischkorn.de/fullchain.pem")
_SSL_KEY          = Path("/etc/letsencrypt/live/internal.familie-frischkorn.de/privkey.pem")

DEFAULT_OLLAMA_OPTIONS = {"num_gpu": -1, "num_ctx": 65536}

# Per-model num_ctx overrides — DEFAULT_OLLAMA_OPTIONS is shared by every
# model behind this proxy, including small models on the 12GB GPU1-only
# upstream, so it must stay conservative. Larger windows are opted in here
# per model instead of raised globally.
MODEL_NUM_CTX_OVERRIDES = {
    # Dual-GPU tensor split (24GB combined) with OLLAMA_NUM_PARALLEL=1 —
    # see docs/technical.md and doku/posts/13-app-llmproxy.md.
    "qwen3.6-35b-uncensored-nolimit:IQ3_M": 131072,
}

# Log retention (days) — used by /maintenance/cleanup
LOG_RETENTION_DAYS = {"failures": 14, "notifications": 30, "requests": 180}

# ── Config loading ─────────────────────────────────────────────────────────────

def _load_yaml(name: str, default: dict) -> dict:
    p = CONFIG_DIR / name
    if p.exists():
        try:
            return yaml.safe_load(p.read_text()) or default
        except Exception:
            pass
    return default

_client_cfg       = _load_yaml("clients.yaml",       {"clients": {"default": {"limit": 5_000_000, "models": "*", "blocked": False}}})

_token_map = {}

def _build_token_map():
    global _token_map
    _token_map.clear()
    for cid, cinfo in _client_cfg.get("clients", {}).items():
        t = cinfo.get("token")
        if t:
            _token_map[t] = cid

def _migrate_tokens():
    # Zero-downtime migration: merge legacy tokens.yaml into clients.yaml
    tokens_file = CONFIG_DIR / "tokens.yaml"
    if tokens_file.exists():
        try:
            legacy_tokens = yaml.safe_load(tokens_file.read_text()) or {}
            t_map = legacy_tokens.get("tokens", {})
            changed = False
            for tok, cid in t_map.items():
                if cid in _client_cfg.get("clients", {}):
                    _client_cfg["clients"][cid]["token"] = tok
                    changed = True
            if changed:
                with open(CONFIG_DIR / "clients.yaml", "w") as f:
                    yaml.safe_dump(_client_cfg, f)
            import shutil
            shutil.move(str(tokens_file), str(tokens_file) + ".bak")
        except Exception:
            pass

_migrate_tokens()
_build_token_map()
_frontier_cfg     = _load_yaml("frontier.yaml",      {"enabled": False, "providers": {}})
_routing_cfg      = _load_yaml("routing.yaml",        {"routes": []})
_eviction_cfg     = _load_yaml("eviction.yaml",        {"eviction_timeout_min": 15, "vram_threshold_pct": 80, "never_evict": []})
_notify_cfg       = _load_yaml("notifications.yaml",  {"events": {}})
_logging_cfg      = _load_yaml("logging.yaml",        {"enabled": True})
_fallback_cfg     = _load_yaml("fallback.yaml",        {"enabled": False, "mapping": {}})
_audit_cfg        = _load_yaml("audit.yaml",           {"enabled": True, "model": "qwen3:8b", "upstream": 0,
                                                          "max_requests": 300, "max_chars_per_text": 600})
# Wake-on-LAN für dana (GPU-Host): wenn eine Anfrage aufgrund von lokaler
# Nichterreichbarkeit auf ein Frontier-Modell umgeleitet wird, wecken wir dana
# per Magic Packet, damit möglichst schnell wieder lokal geroutet werden kann,
# statt dauerhaft auf Frontier zu bleiben. MAC/Broadcast/Port stammen aus dem
# bereits bestehenden `wakedana`-Skript.
_wol_cfg          = _load_yaml("wol.yaml",             {"enabled": True, "mac": "18:31:BF:B6:18:F1",
                                                          "broadcast": "192.168.10.255", "port": 9,
                                                          "cooldown_s": 300})
_last_wol_sent    = 0.0

_guardrails_cfg = _load_yaml("guardrails.yaml", {"enabled": False, "global_rules": [], "client_rules": {}})
_fail2ban_cfg = _load_yaml("fail2ban.yaml", {"bans": {}})
_pricing_cfg = _load_yaml("pricing.yaml", {
    "fx": {"usd_to_eur": 0.92, "updated": "", "source": ""},
    "models": {}, "default": {"currency": "USD", "input_per_1k": 0.0, "output_per_1k": 0.0}})

def _model_cost_native(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, str]:
    """Cost in whatever currency the model is actually billed in (per
    config/pricing.yaml), not yet converted."""
    p = _pricing_cfg.get("models", {}).get(model) or _pricing_cfg.get("default", {})
    amount = (prompt_tokens or 0) / 1000 * p.get("input_per_1k", 0.0) + \
             (completion_tokens or 0) / 1000 * p.get("output_per_1k", 0.0)
    currency = p.get("currency", "USD")
    return amount, currency

def _to_usd_eur(amount: float, currency: str) -> tuple[float, float]:
    """Converts a native-currency amount to (usd, eur) using the single
    fx.usd_to_eur rate in pricing.yaml. Only USD and EUR native currencies
    are expected in practice; anything else is treated as already-USD
    rather than silently dropping the amount."""
    rate = _pricing_cfg.get("fx", {}).get("usd_to_eur", 0.92)
    if currency == "EUR":
        return (amount / rate if rate else 0.0), amount
    return amount, amount * rate

def _model_cost_usd_eur(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, float]:
    amount, currency = _model_cost_native(model, prompt_tokens, completion_tokens)
    return _to_usd_eur(amount, currency)

from presidio_analyzer import AnalyzerEngine

_presidio_engine = AnalyzerEngine()
_DLP_ENTITIES = ["CREDIT_CARD", "US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "PERSON"]

async def _dlp_mask(text: str) -> str:
    results = await asyncio.to_thread(
        _presidio_engine.analyze, text=text, language="en", entities=_DLP_ENTITIES
    )
    masked = text
    for r in sorted(results, key=lambda r: r.start, reverse=True):
        masked = masked[:r.start] + f"[{r.entity_type}]" + masked[r.end:]
    return masked

def _is_banned(token_name: str) -> bool:
    ban_time = _fail2ban_cfg.get("bans", {}).get(token_name)
    if ban_time:
        import time
        if time.time() < ban_time:
            return True
        else:
            del _fail2ban_cfg["bans"][token_name]
            with open(CONFIG_DIR / "fail2ban.yaml", "w") as f:
                yaml.safe_dump(_fail2ban_cfg, f)
    return False

def _record_violation(token_name: str, client_ip: str, rule: dict, prompt: str):
    import time
    now = time.time()
    
    # SQLite Log
    try:
        _db().execute(
            "INSERT INTO guardrail_events (ts, token_name, client_ip, action, trigger, rule_pattern, snippet) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [datetime.datetime.now().isoformat(), token_name, client_ip, rule.get("action", "deny"), rule.get("trigger", ""), rule.get("pattern", ""), prompt[:200]]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[guardrails] sqlite error: {e}")

    # Track in memory for Fail2Ban
    if not hasattr(_record_violation, "strikes"):
        _record_violation.strikes = {}
    strikes = _record_violation.strikes.setdefault(token_name, [])
    strikes = [s for s in strikes if now - s < 300]
    strikes.append(now)
    _record_violation.strikes[token_name] = strikes
    
    if len(strikes) >= 10:
        _fail2ban_cfg.setdefault("bans", {})[token_name] = now + 3600
        with open(CONFIG_DIR / "fail2ban.yaml", "w") as f:
            yaml.safe_dump(_fail2ban_cfg, f)
        _log_to_splunk("guardrail_fail2ban", {"token_name": token_name, "duration": 3600})


def _get_effective_rules(token_name: str) -> list:
    """Merge global_rules + per-client rules for a given token_name.
    Client rules are appended after global rules, giving them lower precedence
    by default but evaluated in order — so client-specific deny rules fire first
    when placed at the start of client_rules list."""
    global_rules = _guardrails_cfg.get("global_rules", _guardrails_cfg.get("rules", []))
    client_rules = _guardrails_cfg.get("client_rules", {}).get(token_name, {}).get("rules", [])
    return list(global_rules) + list(client_rules)


async def _apply_rules(prompt_text: str, token_name: str, client_ip: str, body: dict,
                 rules: list, record: bool = True) -> tuple[bool, str|None, str, dict, dict|None]:
    """Core rule engine. Returns (modified, violation_msg, action, new_body, triggered_rule)."""
    import re as _re
    prompt_lower = prompt_text.lower()
    modified = False
    new_body = body.copy()

    for rule in rules:
        trigger = rule.get("trigger", "keyword")
        pattern = rule.get("pattern", "")
        action = rule.get("action", "pass")
        mode = rule.get("mode", "enforce")

        hit = False
        if trigger == "dlp":
            masked = await _dlp_mask(prompt_text)
            if masked != prompt_text:
                hit = True
                if action == "rewrite" and mode == "enforce":
                    prompt_text = masked
                    modified = True
        elif trigger == "keyword":
            if pattern.lower() in prompt_lower:
                hit = True
        elif trigger in ("regex", "output_keyword"):
            try:
                if _re.search(pattern, prompt_text, _re.IGNORECASE):
                    hit = True
            except Exception:
                pass

        if hit:
            msg = f"Triggered {trigger}: {pattern!r}"
            if mode in ("shadow", "monitor"):
                _log_to_splunk("guardrail_shadow_violation", {"token_name": token_name, "rule": rule, "snippet": prompt_text[:200]})
                continue
            if action in ("deny", "silent"):
                if record:
                    _record_violation(token_name, client_ip, rule, prompt_text)
                return False, msg, action, new_body, rule
            if action == "redirect":
                new_body["model"] = rule.get("target_model") or DEFAULT_REDIRECT_MODEL
                modified = True
                _log_to_splunk("guardrail_redirected", {"token_name": token_name, "from": body.get("model"), "to": new_body["model"]})
            if action == "warn" and record:
                _record_violation(token_name, client_ip, rule, prompt_text)

    if modified:
        if "messages" in new_body and isinstance(new_body["messages"], list) and new_body["messages"]:
            new_body["messages"][-1]["content"] = prompt_text
        elif "prompt" in new_body:
            new_body["prompt"] = prompt_text
        _log_to_splunk("guardrail_rewritten", {"token_name": token_name})

    return modified, None, "pass", new_body, None


async def _check_output_safeguards(text: str, token_name: str) -> bool:
    if not _guardrails_cfg.get("enabled", False):
        return True
    rules = _get_effective_rules(token_name)
    text_lower = text.lower()
    for rule in rules:
        if rule.get("trigger") == "output_keyword":
            if rule.get("pattern", "").lower() in text_lower:
                _log_to_splunk("output_guardrail_violation", {"token_name": token_name, "rule": rule, "snippet": text[:200]})
                return False
        elif rule.get("trigger") == "output_dlp":
            if await _dlp_mask(text) != text:
                _log_to_splunk("output_guardrail_dlp_leak", {"token_name": token_name, "snippet": text[:200]})
                return False
    return True


async def _run_guardrails(prompt_text: str, token_name: str, client_ip: str, body: dict) -> tuple[bool, str|None, str, dict]:
    if not _guardrails_cfg.get("enabled", False):
        return False, None, "pass", body
    if _is_banned(token_name):
        return False, "Token is banned (Fail2Ban)", "deny", body
    rules = _get_effective_rules(token_name)
    modified, violation, action, new_body, _ = await _apply_rules(prompt_text, token_name, client_ip, body, rules, record=True)
    return modified, violation, action, new_body


async def _guard_safeguards(request: Request):
    if request.url.path not in ["/api/chat", "/api/generate", "/v1/chat/completions"]:
        return
        
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes)
    except Exception:
        return
        
    prompt_text = _extract_last_user_message(body)
    if not prompt_text:
        return
        
    token_name = _get_token_name(request)
    client_ip = _get_client_ip(request)
    
    modified, violation, action, new_body = await _run_guardrails(prompt_text, token_name, client_ip, body)
    
    if action in ("deny", "silent"):
        status = 403 if action == "deny" else 200
        detail = {"error": "Request blocked by guardrails", "violation": violation} if action == "deny" else {"status": "ok"}
        
        _db_log_failure(model=body.get("model", "unknown"), client_ip=client_ip, token_name=token_name,
                        endpoint=request.url.path, status_code=status, failure_reason="guardrail_blocked",
                        last_user_message=prompt_text)
        _log_to_splunk("guardrail_violation", {
            "token_name": token_name, "client_ip": client_ip, "violation": violation, "action": action
        })
        raise HTTPException(status_code=status, detail=detail)
        
    if modified:
        request._body = json.dumps(new_body).encode("utf-8")

_ollama_lock_cfg  = _load_yaml("ollama_lock.yaml",      {"locked": False})

# ── SQLite ─────────────────────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def _db_init():
    con = _db_connect()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id                  INTEGER PRIMARY KEY,
            ts                  TEXT,
            date                TEXT,
            model               TEXT,
            prompt_tokens       INTEGER DEFAULT 0,
            completion_tokens   INTEGER DEFAULT 0,
            total_tokens        INTEGER DEFAULT 0,
            duration_s          REAL,
            tokens_per_second   REAL,
            ttft_s              REAL,
            token_name          TEXT,
            client_ip           TEXT,
            user_agent          TEXT,
            endpoint            TEXT,
            stream              INTEGER DEFAULT 0,
            num_messages        INTEGER DEFAULT 0,
            has_tools           INTEGER DEFAULT 0,
            num_tool_calls      INTEGER DEFAULT 0,
            num_ctx             INTEGER,
            status_code         INTEGER DEFAULT 200,
            gaming_blocked      INTEGER DEFAULT 0,
            complexity_score    REAL,
            predicted_duration_s REAL,
            routed_from         TEXT
        );
        CREATE TABLE IF NOT EXISTS failures (
            id                  INTEGER PRIMARY KEY,
            ts                  TEXT,
            model               TEXT,
            token_name          TEXT,
            client_ip           TEXT,
            endpoint            TEXT,
            status_code         INTEGER,
            failure_reason      TEXT,
            last_user_message   TEXT
        );
        CREATE TABLE IF NOT EXISTS model_baselines (
            model               TEXT PRIMARY KEY,
            median_tps          REAL,
            p10_tps             REAL,
            p90_tps             REAL,
            sample_count        INTEGER DEFAULT 0,
            updated_at          TEXT
        );
        CREATE TABLE IF NOT EXISTS budgets (
            token_name          TEXT,
            date                TEXT,
            tokens_used         INTEGER DEFAULT 0,
            tokens_used_local   INTEGER DEFAULT 0,
            tokens_used_frontier INTEGER DEFAULT 0,
            PRIMARY KEY (token_name, date)
        );
        CREATE TABLE IF NOT EXISTS client_profiles (
            token_name          TEXT PRIMARY KEY,
            avg_complexity      REAL,
            top_model           TEXT,
            peak_hour           INTEGER,
            tool_use_rate       REAL,
            avg_messages        REAL,
            total_requests      INTEGER DEFAULT 0,
            updated_at          TEXT
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY,
            ts          TEXT,
            event       TEXT,
            title       TEXT,
            message     TEXT,
            priority    TEXT DEFAULT 'default',
            read_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_actions (
            id          INTEGER PRIMARY KEY,
            ts          TEXT,
            action      TEXT,
            source      TEXT,
            token_name  TEXT,
            client_ip   TEXT,
            detail      TEXT
        );
        CREATE TABLE IF NOT EXISTS guardrail_events (
            id          INTEGER PRIMARY KEY,
            ts          TEXT,
            token_name  TEXT,
            client_ip   TEXT,
            action      TEXT,
            trigger     TEXT,
            rule_pattern TEXT,
            snippet     TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY,
            username        TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            role            TEXT NOT NULL,
            created_at      TEXT,
            last_login_at   TEXT,
            disabled        INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id              INTEGER PRIMARY KEY,
            key_id          TEXT UNIQUE NOT NULL,
            secret_hash     TEXT NOT NULL,
            owner_type      TEXT NOT NULL,
            owner_name      TEXT NOT NULL,
            role            TEXT NOT NULL,
            created_at      TEXT,
            last_used_at    TEXT,
            disabled        INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS secrets (
            name            TEXT PRIMARY KEY,
            value_encrypted BLOB NOT NULL,
            updated_at      TEXT
        );
    """)
    for col_def in ["hostname TEXT", "prompt_text TEXT", "response_text TEXT", "is_frontier INTEGER DEFAULT 0", "token_name TEXT"]:
        try:
            con.execute(f"ALTER TABLE requests ADD COLUMN {col_def}")
        except Exception:
            pass
    for col_def in ["tokens_used_local INTEGER DEFAULT 0", "tokens_used_frontier INTEGER DEFAULT 0"]:
        try:
            con.execute(f"ALTER TABLE budgets ADD COLUMN {col_def}")
        except Exception:
            pass
            
    # Migration: Rename client_ip to token_name in budgets and profiles (they are purely identity-based)
    for table in ["budgets", "client_profiles"]:
        try:
            con.execute(f"ALTER TABLE {table} RENAME COLUMN client_ip TO token_name")
        except Exception:
            pass
    # Add token_name to tables that track both IP and identity
    for table in ["requests", "failures", "admin_actions"]:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN token_name TEXT")
        except Exception:
            pass
            
    con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_date     ON requests(date);
        CREATE INDEX IF NOT EXISTS idx_model    ON requests(model);
        CREATE INDEX IF NOT EXISTS idx_token    ON requests(token_name);
        CREATE INDEX IF NOT EXISTS idx_ip       ON requests(client_ip);
        CREATE INDEX IF NOT EXISTS idx_ts       ON requests(ts);
        CREATE INDEX IF NOT EXISTS idx_notif_ts ON notifications(ts);
        CREATE INDEX IF NOT EXISTS idx_admin_actions_ts ON admin_actions(ts);
        CREATE INDEX IF NOT EXISTS idx_guardrail_events_ts ON guardrail_events(ts);
    """)

    # Add new metadata columns for full feature readiness
    for col_def in ["project TEXT", "org_group TEXT", "user TEXT"]:
        try:
            con.execute(f"ALTER TABLE requests ADD COLUMN {col_def}")
        except Exception:
            pass
            
    con.commit()
    con.close()

_db_con: sqlite3.Connection | None = None

def _db() -> sqlite3.Connection:
    global _db_con
    if _db_con is None:
        _db_con = _db_connect()
    return _db_con

def _db_insert_request(**kw):
    token_name = kw.get("token_name")
    if token_name:
        meta = _client_cfg.get("metadata", {}).get(token_name, {})
        kw["project"] = meta.get("project", "")
        kw["org_group"] = meta.get("org_group", "")
        kw["user"] = meta.get("user", "")
    if not _logging_cfg.get("enabled", True):
        # Performance-/Token-Metriken immer loggen; Prompt/Response-Text nur bei aktivem Logging
        for key in ("prompt_text", "response_text"):
            kw.pop(key, None)
    now = datetime.datetime.now()
    kw.setdefault("ts",   now.isoformat(timespec="seconds"))
    kw.setdefault("date", now.date().isoformat())
    cols = ", ".join(kw.keys())
    placeholders = ", ".join("?" * len(kw))
    try:
        _db().execute(f"INSERT INTO requests ({cols}) VALUES ({placeholders})", list(kw.values()))
        _db().commit()
    except Exception as e:
        logger.error(f"[db] insert error: {e}")

    _log_to_splunk("request_completed", kw)

def _db_log_failure(*, model: str, client_ip: str, token_name: str = "", endpoint: str,
                    status_code: int, failure_reason: str, last_user_message: str = ""):
    if not _logging_cfg.get("enabled", True):
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute(
            "INSERT INTO failures (ts, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [now, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message[:500]]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[db] failure log error: {e}")

def _log_admin_action(action: str, source: str, client_ip: str = "", token_name: str = "", detail: str = ""):
    """Audit-Trail für /maintenance/*-Aktionen (Stop-All, Ollama-Lock, ...).
    `source` unterscheidet, wer die Aktion ausgelöst hat: 'admin' (per
    X-Admin-Token über Dashboard/API) oder 'auto' (z.B. der ComfyUI-Queue-
    Poller, ohne HTTP-Request). Immer geloggt, unabhängig von logging.yaml -
    Admin-Aktionen sind kein Inferenz-Traffic und sollen auch bei
    deaktiviertem Request-Logging nachvollziehbar bleiben."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute(
            "INSERT INTO admin_actions (ts, action, source, client_ip, token_name, detail) VALUES (?, ?, ?, ?, ?)",
            [now, action, source, client_ip, token_name, detail]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[db] admin_actions log error: {e}")


def _db_get_recent(n: int = 20) -> list[dict]:
    try:
        cur = _db().execute(
            "SELECT ts, model, client_ip, token_name, endpoint, prompt_tokens, completion_tokens, "
            "tokens_per_second, duration_s, status_code FROM requests ORDER BY id DESC LIMIT ?", [n]
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []

# ── Internal notifications ────────────────────────────────────────────────────

_notify_unread: int = 0   # in-memory counter, incremented on new notification


async def _notify(event: str, title: str, message: str, priority: str = "default"):
    global _notify_unread
    if not _notify_cfg.get("events", {}).get(event, True):
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute(
            "INSERT INTO notifications (ts, event, title, message, priority) VALUES (?,?,?,?,?)",
            [now, event, title, message, priority]
        )
        _db().commit()
        _notify_unread += 1
        logger.info(f"[notify] {event}: {title}")
    except Exception as e:
        logger.error(f"[notify] db error: {e}")


def _send_wol_packet() -> bool:
    """Sendet ein Wake-on-LAN Magic Packet an dana. Gleiche Logik wie das
    bestehende `~/.local/bin/wakedana`-Skript, hier fest im Proxy verdrahtet,
    damit der Fallback-Pfad nicht von einem externen Skript abhängt."""
    try:
        mac_bytes = bytes.fromhex(_wol_cfg["mac"].replace(":", ""))
        packet = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (_wol_cfg["broadcast"], int(_wol_cfg["port"])))
        return True
    except Exception as e:
        logger.error(f"[wol] Magic Packet fehlgeschlagen: {e}")
        return False


async def _maybe_wake_dana(reason: str):
    """Bei Frontier-Fallback (lokal nicht erreichbar) versucht, dana per WOL zu
    wecken - respektiert ein Cooldown, damit nicht bei jeder Anfrage während
    des Boot-Vorgangs erneut ein Magic Packet verschickt wird."""
    global _last_wol_sent
    if not _wol_cfg.get("enabled", True):
        return
    now = time.monotonic()
    cooldown = float(_wol_cfg.get("cooldown_s", 300))
    if now - _last_wol_sent < cooldown:
        return
    _last_wol_sent = now
    ok = await asyncio.to_thread(_send_wol_packet)
    if ok:
        logger.info(f"[wol] Magic Packet an dana gesendet ({reason})")
        await _notify("wol_wake", "🔌 Wake-on-LAN: dana geweckt",
                      f"Grund: {reason}. Weitere Wake-Versuche sind für {int(cooldown)}s pausiert.")

# ── Global state ───────────────────────────────────────────────────────────────

_gaming_mode:     bool = False
_gaming_override: str  = "auto"
_stop_all:        bool = False   # Maintenance-Modus: blockiert alle Inference-Requests
# Manueller Schalter (persistiert in config/ollama_lock.yaml): sperrt gezielt nur den
# Zugriff auf lokale Ollama-Modelle, z.B. um beide GPUs für ComfyUI freizuräumen.
# Frontier-Fallback (fallback.yaml) bleibt dabei nutzbar - im Unterschied zu _stop_all,
# das ausnahmslos alles blockiert.
_ollama_locked:   bool = _ollama_lock_cfg.get("locked", False)
# True wenn der aktuelle Lock-Zustand vom ComfyUI-Queue-Poller gesetzt wurde
# (siehe _poll_comfyui_queue) statt manuell per /maintenance/ollama-lock -
# nur dann darf der Poller automatisch wieder entsperren, sobald die Queue
# leer ist. Ein manueller Lock/Unlock setzt dies immer auf False, damit der
# Poller einen bewusst gesetzten Zustand nicht überschreibt.
_ollama_lock_auto: bool = False
_hw_stats:        dict = {}
_loaded_models:   list = []
# Modell-Katalog pro Upstream (0/1), gefüllt von _poll_model_catalog() via /api/tags.
# Nicht zu verwechseln mit _loaded_models (aktuell im VRAM) - das hier ist "installiert,
# aber ggf. gerade nicht geladen". Wird gebraucht, damit das Routing weiss, welches
# Modell wo überhaupt existiert (z.B. Vision-Modelle, die nur auf einer GPU liegen).
_model_catalog:   dict[int, set] = {0: set(), 1: set()}
# Erreichbarkeit pro Ollama-Upstream, gepflegt vom selben Poll wie _model_catalog
# (jeder erfolgreiche /api/tags-Call = healthy). Treibt das Frontier-Fallback in
# proxy_openai(): wenn ein Upstream hier als down markiert ist, wird gar nicht erst
# lokal versucht, sondern direkt auf das konfigurierte Frontier-Modell umgeleitet.
_ollama_healthy:  dict[int, bool] = {0: True, 1: True}
_sse_subscribers: list[asyncio.Queue] = []
_model_baselines: dict = {}   # model → median_tps (in-memory cache)
_load_shed_queue: asyncio.Queue | None = None   # set in run_proxies()
_active_requests: dict = {}   # req_id → {model, client_ip, token_name, endpoint, t_start}
_req_counter:     int  = 0


def _req_start(model: str, client_ip: str, token_name: str, endpoint: str, upstream_idx: int | None = None) -> int:
    global _req_counter
    _req_counter += 1
    rid = _req_counter
    _active_requests[rid] = {"model": model, "client_ip": client_ip, "token_name": token_name,
                              "endpoint": endpoint, "t_start": time.time(),
                              "upstream": upstream_idx}
    return rid


def _req_end(rid: int):
    _active_requests.pop(rid, None)

# ── Background tasks ───────────────────────────────────────────────────────────

async def _poll_gaming_mode():
    """Pollt den Gaming-Mode-Status remote über den gpu-agent (Steam läuft auf
    dem GPU-Host, nicht auf dem Proxy-Host — pgrep lokal würde hier ins Leere
    laufen)."""
    global _gaming_mode, _gaming_override
    prev = False
    async with httpx.AsyncClient(base_url=GPU_AGENT_URL, timeout=5.0) as client:
        while True:
            try:
                r = await client.get("/status")
                r.raise_for_status()
                now = bool(r.json().get("gaming_mode", False))
                if now != prev:
                    if now:
                        logger.info("[gaming] Steam detected — LLM endpoints blocked")
                        await _notify("gaming_mode_start", "🎮 Gaming Mode", "Der GPU-Host ist jetzt im Gaming-Modus. LLM-Anfragen werden geblockt.", "high")
                    else:
                        logger.info("[gaming] Steam closed — LLM endpoints unblocked")
                        await _notify("gaming_mode_end", "✅ Gaming Mode Ende", "Steam beendet — LLM-Endpunkte wieder verfügbar.", "default")
                    prev = now
                _gaming_mode = now
                _gaming_override = r.json().get("gaming_override", "auto")
            except Exception:
                # GPU-Host/gpu-agent nicht erreichbar (z.B. GPU-Host ausgeschaltet) → kein Gaming-Mode annehmen
                _gaming_mode = False
                _gaming_override = "auto"
            await asyncio.sleep(10)


async def _poll_comfyui_queue():
    """Pollt ComfyUIs eigene Queue-API (GET /queue auf dem GPU-Host) und
    setzt/hebt den Ollama-Lock automatisch, je nachdem ob ComfyUI gerade
    etwas zu rendern hat ("läuft" = queue_running oder queue_pending nicht
    leer). Greift nicht ein, wenn der Lock manuell gesetzt wurde (siehe
    _ollama_lock_auto) - ein Admin, der bewusst sperrt, soll nicht durch eine
    leere ComfyUI-Queue überstimmt werden."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                r = await client.get(f"{COMFYUI_UPSTREAM}/queue")
                r.raise_for_status()
                data = r.json()
                busy = bool(data.get("queue_running")) or bool(data.get("queue_pending"))
            except Exception:
                # ComfyUI/GPU-Host nicht erreichbar - keinen Lock auf Basis von
                # Vermutungen setzen oder halten.
                busy = False
            if busy and not _ollama_locked:
                await _set_ollama_lock(True, auto=True, reason="ComfyUI-Queue aktiv")
            elif not busy and _ollama_locked and _ollama_lock_auto:
                # auto=True hier ebenfalls, nur damit der Audit-Log-Eintrag korrekt als
                # "auto" (nicht "admin") attribuiert wird - _ollama_lock_auto=True bei
                # _ollama_locked=False ist unschädlich (wird nur geprüft, wenn beides gilt).
                await _set_ollama_lock(False, auto=True, reason="ComfyUI-Queue leer")
            await asyncio.sleep(10)


async def _poll_hardware():
    """Pollt CPU/RAM/GPU/Temperaturen remote über den gpu-agent — die GPU
    steckt im GPU-Host, nicht im Proxy-Host, auf dem dieser Proxy läuft."""
    global _hw_stats
    async with httpx.AsyncClient(base_url=GPU_AGENT_URL, timeout=5.0) as client:
        while True:
            try:
                r = await client.get("/status")
                r.raise_for_status()
                hw = r.json().get("hw") or {}
                if hw:
                    _hw_stats = hw
                    for g in hw.get("gpus", []):
                        if g.get("temp_c", 0) >= 80:
                            await _notify("thermal_warning", f"🌡️ GPU #{g['index']} Überhitzung",
                                          f"GPU #{g['index']} Temperatur: {g['temp_c']}°C", "urgent")
                else:
                    _hw_stats = {}
            except Exception:
                # GPU-Host/gpu-agent nicht erreichbar → leere Stats statt Crash
                _hw_stats = {}
            await asyncio.sleep(2)


async def _poll_loaded_models():
    global _loaded_models
    async with httpx.AsyncClient(timeout=3.0) as client:
        while True:
            try:
                r0 = await client.get(f"{OLLAMA_UPSTREAM_0}/api/ps")
                m0 = r0.json().get("models", []) if r0.status_code == 200 else []
                for m in m0: m["upstream"] = 0
            except Exception:
                m0 = []
            try:
                r1 = await client.get(f"{OLLAMA_UPSTREAM_1}/api/ps")
                m1 = r1.json().get("models", []) if r1.status_code == 200 else []
                for m in m1: m["upstream"] = 1
            except Exception:
                m1 = []
            _loaded_models = m0 + m1
            await asyncio.sleep(2)


async def _poll_model_catalog():
    """Pollt /api/tags auf beiden Ollama-Instanzen, damit das Routing weiss,
    welches Modell wo überhaupt installiert ist (nicht nur gerade im VRAM).

    Ohne das würde _select_upstream() Requests rein nach GPU-Auslastung
    verteilen und dabei ignorieren, ob das angefragte Modell (z.B. ein
    Vision-Modell, das nur auf einer GPU gepullt wurde) dort existiert -
    der Request landet dann zufällig auf dem falschen Host und schlägt fehl.
    Bei transienten Fehlern bleibt der zuletzt bekannte Katalog erhalten,
    damit ein kurzer Netzwerk-Hänger nicht sofort alle Routing-Entscheidungen
    verfälscht.
    """
    global _model_catalog, _ollama_healthy
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            for idx, upstream in ((0, OLLAMA_UPSTREAM_0), (1, OLLAMA_UPSTREAM_1)):
                try:
                    r = await client.get(f"{upstream}/api/tags")
                    if r.status_code == 200:
                        names = {m.get("name") or m.get("model") for m in r.json().get("models", [])}
                        _model_catalog[idx] = {n for n in names if n}
                        if not _ollama_healthy[idx]:
                            logger.info(f"[fallback] Ollama-Upstream {idx} wieder erreichbar")
                        _ollama_healthy[idx] = True
                    else:
                        _ollama_healthy[idx] = False
                except Exception:
                    # letzten bekannten Katalog für diese GPU behalten, aber als down markieren
                    _ollama_healthy[idx] = False
            await asyncio.sleep(30)


async def _sse_broadcast_loop():
    while True:
        if _sse_subscribers:
            now = time.time()
            # Evict stale in-flight entries older than 5 min (safeguard against missed _req_end)
            stale = [rid for rid, v in _active_requests.items() if now - v["t_start"] > 300]
            for rid in stale:
                _active_requests.pop(rid, None)
            snapshot = {
                "hw":           _hw_stats,
                "models":       _loaded_models,
                "gaming_mode":  _gaming_mode,
                "gaming_override": _gaming_override,
                "unread_count": _notify_unread,
                "recent":       _db_get_recent(5),
                "active":       [{"model": v["model"], "client_ip": v["client_ip"], "token_name": v.get("token_name", ""),
                                  "endpoint": v["endpoint"],
                                  "elapsed_s": round(now - v["t_start"], 1)}
                                 for v in _active_requests.values()],
                "ts":           now,
            }
            dead = []
            for q in _sse_subscribers:
                try:
                    q.put_nowait(snapshot)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass
        await asyncio.sleep(2)


async def _update_baselines():
    """Daily: recompute model tps baselines from last 7 days of data."""
    while True:
        await asyncio.sleep(3600)
        try:
            cur = _db().execute("""
                SELECT model,
                       AVG(tokens_per_second) as median_tps,
                       MIN(tokens_per_second) as p10,
                       MAX(tokens_per_second) as p90,
                       COUNT(*) as n
                FROM requests
                WHERE date >= date('now', '-7 days')
                  AND tokens_per_second IS NOT NULL
                  AND tokens_per_second > 0
                  AND status_code = 200
                GROUP BY model
            """)
            now = datetime.datetime.now().isoformat(timespec="seconds")
            for row in cur.fetchall():
                model, med, p10, p90, n = row
                _model_baselines[model] = med
                _db().execute(
                    "INSERT OR REPLACE INTO model_baselines VALUES (?,?,?,?,?,?)",
                    [model, med, p10, p90, n, now]
                )
            _db().commit()
        except Exception as e:
            logger.error(f"[baselines] update error: {e}")


async def _evict_idle_models():
    """Every 60s: evict idle models from VRAM if threshold exceeded, per GPU."""
    timeout_min = _eviction_cfg.get("eviction_timeout_min", 15)
    vram_threshold = _eviction_cfg.get("vram_threshold_pct", 80)
    never_evict = set(_eviction_cfg.get("never_evict", []))

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            await asyncio.sleep(60)
            try:
                cutoff = (datetime.datetime.now() - datetime.timedelta(minutes=timeout_min)).isoformat()
                gpus = _hw_stats.get("gpus", [])
                for g in gpus:
                    idx = g.get("index", 0)
                    t = g.get("ram_total", 0)
                    u = g.get("ram_used", 0)
                    ram_pct = (u / t * 100) if t > 0 else 0
                    if ram_pct >= vram_threshold:
                        for m in _loaded_models:
                            if m.get("upstream") != idx:
                                continue
                            name = m.get("name", "")
                            if name in never_evict:
                                continue
                            cur = _db().execute("SELECT MAX(ts) FROM requests WHERE model = ?", [name])
                            last_ts = (cur.fetchone() or [None])[0]
                            if last_ts and last_ts < cutoff:
                                logger.info(f"[eviction] evicting {name} on GPU {idx} (idle {timeout_min}min, VRAM: {ram_pct:.0f}%)")
                                upstream = OLLAMA_UPSTREAM_1 if idx == 1 else OLLAMA_UPSTREAM_0
                                try:
                                    # Use generate with keep_alive=0 to unload from VRAM, DO NOT use /api/delete
                                    await client.post(f"{upstream}/api/generate", json={"model": name, "keep_alive": 0})
                                except Exception:
                                    pass
                                _db_log_failure(
                                    model=name, client_ip="proxy", endpoint="/api/generate",
                                    status_code=200, failure_reason="model_evicted",
                                    last_user_message=f"idle>{timeout_min}min vram={ram_pct:.0f}% gpu={idx}"
                                )
                                await _notify("model_evicted", f"🗑️ Model evicted on GPU {idx}: {name}",
                                              f"{name} nach {timeout_min}min Idle entladen. VRAM: {ram_pct:.0f}%")
            except Exception as e:
                logger.error(f"[eviction] error: {e}")


async def _checkpoint_wal():
    """Every 30s: flush WAL to main DB file so read-only Docker mounts see current data."""
    while True:
        await asyncio.sleep(30)
        try:
            _db().execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass


async def _update_client_profiles():
    """Every 6h: recompute per-IP profiles."""
    while True:
        await asyncio.sleep(21600)
        try:
            cur = _db().execute("""
                SELECT token_name,
                       AVG(complexity_score) as avg_cx,
                       MAX(model) as top_model,
                       CAST(strftime('%H', ts) AS INTEGER) as hour,
                       AVG(CAST(has_tools AS REAL)) as tool_rate,
                       AVG(num_messages) as avg_msgs,
                       COUNT(*) as total
                FROM requests
                WHERE date >= date('now', '-30 days')
                GROUP BY token_name
            """)
            now = datetime.datetime.now().isoformat(timespec="seconds")
            for row in cur.fetchall():
                ip, avg_cx, top_model, hour, tool_rate, avg_msgs, total = row
                _db().execute(
                    "INSERT OR REPLACE INTO client_profiles (token_name, avg_complexity, top_model, peak_hour, tool_use_rate, avg_messages, total_requests, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    [ip, avg_cx, top_model, hour, tool_rate, avg_msgs, total, now]
                )
            _db().commit()
        except Exception as e:
            logger.error(f"[profiles] update error: {e}")

# ── Smart feature helpers ──────────────────────────────────────────────────────

def _text_content_len(content) -> int:
    """Länge des reinen Text-Anteils einer Chat-Message.

    OpenAI-Style-Content kann eine Liste aus {"type": "text", ...} und
    {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}
    sein. Ein einzelnes Foto packt oft zehntausende Zeichen Base64 in diese
    URL - würde man das wie vorher via len(str(content)) mitzählen, würde ein
    simpler Bild-Chat fälschlich als extrem hohe Komplexität eingestuft und
    könnte ungewollt Routing-Schwellen aus routing.yaml triggern.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") != "image_url"
        )
    return 0


def _compute_complexity(body: dict) -> float:
    """Score 0.0–1.0: message count + estimated tokens + tools."""
    messages = body.get("messages", [])
    num_msgs = len(messages)
    est_tokens = sum(_text_content_len(m.get("content", "")) // 4 for m in messages)
    has_tools = 1 if body.get("tools") else 0
    raw = (num_msgs * 0.02) + (est_tokens / 50_000) + (has_tools * 0.3)
    return round(min(raw, 1.0), 4)


def _predict_duration(model: str, complexity: float) -> float | None:
    baseline = _model_baselines.get(model)
    if not baseline or baseline <= 0:
        return None
    est_tokens = complexity * 50_000
    return round(est_tokens / baseline, 1)


def _apply_router(model: str, complexity: float) -> tuple[str, str | None, int | None]:
    """Returns (target_model, original_model_if_routed, target_gpu)."""
    if not _routing_cfg.get("enabled", True):
        return model, None, None
    for rule in _routing_cfg.get("routes", []):
        threshold = rule.get("if_complexity_below", 0)
        pattern   = rule.get("model_pattern", "*")
        route_to  = rule.get("route_to", model)
        target_gpu = rule.get("target_gpu", None)
        if threshold == 0 or complexity < threshold:
            if pattern == "*" or (pattern.endswith(":") and model.startswith(pattern[:-1])) or pattern == model:
                if route_to != model or target_gpu is not None:
                    if route_to != model:
                        logger.info(f"[router] {model} → {route_to} (complexity={complexity})")
                    return route_to, (model if route_to != model else None), target_gpu
    return model, None, None

_rr_lock    = __import__("threading").Lock()
_rr_counter = 0

def _upstream_url(idx: int) -> str:
    return OLLAMA_UPSTREAM_1 if idx == 1 else OLLAMA_UPSTREAM_0


def _model_present(idx: int, model: str) -> bool:
    """Ist `model` laut zuletzt gepolltem Katalog auf Upstream `idx` installiert?

    Solange der Katalog für diese GPU noch leer ist (z.B. direkt nach dem
    Start, bevor _poll_model_catalog() den ersten Durchlauf beendet hat),
    wird nicht blockiert - sonst würde ein kalter Start alle Requests auf
    eine GPU zwingen, bis der erste Poll durch ist.
    """
    catalog = _model_catalog.get(idx)
    if not catalog:
        return True
    return model in catalog


def _select_upstream(model: str, target_gpu: int | None) -> str:
    global _rr_counter

    if target_gpu is not None:
        if _model_present(target_gpu, model):
            return _upstream_url(target_gpu)
        other = 1 - target_gpu
        if _model_present(other, model):
            logger.info(f"[router] {model} nicht auf GPU {target_gpu} installiert → GPU {other} (aus routing.yaml abgewichen)")
            return _upstream_url(other)
        # Katalog kennt das Modell auf keiner GPU (z.B. noch nicht gepullt oder
        # Katalog-Poll noch nicht gelaufen) - ursprüngliche Ziel-GPU beibehalten,
        # damit der Fehler vom eigentlichen Ollama-Host kommt statt hier verschluckt zu werden.
        return _upstream_url(target_gpu)

    # Kein festes target_gpu aus routing.yaml: nur unter GPUs wählen, die das
    # Modell laut Katalog tatsächlich haben (Root-Cause-Fix dafür, dass z.B.
    # Vision-Modelle, die nur auf einer GPU gepullt sind, per Load-Balancing
    # zufällig auf die falsche GPU geroutet und dort mit 404 abgelehnt wurden).
    present_on = [idx for idx in (0, 1) if _model_present(idx, model)]
    if len(present_on) == 1:
        only = present_on[0]
        logger.info(f"[router] {model} nur auf GPU {only} installiert → dorthin geroutet")
        return _upstream_url(only)
    if len(present_on) == 0:
        # Katalog kennt das Modell auf keiner GPU - Load-/Round-Robin-Logik unten
        # greift trotzdem, damit unbekannte/neu gepullte Modelle nicht blockiert werden.
        pass

    gpus = _hw_stats.get("gpus", [])
    if len(gpus) >= 2:
        g0_ram = (gpus[0].get("ram_used", 0) / gpus[0].get("ram_total", 1)) * 100 if gpus[0].get("ram_total", 0) > 0 else 0
        g1_ram = (gpus[1].get("ram_used", 0) / gpus[1].get("ram_total", 1)) * 100 if gpus[1].get("ram_total", 0) > 0 else 0
        g0_load = gpus[0].get("load_pct", 0)
        g1_load = gpus[1].get("load_pct", 0)
        g0_busy = g0_load > 80 or g0_ram > 80
        g1_busy = g1_load > 80 or g1_ram > 80
        if g0_busy and not g1_busy:
            logger.info(f"[router] GPU 0 busy → GPU 1 ({model})")
            return OLLAMA_UPSTREAM_1
        if g1_busy and not g0_busy:
            logger.info(f"[router] GPU 1 busy → GPU 0 ({model})")
            return OLLAMA_UPSTREAM_0
        if not g0_busy and not g1_busy:
            # Beide frei: Round-Robin damit parallele Requests auf beide GPUs verteilt werden
            with _rr_lock:
                slot = _rr_counter % 2
                _rr_counter += 1
            upstream = OLLAMA_UPSTREAM_1 if slot == 1 else OLLAMA_UPSTREAM_0
            gpu_idx  = 1 if slot == 1 else 0
            logger.info(f"[router] beide GPUs frei → Round-Robin GPU {gpu_idx} ({model})")
            return upstream
    return OLLAMA_UPSTREAM_0


def _get_client_config(client_ip: str) -> dict:
    clients = _client_cfg.get("clients", {})
    return clients.get(client_ip, clients.get("default", {"limit": 5_000_000, "models": "*", "blocked": False}))

def _get_budget(client_ip: str, is_frontier: bool = False) -> int:
    cfg = _get_client_config(client_ip)
    limit_local = cfg.get("limit_local", -1)
    limit_frontier = cfg.get("limit_frontier", 1_000_000)
    return limit_frontier if is_frontier else limit_local

def _is_blocked(client_ip: str) -> bool:
    cfg = _get_client_config(client_ip)
    return cfg.get("blocked", False)

def _is_model_in_list(model: str, allowed) -> bool:
    if allowed == "*":
        return True
    if isinstance(allowed, list):
        for m in allowed:
            if m == "*" or model == m or (m.endswith(":") and model.startswith(m[:-1])):
                return True
    return False


def _get_frontier_target(model: str) -> tuple[str, str] | None:
    if not _frontier_cfg.get("enabled", False):
        return None
    providers = _frontier_cfg.get("providers", {})
    for provider, config in providers.items():
        models = config.get("models", [])
        if model in models:
            return config.get("base_url", ""), config.get("api_key", "")
    return None


def _get_fallback_frontier(model: str) -> tuple[str, str, str] | None:
    """Falls für `model` ein Frontier-Fallback konfiguriert ist: (frontier_model, base_url, api_key).
    Sonst None (kein Fallback konfiguriert, oder das gemappte Modell hat keinen Frontier-Provider).
    Ein "*"-Key in mapping wirkt als Catchall für jedes Modell ohne eigenen Eintrag
    (exakter Modell-Match hat immer Vorrang vor dem Catchall)."""
    if not _fallback_cfg.get("enabled", False):
        return None
    mapping = _fallback_cfg.get("mapping", {})
    fb_model = mapping.get(model) or mapping.get("*")
    if not fb_model:
        return None
    target = _get_frontier_target(fb_model)
    if not target:
        return None
    return (fb_model, target[0], target[1])


def _check_budget(token_name: str, is_frontier: bool = False) -> tuple[bool, int, int]:
    """Returns (allowed, used, limit)."""
    today = datetime.date.today().isoformat()
    limit = _get_budget(token_name, is_frontier)
    col = "tokens_used_frontier" if is_frontier else "tokens_used_local"
    try:
        cur = _db().execute(
            f"SELECT {col} FROM budgets WHERE token_name=? AND date=?", [token_name, today]
        )
        row = cur.fetchone()
        used = row[0] if row else 0
    except Exception:
        used = 0
    return limit == -1 or used < limit, used, limit


def _add_budget_usage(token_name: str, tokens: int, is_frontier: bool = False):
    today = datetime.date.today().isoformat()
    col = "tokens_used_frontier" if is_frontier else "tokens_used_local"
    try:
        _db().execute(
            f"INSERT INTO budgets (token_name, date, tokens_used, {col}) VALUES (?,?,?,?) "
            f"ON CONFLICT(token_name, date) DO UPDATE SET tokens_used = tokens_used + ?, {col} = {col} + ?",
            [token_name, today, tokens, tokens, tokens, tokens]
        )
        _db().commit()
    except Exception:
        pass


async def _check_budget_warnings(token_name: str, is_frontier: bool = False):
    today = datetime.date.today().isoformat()
    limit = _get_budget(token_name, is_frontier)
    if limit == -1: return
    col = "tokens_used_frontier" if is_frontier else "tokens_used_local"
    lbl = "Frontier-" if is_frontier else "Lokal-"
    try:
        cur = _db().execute(
            f"SELECT {col} FROM budgets WHERE token_name=? AND date=?", [token_name, today]
        )
        row = cur.fetchone()
        used = row[0] if row else 0
        pct = used / limit * 100 if limit > 0 else 0
        if 80 <= pct < 100:
            await _notify("budget_warning", f"⚠️ {lbl}Budget-Warnung {token_name}",
                          f"{token_name}: {pct:.0f}% des {lbl}Tages-Budgets verbraucht ({used:,}/{limit:,} Tokens)")
        elif pct >= 100:
            await _notify("budget_exceeded", f"🚫 {lbl}Budget überschritten {token_name}",
                          f"{token_name}: {lbl}Tages-Budget erschöpft ({limit:,} Tokens). Reset um Mitternacht.", "high")
    except Exception:
        pass


def _check_tps_anomaly(model: str, tps: float | None, completion_tokens: int = 0) -> bool:
    # Short completions are dominated by fixed overhead (dispatch, TTFT), so
    # a handful of tokens naturally computes a low tps that isn't a real
    # slowdown — agentic tool-calling loops fire many of these back to back
    # and would otherwise spam a notification per call. Require enough
    # tokens for the measurement to be meaningful.
    if completion_tokens < 20:
        return False
    if tps is None or tps <= 0:
        return False
    baseline = _model_baselines.get(model)
    if not baseline or baseline <= 0:
        return False
    return tps < (baseline * 0.5)


def _extract_last_user_message(body: dict) -> str:
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(parts)
            return str(content)[:500]
    return body.get("prompt", "")[:500]


def _resolve_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ip


# ── Load shedding ──────────────────────────────────────────────────────────────

def _gpu_overloaded() -> bool:
    gpus = _hw_stats.get("gpus", [])
    if not gpus:
        return False
    return any(g.get("load_pct", 0) > 95 for g in gpus)

# ── FastAPI app + clients ──────────────────────────────────────────────────────

app = FastAPI(title="llmproxy", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# read=None, weil Generation lange dauern darf - aber connect/pool sind begrenzt,
# damit ein unerreichbarer/hängender GPU-Host den Request nicht unbegrenzt blockiert
# (vorher: timeout=None ganz ohne Limit, auch für den TCP-Connect).
_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0))


def _get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_token_name(request: Request) -> str:
    """Resolve the calling app's identity: bearer token if present (preferred,
    stable across DHCP), otherwise fall back to client IP (legacy callers)."""
    auth = request.headers.get("authorization", "")
    if auth:
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail={"error": "invalid Authorization header"})
        token = auth[7:].strip()
        token_name = _token_map.get(token)
        if not token_name:
            raise HTTPException(status_code=401, detail={"error": "invalid token"})
        return token_name
    return _get_client_ip(request)


async def _guard_stop_all(request: Request):
    if _stop_all:
        raise HTTPException(
            status_code=503,
            detail={"error": "llmproxy is in stop-all maintenance mode — all inference blocked",
                    "stop_all": True, "retry_after": 60},
            headers={"Retry-After": "60"},
        )


async def _guard_ollama_lock(request: Request):
    """Blockiert native/Embeddings-Endpunkte hart, wenn der Ollama-Lock aktiv ist.
    Diese Endpunkte kennen kein Frontier-Fallback (siehe fallback.yaml-Doku) -
    /v1/chat/completions hat stattdessen eine eigene Prüfung mit Fallback-Versuch."""
    if _ollama_locked:
        raise HTTPException(
            status_code=503,
            detail={"error": "local Ollama access is disabled (ollama_locked)", "ollama_locked": True, "retry_after": 60},
            headers={"Retry-After": "60"},
        )


async def _guard_gaming(request: Request):
    if _gaming_mode:
        client_ip = _get_client_ip(request)
        _db_insert_request(
            model="blocked", client_ip=client_ip,
            endpoint=str(request.url.path), gaming_blocked=1,
            status_code=503, duration_s=0,
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "GPU host is in gaming mode", "gaming_mode": True, "retry_after": 60},
            headers={"Retry-After": "60"},
        )


def _guard_budget_sync(token_name: str, is_frontier: bool = False):
    if _is_blocked(token_name):
        raise HTTPException(status_code=403, detail={"error": "client is blocked"})
    allowed, used, limit = _check_budget(token_name, is_frontier)
    if limit != -1 and not allowed:
        secs_until_midnight = int(
            (datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1),
                                       datetime.time()) - datetime.datetime.now()).total_seconds()
        )
        raise HTTPException(
            status_code=429,
            detail={"error": "daily token budget exceeded", "used": used, "limit": limit,
                    "retry_after": secs_until_midnight},
            headers={"Retry-After": str(secs_until_midnight)},
        )

async def _guard_blocked(request: Request):
    token_name = _get_token_name(request)
    if _is_blocked(token_name):
        raise HTTPException(status_code=403, detail={"error": "client is blocked"})

# ── Native Ollama: /api/chat and /api/generate ─────────────────────────────────

@app.post("/api/chat",     dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_ollama_lock), Depends(_guard_safeguards)])
@app.post("/api/generate", dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_ollama_lock), Depends(_guard_safeguards)])
async def proxy_native(request: Request):
    path      = request.url.path
    client_ip = _get_client_ip(request)
    token_name = _get_token_name(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    stream    = body.get("stream", True)
    num_ctx   = body.get("options", {}).get("num_ctx")
    cx_score  = _compute_complexity(body)
    pred_dur  = _predict_duration(model, cx_score)
    
    is_frontier = _get_frontier_target(model) is not None
    _guard_budget_sync(token_name, is_frontier)

    model, _, target_gpu = _apply_router(model, cx_score)
    upstream_url = _select_upstream(model, target_gpu)
    upstream_idx = 1 if upstream_url == OLLAMA_UPSTREAM_1 else 0

    client_cfg = _get_client_config(token_name)
    if not _is_model_in_list(model, client_cfg.get("models", "*")):
        raise HTTPException(status_code=403, detail={"error": f"model {model} not allowed for client {client_ip}"})

    logger.info(f"[proxy] {request.method} {path} model={model} cx={cx_score} from={client_ip}")
    _rid = _req_start(model, client_ip, token_name, path, upstream_idx)
    t0 = time.monotonic()

    prompt_text = (_extract_last_user_message(body) or body.get("prompt", ""))[:2000]
    hostname = _resolve_hostname(client_ip)

    # Outage fallback (native clients): _guard_ollama_lock above already handles the
    # *deliberate* ollama_locked case with a hard 503, no fallback, by design. This
    # covers the separate, unplanned case -- Ollama actually being down -- which until
    # now just propagated as a bare 500 for native clients (unlike /v1/chat/completions,
    # which has always had this).
    fb = _get_fallback_frontier(model)
    frontier_allowed = fb is not None and client_cfg.get("frontier_allowed", False)

    async def _frontier_fallback_native(reason: str):
        fb_model, fb_base_url, fb_api_key = fb
        logger.warning(f"[fallback] Ollama-Upstream {upstream_idx} {reason} → {model} auf Frontier {fb_model} umgeleitet (native)")
        await _maybe_wake_dana(f"Upstream {upstream_idx} {reason}")
        _guard_budget_sync(token_name, is_frontier=True)
        openai_body = _native_request_to_openai(body, fb_model)
        fr_resp = await _proxy_frontier_openai(request, openai_body, fb_model, False, client_ip, ua, cx_score,
                                               fb_base_url, fb_api_key, token_name=token_name)
        if fr_resp.status_code != 200:
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint=path,
                            status_code=fr_resp.status_code, failure_reason="frontier_upstream_error",
                            last_user_message=prompt_text)
            raise RuntimeError(f"frontier fallback failed: HTTP {fr_resp.status_code}")
        openai_data = json.loads(fr_resp.body)
        return _openai_response_to_native(openai_data, model)

    if frontier_allowed and not _ollama_healthy.get(upstream_idx, True):
        native_data = await _frontier_fallback_native("down (health-check)")
        if not stream:
            return Response(content=json.dumps(native_data), media_type="application/json")
        async def _fallback_stream():
            yield (json.dumps({**native_data, "done": False}) + "\n").encode()
            yield (json.dumps({"model": model, "done": True, "done_reason": "stop",
                                "prompt_eval_count": native_data["prompt_eval_count"],
                                "eval_count": native_data["eval_count"],
                                "message": {"role": "assistant", "content": ""}}) + "\n").encode()
        return StreamingResponse(_fallback_stream(), media_type="application/x-ndjson")

    if not stream:
        try:
            try:
                resp = await _client.post(f"{upstream_url}{path}", json=body)
            except Exception as e:
                _ollama_healthy[upstream_idx] = False
                if frontier_allowed:
                    native_data = await _frontier_fallback_native(f"nicht erreichbar ({e})")
                    return Response(content=json.dumps(native_data), media_type="application/json")
                _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint=path,
                                status_code=500, failure_reason="upstream_error",
                                last_user_message=prompt_text)
                raise
            duration_s = time.monotonic() - t0
            data = resp.json()
            pt   = data.get("prompt_eval_count", 0)
            ct   = data.get("eval_count", 0)
            tps  = round(ct / duration_s, 1) if duration_s > 0 and ct > 0 else None
            resp_text = (data.get("response") or data.get("message", {}).get("content") or "")[:2000]
            _db_insert_request(model=model, prompt_tokens=pt, completion_tokens=ct,
                               total_tokens=pt+ct, duration_s=round(duration_s,3),
                               tokens_per_second=tps, client_ip=client_ip, token_name=token_name, user_agent=ua,
                               endpoint=path, stream=0, num_ctx=num_ctx,
                               complexity_score=cx_score, predicted_duration_s=pred_dur,
                               status_code=resp.status_code, is_frontier=int(is_frontier),
                               hostname=hostname, prompt_text=prompt_text, response_text=resp_text)
            _add_budget_usage(token_name, pt+ct, is_frontier=is_frontier)
            await _check_budget_warnings(token_name, is_frontier=is_frontier)
            if _check_tps_anomaly(model, tps, ct):
                _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint=path,
                                status_code=200, failure_reason="tps_anomaly",
                                last_user_message=f"tps={tps} baseline={_model_baselines.get(model)}")
                await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                              f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")
            return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)
        finally:
            _req_end(_rid)

    async def stream_and_log():
        pt = ct = 0
        ttft_s = None
        resp_parts = []
        try:
            async with _client.stream("POST", f"{upstream_url}{path}", json=body) as upstream_conn:
                async for chunk in upstream_conn.aiter_bytes():
                    if ttft_s is None:
                        ttft_s = time.monotonic() - t0
                    yield chunk
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        try:
                            d = json.loads(line)
                            if d.get("done"):
                                pt = d.get("prompt_eval_count", pt)
                                ct = d.get("eval_count", ct)
                            content = d.get("response") or d.get("message", {}).get("content") or ""
                            if content:
                                resp_parts.append(content)
                        except (json.JSONDecodeError, ValueError):
                            pass
        except Exception as e:
            if not resp_parts and frontier_allowed:
                # Nothing streamed yet (failed at connect) -- safe to still swap
                # in the frontier fallback under the same ndjson response.
                _ollama_healthy[upstream_idx] = False
                native_data = await _frontier_fallback_native(f"nicht erreichbar ({e})")
                yield (json.dumps({**native_data, "done": False}) + "\n").encode()
                yield (json.dumps({"model": model, "done": True, "done_reason": "stop",
                                    "prompt_eval_count": native_data["prompt_eval_count"],
                                    "eval_count": native_data["eval_count"],
                                    "message": {"role": "assistant", "content": ""}}) + "\n").encode()
                return
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint=path,
                            status_code=500, failure_reason="upstream_error",
                            last_user_message=prompt_text)
            raise
        finally:
            _req_end(_rid)
        duration_s = time.monotonic() - t0
        tps = round(ct / duration_s, 1) if duration_s > 0 and ct > 0 else None
        resp_text = "".join(resp_parts)[:2000]
        _db_insert_request(model=model, prompt_tokens=pt, completion_tokens=ct,
                           total_tokens=pt+ct, duration_s=round(duration_s,3),
                           tokens_per_second=tps, ttft_s=round(ttft_s,3) if ttft_s else None,
                           client_ip=client_ip, token_name=token_name, user_agent=ua, endpoint=path, stream=1,
                           num_ctx=num_ctx, complexity_score=cx_score, predicted_duration_s=pred_dur,
                           hostname=hostname, prompt_text=prompt_text, response_text=resp_text, is_frontier=int(is_frontier))
        _add_budget_usage(token_name, pt+ct, is_frontier=is_frontier)
        await _check_budget_warnings(token_name, is_frontier=is_frontier)
        if _check_tps_anomaly(model, tps, ct):
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint=path,
                            status_code=200, failure_reason="tps_anomaly",
                            last_user_message=f"tps={tps} baseline={_model_baselines.get(model)}")
            await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                          f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")

    return StreamingResponse(stream_and_log(), media_type="application/x-ndjson")


# ── OpenAI-compat: /v1/chat/completions ───────────────────────────────────────

def _parse_openai_content(content):
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        texts, images = [], []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:image"):
                    try:
                        images.append(url.split(",")[1])
                    except IndexError:
                        pass
        return "\n".join(texts), images
    return str(content), []


def _openai_to_native_chat(body: dict) -> dict:
    raw = []
    for msg in body.get("messages", []):
        text, images = _parse_openai_content(msg.get("content", ""))
        role = msg.get("role", "user")
        new_msg: dict = {"role": role, "content": text}
        if images:
            new_msg["images"] = images
        if role == "tool" and "tool_call_id" in msg:
            new_msg["tool_call_id"] = msg["tool_call_id"]
        if "tool_calls" in msg and msg["tool_calls"]:
            tcs = []
            for tc in msg["tool_calls"]:
                if "function" not in tc:
                    continue
                args = tc["function"].get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                tcs.append({"function": {"name": tc["function"].get("name",""), "arguments": args}})
            new_msg["tool_calls"] = tcs
        raw.append(new_msg)

    messages = []
    for msg in raw:
        if not messages:
            messages.append(msg)
            continue
        last = messages[-1]
        if msg["role"] in ("system",) or last["role"] in ("system",):
            messages.append(msg)
            continue
        if msg["role"] == last["role"] and msg["role"] != "tool":
            last["content"] = (last.get("content") or "") + "\n\n" + (msg.get("content") or "")
            if "images" in msg:
                last.setdefault("images", []).extend(msg["images"])
            if "tool_calls" in msg:
                last.setdefault("tool_calls", []).extend(msg["tool_calls"])
        else:
            messages.append(msg)

    model_name = body.get("model", "")
    model_options = dict(DEFAULT_OLLAMA_OPTIONS)
    if model_name in MODEL_NUM_CTX_OVERRIDES:
        model_options["num_ctx"] = MODEL_NUM_CTX_OVERRIDES[model_name]
    native: dict = {
        "model":   model_name,
        "messages": messages,
        "stream":  body.get("stream", False),
        "options": {**model_options, **body.get("options", {})},
    }
    if body.get("tools"):
        native["tools"] = body["tools"]
    opts = native["options"]
    if body.get("max_tokens", 0) and body["max_tokens"] > 0:
        opts.setdefault("num_predict", body["max_tokens"])
    for k, ok in [("temperature","temperature"),("top_p","top_p"),
                  ("presence_penalty","presence_penalty"),("frequency_penalty","frequency_penalty")]:
        if k in body:
            opts.setdefault(ok, body[k])
    if "stop" in body:
        s = body["stop"]
        opts["stop"] = [s] if isinstance(s, str) else s
    return native


def _repair_json_arguments(s: str, max_attempts: int = 5) -> str:
    """Best-effort repair for malformed tool-call arguments JSON coming
    straight from the model — heavily quantized models routinely drop a
    comma between fields, especially deep into a long context. Returns a
    guaranteed-valid re-serialized JSON string on success, or the original
    string unchanged if it was already valid or repair didn't converge."""
    attempt = s
    for _ in range(max_attempts):
        try:
            parsed = json.loads(attempt)
            return json.dumps(parsed) if attempt is not s else s
        except json.JSONDecodeError as e:
            if "Expecting ',' delimiter" in e.msg and e.pos > 0:
                attempt = attempt[:e.pos] + "," + attempt[e.pos:]
                continue
            break
    if attempt is not s:
        logger.warning(f"[proxy] could not repair malformed tool-call arguments JSON: {s[:200]!r}")
    return s


def _native_to_openai(data: dict, model: str, stream: bool = False,
                       req_id: str = "chatcmpl-proxy", req_ts: int = None) -> dict:
    msg  = data.get("message", {})
    text = msg.get("content", "")
    native_tcs = msg.get("tool_calls", [])
    finish = "tool_calls" if native_tcs else ("stop" if data.get("done") else None)
    ts = req_ts or int(time.time())

    openai_tcs = []
    for i, tc in enumerate(native_tcs):
        args = tc.get("function", {}).get("arguments", {})
        item = {
            "id": f"call_{i}_{hex(ts)[2:]}",
            "type": "function",
            "function": {
                "name": tc.get("function", {}).get("name", ""),
                "arguments": _repair_json_arguments(args) if isinstance(args, str) else json.dumps(args),
            },
        }
        if stream:
            item["index"] = i
        openai_tcs.append(item)

    if stream:
        delta: dict = {}
        if text:
            delta["content"] = text
        if openai_tcs:
            delta["tool_calls"] = openai_tcs
        return {"id": req_id, "object": "chat.completion.chunk", "created": ts, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    return {
        "id": req_id, "object": "chat.completion", "created": ts, "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text,
                                 **({"tool_calls": openai_tcs} if openai_tcs else {})},
                     "finish_reason": finish or "stop"}],
        "usage": {"prompt_tokens": data.get("prompt_eval_count", 0),
                  "completion_tokens": data.get("eval_count", 0),
                  "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0)},
    }


def _native_request_to_openai(body: dict, model: str) -> dict:
    """Native /api/chat or /api/generate request body -> OpenAI chat-completions
    body, for frontier fallback of native-Ollama clients (e.g. cassandra)."""
    messages = body.get("messages")
    if messages is None:
        # /api/generate uses a flat "prompt" instead of a "messages" list.
        messages = []
        if body.get("system"):
            messages.append({"role": "system", "content": body["system"]})
        messages.append({"role": "user", "content": body.get("prompt", "")})
    else:
        messages = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]

    openai_body: dict = {"model": model, "messages": messages, "stream": False}
    opts = body.get("options", {}) or {}
    if "num_predict" in opts:
        openai_body["max_tokens"] = opts["num_predict"]
    if "temperature" in opts:
        openai_body["temperature"] = opts["temperature"]
    if "top_p" in opts:
        openai_body["top_p"] = opts["top_p"]
    if body.get("tools"):
        openai_body["tools"] = body["tools"]
    if body.get("format") == "json":
        openai_body["response_format"] = {"type": "json_object"}
    return openai_body


def _openai_response_to_native(data: dict, model: str) -> dict:
    """Inverse of _native_to_openai: OpenAI chat-completions response ->
    native Ollama /api/chat response shape, so a native client never has to
    know a request was actually served by a frontier fallback."""
    choices = data.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    content = msg.get("content", "") or ""

    tool_calls = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        tool_calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})

    usage = data.get("usage", {})
    return {
        "model": model,
        "message": {"role": "assistant", "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {})},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "eval_count": usage.get("completion_tokens", 0),
    }


async def _proxy_frontier_openai(request, body, model, stream, client_ip, ua, cx_score, base_url, api_key, token_name=None):
    token_name = token_name or client_ip
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    prompt_text = _extract_last_user_message(body)[:2000]

    is_anthropic = "anthropic/" in model or "claude-" in model
    if is_anthropic:
        messages = body.get("messages", [])
        if messages and isinstance(messages, list):
            for msg in messages:
                if msg.get("role") == "system":
                    content = msg.get("content", "")
                    if _text_content_len(content) > 1024:
                        if isinstance(content, str):
                            msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                        elif isinstance(content, list):
                            for block in reversed(content):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    block["cache_control"] = {"type": "ephemeral"}
                                    break
                        break

    hostname = _resolve_hostname(client_ip)
    pred_dur = _predict_duration(model, cx_score)
    num_messages = len(body.get("messages", []))
    has_tools = bool(body.get("tools"))
    
    t0 = time.monotonic()
    _rid = _req_start(model, client_ip, token_name, "/v1/chat/completions (frontier)")
    
    def _log(pt, ct, dur, status=200, ttft=None, ntc=0, resp_text=""):
        tps = round(ct / dur, 1) if dur > 0 and ct > 0 else None
        _db_insert_request(
            model=model, prompt_tokens=pt, completion_tokens=ct, total_tokens=pt+ct,
            duration_s=round(dur, 3), tokens_per_second=tps, ttft_s=round(ttft, 3) if ttft else None,
            client_ip=client_ip, token_name=token_name, user_agent=ua, endpoint="/v1/chat/completions",
            stream=int(stream), num_messages=num_messages, has_tools=int(has_tools),
            num_tool_calls=ntc, num_ctx=None, status_code=status,
            complexity_score=cx_score, predicted_duration_s=pred_dur,
            routed_from=None, hostname=hostname, prompt_text=prompt_text, response_text=resp_text[:2000],
            is_frontier=1
        )
        _add_budget_usage(token_name, pt+ct, is_frontier=True)
        return tps

    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    if not stream:
        try:
            resp = await _client.post(endpoint, json=body, headers=headers)
        except Exception:
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions", status_code=500, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
            _req_end(_rid)
            raise
        dur = time.monotonic() - t0
        _req_end(_rid)
        if resp.status_code != 200:
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions", status_code=resp.status_code, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
            return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)
        data = resp.json()
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        resp_text = ""
        ntc = 0
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            resp_text = msg.get("content", "")
            ntc = len(msg.get("tool_calls", []))
        _log(pt, ct, dur, status=200, ntc=ntc, resp_text=resp_text)
        await _check_budget_warnings(token_name, is_frontier=True)
        return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)
        
    body["stream_options"] = {"include_usage": True}
    pt_acc = ct_acc = ntc_acc = 0
    ttft_s = None
    
    async def sse():
        nonlocal pt_acc, ct_acc, ntc_acc, ttft_s
        content_acc = []
        try:
            async with _client.stream("POST", endpoint, json=body, headers=headers) as upstream:
                async for line in upstream.aiter_lines():
                    if not line.strip():
                        continue
                    if ttft_s is None:
                        ttft_s = time.monotonic() - t0
                    yield f"{line}\n"
                    if line.startswith("data: "):
                        raw = line[6:]
                        if raw == "[DONE]":
                            continue
                        try:
                            d = json.loads(raw)
                            choices = d.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                if "content" in delta and delta["content"]:
                                    content_acc.append(delta["content"])
                                    ct_acc += 1
                                if "tool_calls" in delta and delta["tool_calls"]:
                                    ntc_acc += len(delta["tool_calls"])
                            usage = d.get("usage")
                            if usage:
                                pt_acc = usage.get("prompt_tokens", pt_acc)
                                ct_acc = usage.get("completion_tokens", ct_acc)
                        except Exception:
                            pass
        except Exception:
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions", status_code=500, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
            raise
        finally:
            _req_end(_rid)
        dur = time.monotonic() - t0
        resp_text = "".join(content_acc)
        _log(pt_acc, ct_acc, dur, ttft=ttft_s, ntc=ntc_acc, resp_text=resp_text)
        await _check_budget_warnings(token_name, is_frontier=True)
        
    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/v1/chat/completions", dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_safeguards)])
async def proxy_openai(request: Request):
    client_ip = _get_client_ip(request)
    token_name = _get_token_name(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    stream    = body.get("stream", False)
    cx_score  = _compute_complexity(body)

    client_cfg = _get_client_config(token_name)
    frontier = _get_frontier_target(model)
    
    if frontier:
        if not client_cfg.get("frontier_allowed", False):
            raise HTTPException(status_code=403, detail={"error": "Frontier models are not allowed for this client"})
        allowed_frontier = client_cfg.get("frontier_models", "*")
        if not _is_model_in_list(model, allowed_frontier):
            raise HTTPException(status_code=403, detail={"error": f"frontier model {model} not allowed for client {client_ip}"})
        _guard_budget_sync(token_name, is_frontier=True)
        return await _proxy_frontier_openai(request, body, model, stream, client_ip, ua, cx_score, frontier[0], frontier[1], token_name=token_name)
    else:
        allowed_local = client_cfg.get("models", "*")
        if not _is_model_in_list(model, allowed_local):
            raise HTTPException(status_code=403, detail={"error": f"local model {model} not allowed for client {client_ip}"})
        _guard_budget_sync(token_name, is_frontier=False)

    # Auto-router
    routed_model, routed_from, target_gpu = _apply_router(model, cx_score)
    if routed_from:
        body["model"] = routed_model
        model = routed_model
    upstream_url = _select_upstream(model, target_gpu)
    upstream_idx = 1 if upstream_url == OLLAMA_UPSTREAM_1 else 0

    # Ollama-Lock: manueller Schalter (config/ollama_lock.yaml), unabhängig vom
    # Health-Check unten - Ollama ist hier tatsächlich erreichbar, der Zugriff ist nur
    # bewusst gesperrt (z.B. um GPUs für ComfyUI freizuräumen). Ohne diesen expliziten
    # Block würde die Anfrage unten einfach normal durchlaufen, weil _ollama_healthy
    # weiterhin True ist.
    if _ollama_locked:
        fb = _get_fallback_frontier(model)
        if fb and client_cfg.get("frontier_allowed", False):
            fb_model, fb_base_url, fb_api_key = fb
            logger.warning(f"[ollama-lock] lokaler Zugriff gesperrt → {model} auf Frontier {fb_model} umgeleitet")
            _guard_budget_sync(token_name, is_frontier=True)
            body["model"] = fb_model
            return await _proxy_frontier_openai(request, body, fb_model, stream, client_ip, ua, cx_score, fb_base_url, fb_api_key, token_name=token_name)
        raise HTTPException(
            status_code=503,
            detail={"error": "local Ollama access is disabled (ollama_locked)", "ollama_locked": True, "retry_after": 60},
            headers={"Retry-After": "60"},
        )

    # Ollama-Fallback (proaktiv): der Health-Check in _poll_model_catalog() hat diesen
    # Upstream bereits als down markiert → gar nicht erst lokal versuchen, sondern direkt
    # auf das für `model` konfigurierte Frontier-Modell umleiten (config/fallback.yaml).
    if not _ollama_healthy.get(upstream_idx, True):
        fb = _get_fallback_frontier(model)
        if fb and client_cfg.get("frontier_allowed", False):
            fb_model, fb_base_url, fb_api_key = fb
            logger.warning(f"[fallback] Ollama-Upstream {upstream_idx} down (health-check) → {model} auf Frontier {fb_model} umgeleitet")
            await _maybe_wake_dana(f"Upstream {upstream_idx} down (health-check)")
            _guard_budget_sync(token_name, is_frontier=True)
            body["model"] = fb_model
            return await _proxy_frontier_openai(request, body, fb_model, stream, client_ip, ua, cx_score, fb_base_url, fb_api_key, token_name=token_name)

    native_body   = _openai_to_native_chat(body)
    num_messages  = len(native_body.get("messages", []))
    has_tools     = bool(native_body.get("tools"))
    num_ctx       = native_body.get("options", {}).get("num_ctx")
    pred_dur      = _predict_duration(model, cx_score)

    if not _is_model_in_list(model, client_cfg.get("models", "*")):
        raise HTTPException(status_code=403, detail={"error": f"routed local model {model} not allowed for client {client_ip}"})

    logger.info(f"[proxy] POST /v1/chat/completions model={model} msgs={num_messages} cx={cx_score} from={client_ip}")

    prompt_text = _extract_last_user_message(body)[:2000]
    hostname = _resolve_hostname(client_ip)

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    req_ts = int(time.time())
    t0     = time.monotonic()
    _rid   = _req_start(model, client_ip, token_name, "/v1/chat/completions", upstream_idx)

    def _log(pt, ct, dur, status=200, ttft=None, ntc=0, resp_text=""):
        tps = round(ct / dur, 1) if dur > 0 and ct > 0 else None
        _db_insert_request(
            model=model, prompt_tokens=pt, completion_tokens=ct, total_tokens=pt+ct,
            duration_s=round(dur, 3), tokens_per_second=tps,
            ttft_s=round(ttft, 3) if ttft else None,
            client_ip=client_ip, token_name=token_name, user_agent=ua, endpoint="/v1/chat/completions",
            stream=int(stream), num_messages=num_messages, has_tools=int(has_tools),
            num_tool_calls=ntc, num_ctx=num_ctx, status_code=status,
            complexity_score=cx_score, predicted_duration_s=pred_dur,
            routed_from=routed_from, is_frontier=0,
            hostname=hostname, prompt_text=prompt_text, response_text=resp_text[:2000],
        )
        _add_budget_usage(token_name, pt+ct, is_frontier=False)
        return tps

    if not stream:
        try:
            try:
                resp = await _client.post(f"{upstream_url}/api/chat", json=native_body)
            except Exception as e:
                # Ollama-Fallback (reaktiv): der Health-Check hat diesen Ausfall noch nicht
                # gesehen (Poll läuft alle 30s) — bei einem echten Verbindungsfehler sofort
                # auf Frontier umleiten, statt bis zum nächsten Poll auf dem toten Upstream
                # zu scheitern. Markiert den Upstream zusätzlich sofort als down, damit
                # nachfolgende Requests den proaktiven Pfad oben nehmen.
                _ollama_healthy[upstream_idx] = False
                fb = _get_fallback_frontier(model)
                if fb and client_cfg.get("frontier_allowed", False):
                    fb_model, fb_base_url, fb_api_key = fb
                    logger.warning(f"[fallback] Ollama-Upstream {upstream_idx} nicht erreichbar ({e}) → {model} auf Frontier {fb_model} umgeleitet")
                    await _maybe_wake_dana(f"Upstream {upstream_idx} nicht erreichbar ({e})")
                    _guard_budget_sync(token_name, is_frontier=True)
                    body["model"] = fb_model
                    return await _proxy_frontier_openai(request, body, fb_model, stream, client_ip, ua, cx_score, fb_base_url, fb_api_key, token_name=token_name)
                _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions",
                                status_code=500, failure_reason="upstream_error",
                                last_user_message=_extract_last_user_message(body))
                raise
            duration_s = time.monotonic() - t0
            if resp.status_code != 200:
                _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions",
                                status_code=resp.status_code, failure_reason="upstream_error",
                                last_user_message=_extract_last_user_message(body))
                return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)

            data  = resp.json()
            ntc   = len(data.get("message", {}).get("tool_calls", []))
            pt    = data.get("prompt_eval_count", 0)
            ct    = data.get("eval_count", 0)
            resp_text = (data.get("message", {}).get("content") or "")
            tps   = _log(pt, ct, duration_s, ntc=ntc, resp_text=resp_text)

            if has_tools and ntc == 0:
                _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions",
                                status_code=200, failure_reason="tool_ignored",
                                last_user_message=prompt_text)
            if _check_tps_anomaly(model, tps, ct):
                await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                              f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")

            await _check_budget_warnings(token_name)
            compat = _native_to_openai(data, model, stream=False, req_id=req_id, req_ts=req_ts)
            headers = {}
            if routed_from:
                headers["X-LLM-Routed-From"] = routed_from
            return Response(content=json.dumps(compat), media_type="application/json", headers=headers)
        finally:
            _req_end(_rid)

    # Streaming
    pt_acc = ct_acc = ntc_acc = 0
    ttft_s = None

    async def sse():
        nonlocal pt_acc, ct_acc, ntc_acc, ttft_s
        first = True
        done_yielded = False
        content_acc = []
        try:
            async with _client.stream("POST", f"{upstream_url}/api/chat", json=native_body) as upstream:
                async for raw in upstream.aiter_lines():
                    if not raw.strip():
                        continue
                    if ttft_s is None:
                        ttft_s = time.monotonic() - t0
                    if first:
                        yield f"data: {json.dumps({'id':req_id,'object':'chat.completion.chunk','created':req_ts,'model':model,'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]})}\n\n".encode()
                        first = False
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if d.get("error"):
                        yield f"data: {json.dumps({'error': d['error']})}\n\n".encode()
                        return
                    if d.get("done"):
                        pt_acc = d.get("prompt_eval_count", pt_acc)
                        ct_acc = d.get("eval_count", ct_acc)
                    if done_yielded:
                        continue
                    chunk  = _native_to_openai(d, model, stream=True, req_id=req_id, req_ts=req_ts)
                    delta  = chunk["choices"][0]["delta"]
                    finish = chunk["choices"][0].get("finish_reason")

                    if "tool_calls" in delta and delta["tool_calls"]:
                        init = {**chunk, "choices": [{"index":0,"delta":{"tool_calls":[]},"finish_reason":None}]}
                        args = {**chunk, "choices": [{"index":0,"delta":{"tool_calls":[]},"finish_reason":finish}]}
                        for tc in delta["tool_calls"]:
                            init["choices"][0]["delta"]["tool_calls"].append(
                                {"index":tc["index"],"id":tc["id"],"type":"function","function":{"name":tc["function"]["name"],"arguments":""}})
                            args["choices"][0]["delta"]["tool_calls"].append(
                                {"index":tc["index"],"function":{"arguments":tc["function"].get("arguments","")}})
                            ntc_acc += 1
                        yield f"data: {json.dumps(init)}\n\n".encode()
                        yield f"data: {json.dumps(args)}\n\n".encode()
                        if finish:
                            done_yielded = True
                        continue

                    if finish:
                        done_yielded = True
                    txt = delta.get("content", "")
                    if txt:
                        content_acc.append(txt)
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"[proxy] stream error: {e}")
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions",
                            status_code=500, failure_reason="upstream_error",
                            last_user_message=prompt_text)
            raise
        finally:
            _req_end(_rid)

        dur = time.monotonic() - t0
        resp_text = "".join(content_acc)
        tps = _log(pt_acc, ct_acc, dur, ttft=ttft_s, ntc=ntc_acc, resp_text=resp_text)
        if has_tools and ntc_acc == 0:
            _db_log_failure(model=model, client_ip=client_ip, token_name=token_name, endpoint="/v1/chat/completions",
                            status_code=200, failure_reason="tool_ignored",
                            last_user_message=prompt_text)
        if _check_tps_anomaly(model, tps, ct_acc):
            await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                          f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")
        await _check_budget_warnings(token_name)

    headers = {"X-LLM-Routed-From": routed_from} if routed_from else {}
    return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)


# ── Embeddings ─────────────────────────────────────────────────────────────────

@app.post("/v1/embeddings", dependencies=[Depends(_guard_ollama_lock)])
@app.post("/api/embeddings", dependencies=[Depends(_guard_ollama_lock)])
async def proxy_embeddings(request: Request):
    path      = request.url.path
    client_ip = _get_client_ip(request)
    token_name = _get_token_name(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    client_cfg = _get_client_config(token_name)
    if not _is_model_in_list(model, client_cfg.get("models", "*")):
        raise HTTPException(status_code=403, detail={"error": f"model {model} not allowed for client {client_ip}"})

    t0        = time.monotonic()
    _rid      = _req_start(model, client_ip, token_name, path)
    
    routed_model, _, target_gpu = _apply_router(model, 0.1)
    if routed_model != model:
        body["model"] = routed_model
        model = routed_model
    upstream_url = _select_upstream(model, target_gpu)
    
    try:
        resp      = await _client.post(f"{upstream_url}{path}", json=body)
    finally:
        _req_end(_rid)
    duration_s = time.monotonic() - t0
    data  = resp.json()
    usage = data.get("usage", {})
    pt    = usage.get("prompt_tokens", data.get("prompt_eval_count", 0))
    _db_insert_request(model=model, prompt_tokens=pt, completion_tokens=0, total_tokens=pt,
                       duration_s=round(duration_s,3), client_ip=client_ip, token_name=token_name, user_agent=ua,
                       endpoint=path, status_code=resp.status_code)
    _add_budget_usage(token_name, pt)
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


# ── Native Ollama: model catalog (registered before the generic passthrough) ──
# Der generische passthrough()-Handler leitet unbekannte Pfade immer an
# UPSTREAM_0 weiter - für /api/tags und /api/show reicht das nicht, weil
# Modelle auch ausschliesslich auf UPSTREAM_1 installiert sein können
# (siehe _model_catalog). Diese beiden Routen müssen vor dem "/{path:path}"-
# Catch-all registriert werden, damit sie überhaupt zum Zug kommen.

@app.get("/api/tags")
async def merged_tags():
    """Merged /api/tags über beide Ollama-Instanzen, dedupliziert nach Modellname.

    Ohne diesen Merge sehen Clients (testmanager, cyberpulse, ...) nur die auf
    UPSTREAM_0 installierten Modelle - alles, was ausschliesslich auf der
    zweiten GPU liegt, fehlte bisher in der über den Proxy sichtbaren Liste.
    """
    async def _fetch(client, upstream):
        try:
            r = await client.get(f"{upstream}/api/tags")
            if r.status_code == 200:
                return r.json().get("models", [])
        except Exception:
            pass
        return []

    merged: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        # Parallel statt sequentiell - ist ein Upstream down, verdoppelt sich
        # sonst die Latenz dieses Endpunkts (voller Timeout je Upstream), was
        # Caller mit knapperem eigenen Timeout (z.B. dashboard/app.py) reissen kann.
        results = await asyncio.gather(*(_fetch(client, u) for u in (OLLAMA_UPSTREAM_0, OLLAMA_UPSTREAM_1)))
    for models in results:
        for m in models:
            name = m.get("name") or m.get("model")
            if name and name not in merged:
                merged[name] = m
    return {"models": list(merged.values())}


@app.get("/v1/models")
async def openai_models():
    """OpenAI-kompatibles Modell-Listing für /v1/models, gespeist aus demselben
    gemergten Katalog wie /api/tags - fehlte bisher, wodurch OpenAI-compat Clients
    (z.B. hermes-agent) beim Verbinden/Modellwechsel ein 404 auf /v1/models sahen.
    """
    tags = await merged_tags()
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m.get("name") or m.get("model"), "object": "model", "created": now, "owned_by": "llmproxy"}
            for m in tags["models"]
        ],
    }


@app.post("/api/show")
async def show_model(request: Request):
    """Wie /api/tags: fragt gezielt den Upstream, der das Modell laut Katalog
    tatsächlich hat, statt blind (wie der alte passthrough()-Fallback) immer
    UPSTREAM_0 zu fragen und dort ein 404 für GPU1-only-Modelle zu kassieren.
    """
    body  = await request.json()
    model = body.get("model") or body.get("name") or ""

    present_on = [idx for idx in (0, 1) if _model_present(idx, model)]
    if len(present_on) == 1:
        upstreams = [_upstream_url(present_on[0])]
    else:
        upstreams = [OLLAMA_UPSTREAM_0, OLLAMA_UPSTREAM_1]

    last_resp = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for upstream in upstreams:
            try:
                r = await client.post(f"{upstream}/api/show", json=body)
                last_resp = r
                if r.status_code == 200:
                    return Response(content=r.content, media_type="application/json", status_code=200)
            except Exception:
                continue

    if last_resp is not None:
        return Response(content=last_resp.content, media_type="application/json", status_code=last_resp.status_code)
    raise HTTPException(status_code=502, detail={"error": f"no upstream reachable for model {model}"})


# ── Status + Health endpoints ─────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        r = await _client.get(f"{OLLAMA_UPSTREAM_0}/api/tags", timeout=3.0)
        ollama_ok = r.status_code == 200
    except Exception:
        ollama_ok = False
    return {"status": "ok", "version": __version__, "ollama_up": ollama_ok,
            "gaming_mode": _gaming_mode, "stop_all": _stop_all,
            "ollama_locked": _ollama_locked, "ollama_lock_auto": _ollama_lock_auto}


@app.get("/health/recent")
async def health_recent():
    return {"requests": _db_get_recent(20)}


@app.get("/status")
async def status():
    recent = _db_get_recent(5)
    # Budget summary
    today = datetime.date.today().isoformat()
    try:
        cur = _db().execute("SELECT token_name, tokens_used FROM budgets WHERE date=?", [today])
        budgets = {r[0]: {"used": r[1], "limit": _get_budget(r[0])} for r in cur.fetchall()}
    except Exception:
        budgets = {}
    return {
        "hw":           _hw_stats,
        "models":       _loaded_models,
        "gaming_mode":  _gaming_mode,
        "stop_all":     _stop_all,
        "ollama_locked": _ollama_locked,
        "ollama_lock_auto": _ollama_lock_auto,
        "recent":       recent,
        "budgets":      budgets,
    }


@app.get("/status/stream")
async def status_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    _sse_subscribers.append(q)

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snapshot = await asyncio.wait_for(q.get(), timeout=10)
                    yield f"data: {json.dumps(snapshot)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/budget")
async def budget_status():
    today = datetime.date.today().isoformat()
    try:
        cur = _db().execute("SELECT token_name, tokens_used FROM budgets WHERE date=?", [today])
        return {
            r[0]: {"used": r[1], "limit": _get_budget(r[0]),
                   "pct": round(r[1] / _get_budget(r[0]) * 100, 1) if _get_budget(r[0]) > 0 else 0}
            for r in cur.fetchall()
        }
    except Exception:
        return {}


@app.get("/clients")
async def clients():
    try:
        cur = _db().execute("SELECT * FROM client_profiles ORDER BY total_requests DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


@app.get("/notifications")
async def notifications_list(limit: int = 50, unread_only: bool = False):
    global _notify_unread
    try:
        if unread_only:
            cur = _db().execute(
                "SELECT * FROM notifications WHERE read_at IS NULL ORDER BY id DESC LIMIT ?", [limit]
            )
        else:
            cur = _db().execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", [limit]
            )
        cols = [d[0] for d in cur.description]
        return {"items": [dict(zip(cols, row)) for row in cur.fetchall()],
                "unread_count": _notify_unread}
    except Exception:
        return {"items": [], "unread_count": 0}


@app.post("/notifications/{notif_id}/read")
async def notification_mark_read(notif_id: int):
    global _notify_unread
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL", [now, notif_id]
        )
        _db().commit()
        # Recount unread from DB
        cur = _db().execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL")
        _notify_unread = cur.fetchone()[0]
        return {"ok": True, "unread_count": _notify_unread}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/notifications/read-all")
async def notifications_read_all():
    global _notify_unread
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", [now])
        _db().commit()
        _notify_unread = 0
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/maintenance/logging")
async def set_logging_config(request: Request):
    _check_admin(request)
    global _logging_cfg
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        _logging_cfg["enabled"] = enabled
        with open(CONFIG_DIR / "logging.yaml", "w") as f:
            yaml.safe_dump(_logging_cfg, f)
        _log_admin_action("logging", "admin", _get_client_ip(request), _get_token_name(request), f"enabled={enabled}")
        return {"ok": True, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/maintenance/logging")
async def get_logging_config(request: Request):
    _check_admin(request)
    return {"enabled": _logging_cfg.get("enabled", True)}


@app.get("/admin/clients")
async def get_clients_config(request: Request):
    _check_admin(request)
    return _client_cfg

@app.post("/admin/clients")
async def set_clients_config(request: Request):
    _check_admin(request)
    global _client_cfg
    try:
        data = await request.json()
        _client_cfg = data
        with open(CONFIG_DIR / "clients.yaml", "w") as f:
            yaml.safe_dump(_client_cfg, f)
        _build_token_map()
        return {"ok": True, "config": _client_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/admin/frontier")
async def get_frontier_config(request: Request):
    _check_admin(request)
    return _frontier_cfg

@app.post("/admin/frontier")
async def set_frontier_config(request: Request):
    _check_admin(request)
    global _frontier_cfg
    try:
        data = await request.json()
        _frontier_cfg = data
        with open(CONFIG_DIR / "frontier.yaml", "w") as f:
            yaml.safe_dump(_frontier_cfg, f)
        return {"ok": True, "config": _frontier_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/admin/fallback")
async def get_fallback_config(request: Request):
    _check_admin(request)
    return {**_fallback_cfg, "ollama_healthy": _ollama_healthy}

@app.post("/admin/fallback")
async def set_fallback_config(request: Request):
    _check_admin(request)
    global _fallback_cfg
    try:
        data = await request.json()
        _fallback_cfg = data
        with open(CONFIG_DIR / "fallback.yaml", "w") as f:
            yaml.safe_dump(_fallback_cfg, f)
        return {"ok": True, "config": _fallback_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── LLM Config (selected model + proxy settings) ────────────────────────

_llm_config_cfg = _load_yaml("llm_config.yaml", {
    "proxy_url": "",
    "api_token": "",
    "selected_model": "",
})


@app.get("/admin/llm_config")
async def get_llm_config(request: Request):
    """GET /admin/llm_config — read the LLM settings (proxy URL, token, selected model)."""
    _check_admin(request)
    return dict(_llm_config_cfg)


@app.post("/admin/llm_config")
async def set_llm_config(request: Request):
    """POST /admin/llm_config — save LLM settings to llm_config.yaml."""
    _check_admin(request)
    global _llm_config_cfg
    try:
        data = await request.json()
        _llm_config_cfg.update(data)
        with open(CONFIG_DIR / "llm_config.yaml", "w") as f:
            yaml.safe_dump(_llm_config_cfg, f)
        return {"ok": True, "config": _llm_config_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/admin/guardrails")
async def get_guardrails_config(request: Request):
    _check_admin(request)
    # Migrate legacy flat structure on-the-fly
    cfg = dict(_guardrails_cfg)
    if "rules" in cfg and "global_rules" not in cfg:
        cfg["global_rules"] = cfg.pop("rules", [])
        cfg.setdefault("client_rules", {})
    return cfg

@app.post("/admin/guardrails")
async def set_guardrails_config(request: Request):
    _check_admin(request)
    global _guardrails_cfg
    try:
        data = await request.json()
        # Normalize: always store as new structure
        if "rules" in data and "global_rules" not in data:
            data["global_rules"] = data.pop("rules", [])
        data.setdefault("global_rules", [])
        data.setdefault("client_rules", {})
        _guardrails_cfg = data
        with open(CONFIG_DIR / "guardrails.yaml", "w") as f:
            yaml.safe_dump(_guardrails_cfg, f)
        return {"ok": True, "config": _guardrails_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/admin/guardrails/simulate")
async def simulate_guardrails(request: Request):
    _check_admin(request)
    try:
        data = await request.json()
        prompt = data.get("prompt", "")
        token_name = data.get("token_name", "")
        # Use custom rules from request if provided, else live config
        custom_rules = data.get("rules")  # list or None
        if custom_rules is not None:
            rules = custom_rules
        else:
            if not _guardrails_cfg.get("enabled", False):
                return {"result": "pass", "note": "Guardrails disabled"}
            rules = _get_effective_rules(token_name)
        modified, violation, action, new_body, triggered_rule = await _apply_rules(
            prompt, token_name, "simulate", {"messages": [{"role": "user", "content": prompt}]},
            rules, record=False
        )
        return {
            "result": action,
            "blocked": action in ("deny", "silent"),
            "violation": violation,
            "triggered_rule": triggered_rule,
            "modified_prompt": new_body.get("messages", [{}])[-1].get("content", prompt) if modified else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/admin/guardrails/simulate-batch")
async def simulate_guardrails_batch(request: Request):
    _check_admin(request)
    try:
        data = await request.json()
        token_name = data.get("token_name", "")
        limit = min(int(data.get("limit", 100)), 500)
        custom_rules = data.get("rules")
        if custom_rules is not None:
            rules = custom_rules
        else:
            rules = _get_effective_rules(token_name)
        # Pull recent prompts from DB
        where = "WHERE prompt_text IS NOT NULL AND prompt_text != ''"
        params: list = []
        if token_name:
            where += " AND token_name = ?"
            params.append(token_name)
        cur = _db().execute(
            f"SELECT id, ts, token_name, client_ip, prompt_text FROM requests "
            f"{where} ORDER BY id DESC LIMIT ?",
            params + [limit]
        )
        rows = cur.fetchall()
        results = []
        blocked_count = 0
        for row in rows:
            rid, ts, tn, ip, prompt = row
            _, violation, action, _, triggered_rule = await _apply_rules(
                prompt or "", tn or "", ip or "",
                {"messages": [{"role": "user", "content": prompt}]},
                rules, record=False
            )
            would_block = action in ("deny", "silent")
            if would_block:
                blocked_count += 1
            results.append({
                "id": rid, "ts": ts, "token_name": tn,
                "blocked": would_block, "action": action,
                "violation": violation,
                "triggered_rule": triggered_rule,
                "prompt_snippet": (prompt or "")[:120],
            })
        return {
            "total": len(results),
            "blocked": blocked_count,
            "pass": len(results) - blocked_count,
            "results": results,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/admin/log")
async def get_log_filtered(request: Request, token_name: str = "", model: str = "",
                           date_from: str = "", date_to: str = "", status: str = "",
                           search: str = "", is_frontier: str = "",
                           limit: int = 50, offset: int = 0):
    _check_admin(request)
    limit = max(1, min(limit, 500))
    where_parts = []
    params: list = []
    if token_name:
        where_parts.append("token_name = ?")
        params.append(token_name)
    if model:
        where_parts.append("model LIKE ?")
        params.append(f"%{model}%")
    if date_from:
        where_parts.append("date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("date <= ?")
        params.append(date_to)
    if status == "ok":
        where_parts.append("(status_code = 200 OR status_code IS NULL)")
    elif status == "error":
        where_parts.append("status_code != 200 AND status_code IS NOT NULL")
    if search:
        where_parts.append("prompt_text LIKE ?")
        params.append(f"%{search}%")
    if is_frontier == "1":
        where_parts.append("is_frontier = 1")
    elif is_frontier == "0":
        where_parts.append("(is_frontier = 0 OR is_frontier IS NULL)")
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    count_cur = _db().execute(f"SELECT COUNT(*) FROM requests {where_sql}", params)
    total = count_cur.fetchone()[0]
    cur = _db().execute(
        f"SELECT id, ts, date, token_name, client_ip, hostname, model, "
        f"prompt_tokens, completion_tokens, total_tokens, tokens_per_second, "
        f"duration_s, ttft_s, status_code, is_frontier, stream, "
        f"complexity_score, prompt_text, response_text "
        f"FROM requests {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    )
    cols = ["id","ts","date","token_name","client_ip","hostname","model",
            "prompt_tokens","completion_tokens","total_tokens","tokens_per_second",
            "duration_s","ttft_s","status_code","is_frontier","stream",
            "complexity_score","prompt_text","response_text"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"total": total, "rows": rows}

@app.get("/admin/audit_config")
async def get_audit_config(request: Request):
    _check_admin(request)
    return _audit_cfg

@app.post("/admin/audit_config")
async def set_audit_config(request: Request):
    _check_admin(request)
    global _audit_cfg
    try:
        data = await request.json()
        _audit_cfg = data
        with open(CONFIG_DIR / "audit.yaml", "w") as f:
            yaml.safe_dump(_audit_cfg, f)
        return {"ok": True, "config": _audit_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/admin/usage_by_client_day")
async def get_usage_by_client_day(request: Request, token_name: str = "",
                                   date_from: str = "", date_to: str = ""):
    _check_admin(request)
    where_parts = []
    params: list = []
    if token_name:
        where_parts.append("token_name = ?")
        params.append(token_name)
    if date_from:
        where_parts.append("date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("date <= ?")
        params.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    cur = _db().execute(f"""
        SELECT token_name, date,
               COUNT(*) as requests,
               SUM(CASE WHEN is_frontier=1 THEN total_tokens ELSE 0 END) as tokens_frontier,
               SUM(CASE WHEN is_frontier=0 OR is_frontier IS NULL THEN total_tokens ELSE 0 END) as tokens_local
        FROM requests {where_sql}
        GROUP BY token_name, date
        ORDER BY date DESC, tokens_frontier DESC
    """, params)
    rows = [dict(zip(["token_name", "date", "requests", "tokens_frontier", "tokens_local"], r))
            for r in cur.fetchall()]

    cur2 = _db().execute(f"""
        SELECT token_name, date, model, COUNT(*) as cnt
        FROM requests {where_sql}
        GROUP BY token_name, date, model
    """, params)
    top_models: dict[tuple, list] = {}
    for tn, dt, model, cnt in cur2.fetchall():
        top_models.setdefault((tn, dt), []).append((model, cnt))
    for row in rows:
        key = (row["token_name"], row["date"])
        models_sorted = sorted(top_models.get(key, []), key=lambda x: -x[1])[:3]
        row["top_models"] = [{"model": m, "count": c} for m, c in models_sorted]

    return {"rows": rows}


# ── Chargeback / cost-allocation API ────────────────────────────────────────────
# Protected via _check_chargeback (accepts X-Chargeback-Token or X-Admin-Token).
# Grouped primarily by token_name (stable client identity); drilldown breaks
# a given token_name down by client_ip. Costs come from config/pricing.yaml —
# models missing a price entry cost 0 and are surfaced via "unpriced_models".

def _chargeback_where(token_name: str, client_ip: str, date_from: str, date_to: str):
    where_parts = []
    params: list = []
    if token_name:
        where_parts.append("token_name = ?")
        params.append(token_name)
    if client_ip:
        where_parts.append("client_ip = ?")
        params.append(client_ip)
    if date_from:
        where_parts.append("date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("date <= ?")
        params.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return where_sql, params

def _chargeback_summary_rows(token_name: str, date_from: str, date_to: str, group_by: str):
    where_sql, params = _chargeback_where(token_name, "", date_from, date_to)
    date_expr = {"month": "substr(date, 1, 7)", "none": "''"}.get(group_by, "date")
    cur = _db().execute(f"""
        SELECT token_name, {date_expr} as bucket, model, is_frontier,
               COUNT(*) as requests,
               SUM(prompt_tokens) as prompt_tokens,
               SUM(completion_tokens) as completion_tokens,
               SUM(total_tokens) as total_tokens
        FROM requests {where_sql}
        GROUP BY token_name, bucket, model, is_frontier
    """, params)
    agg: dict[tuple, dict] = {}
    unpriced = set()
    for tn, bucket, model, is_frontier, requests, ptok, ctok, ttok in cur.fetchall():
        row = agg.setdefault((tn, bucket), {"token_name": tn, "date": bucket, "requests": 0,
                                             "requests_local": 0, "requests_frontier": 0,
                                             "prompt_tokens": 0, "completion_tokens": 0,
                                             "total_tokens": 0, "tokens_local": 0, "tokens_frontier": 0,
                                             "cost_usd": 0.0, "cost_eur": 0.0})
        row["requests"] += requests
        row["prompt_tokens"] += ptok or 0
        row["completion_tokens"] += ctok or 0
        row["total_tokens"] += ttok or 0
        usd, eur = _model_cost_usd_eur(model, ptok or 0, ctok or 0)
        row["cost_usd"] += usd
        row["cost_eur"] += eur
        if is_frontier:
            row["requests_frontier"] += requests
            row["tokens_frontier"] += ttok or 0
        else:
            row["requests_local"] += requests
            row["tokens_local"] += ttok or 0
        if model not in _pricing_cfg.get("models", {}):
            unpriced.add(model)
    rows = list(agg.values())
    for row in rows:
        row["cost_usd"] = round(row["cost_usd"], 6)
        row["cost_eur"] = round(row["cost_eur"], 6)
        if group_by == "none":
            row.pop("date", None)
    rows.sort(key=lambda r: r.get("date", ""), reverse=True)
    return rows, sorted(unpriced)

@app.get("/admin/chargeback/summary")
async def chargeback_summary(request: Request, token_name: str = "",
                              date_from: str = "", date_to: str = "",
                              group_by: str = "day"):
    _check_chargeback(request)
    if group_by not in ("day", "month", "none"):
        group_by = "day"
    rows, unpriced = _chargeback_summary_rows(token_name, date_from, date_to, group_by)
    return {"rows": rows, "fx": _pricing_cfg.get("fx", {}), "unpriced_models": unpriced}

def _chargeback_drilldown_rows(token_name: str, date_from: str, date_to: str):
    where_sql, params = _chargeback_where(token_name, "", date_from, date_to)
    cur = _db().execute(f"""
        SELECT client_ip, model, is_frontier,
               COUNT(*) as requests,
               SUM(prompt_tokens) as prompt_tokens,
               SUM(completion_tokens) as completion_tokens,
               SUM(total_tokens) as total_tokens,
               MAX(ts) as last_seen
        FROM requests {where_sql}
        GROUP BY client_ip, model, is_frontier
    """, params)
    agg: dict[str, dict] = {}
    unpriced = set()
    for ip, model, is_frontier, requests, ptok, ctok, ttok, last_seen in cur.fetchall():
        row = agg.setdefault(ip, {"client_ip": ip, "requests": 0, "prompt_tokens": 0,
                                   "completion_tokens": 0, "total_tokens": 0,
                                   "tokens_local": 0, "tokens_frontier": 0,
                                   "cost_usd": 0.0, "cost_eur": 0.0, "last_seen": last_seen})
        row["requests"] += requests
        row["prompt_tokens"] += ptok or 0
        row["completion_tokens"] += ctok or 0
        row["total_tokens"] += ttok or 0
        usd, eur = _model_cost_usd_eur(model, ptok or 0, ctok or 0)
        row["cost_usd"] += usd
        row["cost_eur"] += eur
        if is_frontier:
            row["tokens_frontier"] += ttok or 0
        else:
            row["tokens_local"] += ttok or 0
        if last_seen and (not row["last_seen"] or last_seen > row["last_seen"]):
            row["last_seen"] = last_seen
        if model not in _pricing_cfg.get("models", {}):
            unpriced.add(model)
    rows = sorted(agg.values(), key=lambda r: -r["cost_usd"])
    for row in rows:
        row["cost_usd"] = round(row["cost_usd"], 6)
        row["cost_eur"] = round(row["cost_eur"], 6)
    return rows, sorted(unpriced)

@app.get("/admin/chargeback/drilldown")
async def chargeback_drilldown(request: Request, token_name: str = "",
                                date_from: str = "", date_to: str = ""):
    _check_chargeback(request)
    if not token_name:
        raise HTTPException(status_code=400, detail="token_name is required")
    rows, unpriced = _chargeback_drilldown_rows(token_name, date_from, date_to)
    return {"token_name": token_name, "rows": rows,
            "fx": _pricing_cfg.get("fx", {}), "unpriced_models": unpriced}

def _chargeback_detail_rows(token_name: str, client_ip: str, model: str,
                             date_from: str, date_to: str, limit: int, offset: int):
    where_parts = []
    params: list = []
    if token_name:
        where_parts.append("token_name = ?")
        params.append(token_name)
    if client_ip:
        where_parts.append("client_ip = ?")
        params.append(client_ip)
    if model:
        where_parts.append("model LIKE ?")
        params.append(f"%{model}%")
    if date_from:
        where_parts.append("date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("date <= ?")
        params.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    total = _db().execute(f"SELECT COUNT(*) FROM requests {where_sql}", params).fetchone()[0]
    cur = _db().execute(
        f"SELECT id, ts, date, token_name, client_ip, model, "
        f"prompt_tokens, completion_tokens, total_tokens, is_frontier "
        f"FROM requests {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    )
    cols = ["id", "ts", "date", "token_name", "client_ip", "model",
            "prompt_tokens", "completion_tokens", "total_tokens", "is_frontier"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    unpriced = set()
    for row in rows:
        usd, eur = _model_cost_usd_eur(row["model"], row["prompt_tokens"], row["completion_tokens"])
        row["cost_usd"] = round(usd, 6)
        row["cost_eur"] = round(eur, 6)
        if row["model"] not in _pricing_cfg.get("models", {}):
            unpriced.add(row["model"])
    return total, rows, sorted(unpriced)

@app.get("/admin/chargeback/detail")
async def chargeback_detail(request: Request, token_name: str = "", client_ip: str = "",
                             model: str = "", date_from: str = "", date_to: str = "",
                             limit: int = 50, offset: int = 0):
    _check_chargeback(request)
    limit = max(1, min(limit, 500))
    total, rows, unpriced = _chargeback_detail_rows(token_name, client_ip, model,
                                                      date_from, date_to, limit, offset)
    return {"total": total, "rows": rows, "fx": _pricing_cfg.get("fx", {}),
            "unpriced_models": unpriced}

@app.get("/admin/chargeback/pricing")
async def get_chargeback_pricing(request: Request):
    _check_chargeback(request)
    return _pricing_cfg

@app.post("/admin/chargeback/pricing")
async def set_chargeback_pricing(request: Request):
    # Mutating, so full admin only (unlike the read-only chargeback endpoints
    # above) -- config/pricing.yaml is seeded once and never overwritten by
    # deploys afterwards (same as clients.yaml etc.), so this is the only way
    # to update live prices without hand-editing the file on the host.
    _check_admin(request)
    global _pricing_cfg
    try:
        data = await request.json()
        _pricing_cfg = data
        with open(CONFIG_DIR / "pricing.yaml", "w") as f:
            yaml.safe_dump(_pricing_cfg, f)
        return {"ok": True, "config": _pricing_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

_CHARGEBACK_EXPORT_ROW_CAP = 5000  # homelab-scale safety cap on detail exports

@app.get("/admin/chargeback/export")
async def chargeback_export(request: Request, view: str = "summary", format: str = "csv",
                             token_name: str = "", client_ip: str = "", model: str = "",
                             date_from: str = "", date_to: str = "", group_by: str = "day"):
    _check_chargeback(request)
    if view not in ("summary", "drilldown", "detail"):
        raise HTTPException(status_code=400, detail="invalid view")
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="invalid format")

    if view == "summary":
        if group_by not in ("day", "month", "none"):
            group_by = "day"
        rows, _unpriced = _chargeback_summary_rows(token_name, date_from, date_to, group_by)
        fieldnames = ["token_name", "requests", "requests_local", "requests_frontier",
                      "prompt_tokens", "completion_tokens", "total_tokens",
                      "tokens_local", "tokens_frontier", "cost_usd", "cost_eur"]
        if group_by != "none":
            fieldnames.insert(1, "date")
    elif view == "drilldown":
        if not token_name:
            raise HTTPException(status_code=400, detail="token_name is required for drilldown export")
        rows, _unpriced = _chargeback_drilldown_rows(token_name, date_from, date_to)
        fieldnames = ["client_ip", "requests", "prompt_tokens", "completion_tokens",
                      "total_tokens", "tokens_local", "tokens_frontier", "cost_usd", "cost_eur", "last_seen"]
    else:  # detail
        _total, rows, _unpriced = _chargeback_detail_rows(
            token_name, client_ip, model, date_from, date_to,
            limit=_CHARGEBACK_EXPORT_ROW_CAP, offset=0)
        fieldnames = ["id", "ts", "date", "token_name", "client_ip", "model",
                      "prompt_tokens", "completion_tokens", "total_tokens", "is_frontier",
                      "cost_usd", "cost_eur"]

    filename = f"chargeback_{view}_{datetime.date.today().isoformat()}.{format}"

    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response(content=buf.getvalue(), media_type="text/csv",
                         headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    else:
        import io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = view
        ws.append(fieldnames)
        for row in rows:
            ws.append([row.get(f, "") for f in fieldnames])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(content=buf.getvalue(),
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/admin/audit/run")
async def run_audit(request: Request):
    _check_admin(request)
    if not _audit_cfg.get("enabled", True):
        return {"ok": False, "error": "Audit ist deaktiviert (config/audit.yaml)"}
    data = await request.json()
    token_name = data.get("token_name", "")
    date_from = data.get("date_from", "")
    date_to = data.get("date_to", "")

    where_parts = ["prompt_text IS NOT NULL AND prompt_text != ''"]
    params: list = []
    if token_name:
        where_parts.append("token_name = ?")
        params.append(token_name)
    if date_from:
        where_parts.append("date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("date <= ?")
        params.append(date_to)
    where_sql = "WHERE " + " AND ".join(where_parts)

    max_requests = int(_audit_cfg.get("max_requests", 300))
    max_chars = int(_audit_cfg.get("max_chars_per_text", 600))

    cur = _db().execute(f"""
        SELECT date, token_name, model, is_frontier, total_tokens, prompt_text, response_text
        FROM requests {where_sql}
        ORDER BY total_tokens DESC LIMIT ?
    """, params + [max_requests])
    rows = cur.fetchall()
    if not rows:
        return {"ok": False, "error": "Keine Requests mit gespeichertem Prompt-Text im gewählten Zeitraum."}

    def _build(include_response: bool, n: int) -> str:
        lines = []
        for r in rows[:n]:
            date, tn, model, is_fr, tt, pt, rt = r
            pt = (pt or "")[:max_chars]
            entry = f"[{date}] client={tn} model={model} frontier={bool(is_fr)} tokens={tt}\nPROMPT: {pt}"
            if include_response and rt:
                entry += f"\nRESPONSE: {rt[:max_chars]}"
            lines.append(entry)
        return "\n---\n".join(lines)

    sample_text = _build(True, len(rows))
    est_tokens = len(sample_text) // 4
    if est_tokens > 12000:
        sample_text = _build(False, len(rows))
        est_tokens = len(sample_text) // 4
    n = len(rows)
    while est_tokens > 12000 and n > 20:
        n = int(n * 0.7)
        sample_text = _build(False, n)
        est_tokens = len(sample_text) // 4

    prompt = f"""Du bist ein Kostenoptimierungs- und Sicherheits-Auditor für einen LLM-Proxy.
Analysiere die folgenden {n} echten Request-Beispiele (sortiert nach Tokenverbrauch, {"gekürzt" if n < len(rows) else "vollständig"}) und gib konkrete Empfehlungen in drei Abschnitten:

1. TOKEN-EINSPARUNG: Welche Prompt-Muster/Modelle verbrauchen unnötig viele Tokens? Wo könnte gekürzt, gecacht oder gebatcht werden?
2. DLP/GUARDRAILS: Welche sensiblen Muster (Secrets, PII, interne Daten) tauchen auf, die aktuell nicht gefiltert werden?
3. FALLBACK-KANDIDATEN: Welche Requests (insbesondere frontier=True) wirken einfach genug für ein kleineres/lokales Modell?

Antworte auf Deutsch, in Stichpunkten pro Abschnitt.

--- REQUESTS ---
{sample_text}
"""

    model = _audit_cfg.get("model", "qwen3:8b")
    upstream_idx = _audit_cfg.get("upstream", 0)
    upstream_url = OLLAMA_UPSTREAM_0 if upstream_idx == 0 else OLLAMA_UPSTREAM_1
    if model not in _model_catalog.get(upstream_idx, set()):
        found = False
        for idx, names in _model_catalog.items():
            if model in names:
                upstream_url = OLLAMA_UPSTREAM_0 if idx == 0 else OLLAMA_UPSTREAM_1
                found = True
                break
        if not found:
            return {"ok": False, "error": f"Audit-Modell '{model}' auf keinem Ollama-Upstream gefunden."}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)) as client:
            r = await client.post(f"{upstream_url}/api/chat", json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            })
            r.raise_for_status()
            report = r.json().get("message", {}).get("content", "")
    except Exception as e:
        return {"ok": False, "error": f"Audit-Modell nicht erreichbar: {e}"}

    return {"ok": True, "report": report, "sample_size": n, "total_matching": len(rows),
            "model": model, "est_prompt_tokens": est_tokens}

@app.get("/admin/bans")
async def get_bans(request: Request):
    _check_admin(request)
    import time
    now = time.time()
    active_bans = {k: v for k, v in _fail2ban_cfg.get("bans", {}).items() if v > now}
    return {"bans": active_bans}

@app.post("/admin/unban")
async def unban_token(request: Request, token_name: str):
    _check_admin(request)
    if token_name in _fail2ban_cfg.get("bans", {}):
        del _fail2ban_cfg["bans"][token_name]
        with open(CONFIG_DIR / "fail2ban.yaml", "w") as f:
            yaml.safe_dump(_fail2ban_cfg, f)
        # Clear strikes
        if hasattr(_record_violation, "strikes") and token_name in _record_violation.strikes:
            del _record_violation.strikes[token_name]
        _log_admin_action("unban", "admin", _get_client_ip(request), _get_token_name(request), f"unbanned {token_name}")
        return {"ok": True, "unbanned": token_name}
    return {"ok": False, "error": "Token not banned"}

@app.get("/admin/actions")
async def get_admin_actions(request: Request, limit: int = 100):
    """Audit-Trail für /maintenance/*-Aktionen — wer/was/wann hat den Proxy
    gesperrt, VRAM geleert, etc. `source` unterscheidet 'admin' (per
    X-Admin-Token) von 'auto' (ComfyUI-Queue-Poller)."""
    _check_admin(request)
    limit = max(1, min(limit, 500))
    try:
        cur = _db().execute(
            "SELECT ts, action, source, client_ip, detail FROM admin_actions "
            "ORDER BY id DESC LIMIT ?", [limit]
        )
        rows = [dict(zip(("ts", "action", "source", "client_ip", "detail"), r)) for r in cur.fetchall()]
        return {"actions": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/maintenance/cleanup")
async def maintenance_cleanup(request: Request, full: bool = False):
    """Delete old log rows and reclaim disk space.

    full=False (default): delete rows older than LOG_RETENTION_DAYS.
    full=True: purge ALL rows from failures/notifications/requests.
    """
    _check_admin(request)
    global _notify_unread
    try:
        size_before = DB_PATH.stat().st_size
        db = _db()
        deleted = {}
        if full:
            cur = db.execute("DELETE FROM failures")
            deleted["failures"] = cur.rowcount
            cur = db.execute("DELETE FROM notifications")
            deleted["notifications"] = cur.rowcount
            cur = db.execute("DELETE FROM requests")
            deleted["requests"] = cur.rowcount
            cur = db.execute("DELETE FROM guardrail_events")
            deleted["guardrail_events"] = cur.rowcount
        else:
            cur = db.execute("DELETE FROM failures WHERE ts < datetime('now', ?)",
                              [f"-{LOG_RETENTION_DAYS.get('failures', 7)} days"])
            deleted["failures"] = cur.rowcount
            cur = db.execute("DELETE FROM notifications WHERE ts < datetime('now', ?)",
                              [f"-{LOG_RETENTION_DAYS.get('notifications', 7)} days"])
            deleted["notifications"] = cur.rowcount
            cur = db.execute("DELETE FROM requests WHERE date < date('now', ?)",
                              [f"-{LOG_RETENTION_DAYS.get('requests', 30)} days"])
            deleted["requests"] = cur.rowcount
            cur = db.execute("DELETE FROM guardrail_events WHERE ts < datetime('now', ?)",
                              [f"-{LOG_RETENTION_DAYS.get('guardrail_events', 7)} days"])
            deleted["guardrail_events"] = cur.rowcount
        db.commit()
        db.execute("VACUUM")

        cur = db.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL")
        _notify_unread = cur.fetchone()[0]

        size_after = DB_PATH.stat().st_size
        _log_admin_action("cleanup", "admin", _get_client_ip(request), _get_token_name(request), f"full={full} deleted={deleted}")
        return {
            "ok": True,
            "deleted": deleted,
            "size_before_mb": round(size_before / 1024 / 1024, 2),
            "size_after_mb": round(size_after / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/maintenance/strip-prompts")
async def maintenance_strip_prompts(request: Request, older_than_days: int = 7):
    """Löscht prompt_text/response_text aus älteren Einträgen (nach Auswertung).

    Spart Speicher ohne Performance-Metriken zu verlieren.
    """
    _check_admin(request)
    cutoff = (datetime.date.today() - datetime.timedelta(days=older_than_days)).isoformat()
    try:
        db = _db()
        cur = db.execute(
            "UPDATE requests SET prompt_text=NULL, response_text=NULL "
            "WHERE date < ? AND (prompt_text IS NOT NULL OR response_text IS NOT NULL)",
            [cutoff]
        )
        db.commit()
        db.execute("VACUUM")
        _log_admin_action("strip-prompts", "admin", _get_client_ip(request), _get_token_name(request), f"stripped_rows={cur.rowcount} cutoff={cutoff}")
        return {"ok": True, "stripped_rows": cur.rowcount, "cutoff": cutoff}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _evict_loaded_models(tag: str = "evict-models") -> list[str]:
    """Force-evict all currently loaded models from VRAM right now
    (same keep_alive=0 trick as the idle-eviction loop, but on demand)."""
    evicted = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for m in list(_loaded_models):
            name = m.get("name", "")
            if not name:
                continue
            upstream = OLLAMA_UPSTREAM_1 if m.get("upstream") == 1 else OLLAMA_UPSTREAM_0
            try:
                await client.post(f"{upstream}/api/generate", json={"model": name, "keep_alive": 0})
                evicted.append(name)
            except Exception as e:
                logger.error(f"[{tag}] failed to evict {name}: {e}")
    return evicted


@app.post("/maintenance/evict-models")
async def maintenance_evict_models(request: Request):
    _check_admin(request)
    evicted = await _evict_loaded_models("evict-models")
    _log_admin_action("evict-models", "admin", _get_client_ip(request), _get_token_name(request), f"evicted={evicted}")
    return {"ok": True, "evicted": evicted}


@app.post("/maintenance/force-purge")
async def maintenance_force_purge(request: Request):
    """Zweistufiger Hard-Reset: erst soft evict (keep_alive=0), dann killall llama-server.
    Nützlich wenn eine laufende Inference nicht reagiert.
    Ollama startet llama-server beim nächsten Request automatisch neu."""
    _check_admin(request)
    import asyncio

    # Schritt 1: Soft evict via keep_alive=0
    evicted: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for m in list(_loaded_models):
            name = m.get("name", "")
            if not name:
                continue
            upstream = OLLAMA_UPSTREAM_1 if m.get("upstream") == 1 else OLLAMA_UPSTREAM_0
            try:
                await client.post(f"{upstream}/api/generate", json={"model": name, "keep_alive": 0})
                evicted.append(name)
            except Exception:
                pass

    # Kurze Pause damit Ollama aufräumen kann
    await asyncio.sleep(2)

    # Schritt 2: Alle verbleibenden llama-server Prozesse hart killen
    killed_pids: list[int] = []
    try:
        result = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True, text=True)
        pids = [int(p) for p in result.stdout.split() if p.strip()]
        for pid in pids:
            try:
                import os, signal
                os.kill(pid, signal.SIGKILL)
                killed_pids.append(pid)
            except ProcessLookupError:
                pass
    except Exception as e:
        logger.error(f"[force-purge] kill error: {e}")

    logger.info(f"[force-purge] soft-evicted={evicted} hard-killed={killed_pids}")
    await _notify("force_purge",
                  f"🔴 Force-Purge: {len(killed_pids)} llama-server(s) gekillt",
                  f"Soft evicted: {evicted}\nHard killed PIDs: {killed_pids}")
    _log_admin_action("force-purge", "admin", _get_client_ip(request), _get_token_name(request), f"evicted={evicted} killed_pids={killed_pids}")
    return {"ok": True, "soft_evicted": evicted, "hard_killed_pids": killed_pids}


@app.post("/maintenance/stop-all")
async def maintenance_stop_all(request: Request):
    """Sperrt alle Inference-Requests (503) und killt laufende Prozesse.
    Mit /maintenance/resume wieder aufheben."""
    _check_admin(request)
    global _stop_all
    _stop_all = True
    # Force-Purge: laufende Inferenz abbrechen
    evicted: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for m in list(_loaded_models):
            name = m.get("name", "")
            if not name:
                continue
            upstream = OLLAMA_UPSTREAM_1 if m.get("upstream") == 1 else OLLAMA_UPSTREAM_0
            try:
                await client.post(f"{upstream}/api/generate", json={"model": name, "keep_alive": 0})
                evicted.append(name)
            except Exception:
                pass
    import asyncio as _aio
    await _aio.sleep(2)
    killed: list[int] = []
    try:
        res = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True, text=True)
        for pid in [int(p) for p in res.stdout.split() if p.strip()]:
            try:
                import os as _os, signal as _sig
                _os.kill(pid, _sig.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                pass
    except Exception:
        pass
    logger.info(f"[stop-all] aktiv — evicted={evicted} killed={killed}")
    await _notify("stop_all", "🛑 Stop-All aktiviert", f"Evicted: {evicted}\nKilled PIDs: {killed}")
    _log_admin_action("stop-all", "admin", _get_client_ip(request), _get_token_name(request), f"evicted={evicted} killed_pids={killed}")
    return {"ok": True, "stop_all": True, "evicted": evicted, "killed_pids": killed}


@app.post("/maintenance/resume")
async def maintenance_resume(request: Request):
    """Hebt den Stop-All-Modus auf — Requests werden wieder durchgelassen."""
    _check_admin(request)
    global _stop_all
    _stop_all = False
    logger.info("[stop-all] deaktiviert — Inference wieder erlaubt")
    await _notify("resume", "✅ Stop-All aufgehoben", "Inference wieder aktiv.")
    _log_admin_action("resume", "admin", _get_client_ip(request), _get_token_name(request))
    return {"ok": True, "stop_all": False}


def _save_ollama_lock():
    with open(CONFIG_DIR / "ollama_lock.yaml", "w") as f:
        yaml.safe_dump({"locked": _ollama_locked}, f)


async def _set_ollama_lock(locked: bool, auto: bool, reason: str, client_ip: str = "") -> dict:
    """Shared core for manual (/maintenance/ollama-lock|-unlock, admin-checked) and
    automatic (ComfyUI-queue poller, no auth needed - it's an internal call, not
    an HTTP request) toggling. `auto=True` marks a lock the poller itself set, so
    it knows it's allowed to release it again once the queue empties - see
    _ollama_lock_auto and _poll_comfyui_queue()."""
    global _ollama_locked, _ollama_lock_auto
    _ollama_locked = locked
    _ollama_lock_auto = auto
    _save_ollama_lock()
    source = "auto" if auto else "admin"
    if locked:
        evicted = await _evict_loaded_models("ollama-lock")
        logger.info(f"[ollama-lock] aktiv ({reason}) — evicted={evicted}")
        await _notify("ollama_lock", "🔒 Ollama-Lock aktiviert", f"{reason}\nEvicted: {evicted}")
        _log_admin_action("ollama-lock", source, client_ip, f"{reason} evicted={evicted}")
        return {"ok": True, "ollama_locked": True, "evicted": evicted}
    logger.info(f"[ollama-lock] deaktiviert ({reason})")
    await _notify("ollama_unlock", "🔓 Ollama-Lock aufgehoben", reason)
    _log_admin_action("ollama-unlock", source, client_ip, reason)
    return {"ok": True, "ollama_locked": False}


@app.post("/maintenance/ollama-lock")
async def maintenance_ollama_lock(request: Request):
    """Sperrt gezielt den Zugriff auf lokale Ollama-Modelle (native Endpunkte hart,
    /v1/chat/completions mit Frontier-Fallback falls konfiguriert) und entlädt alle
    geladenen Modelle, damit die GPUs sofort frei sind (z.B. für ComfyUI). Der Zustand
    wird in config/ollama_lock.yaml persistiert und übersteht damit einen Neustart."""
    _check_admin(request)
    return await _set_ollama_lock(True, auto=False, reason="Manuell gesperrt (Admin)", client_ip=_get_client_ip(request), token_name=_get_token_name(request))


@app.post("/maintenance/ollama-unlock")
async def maintenance_ollama_unlock(request: Request):
    """Hebt den Ollama-Lock wieder auf — lokale Modelle sind wieder nutzbar."""
    _check_admin(request)
    return await _set_ollama_lock(False, auto=False, reason="Manuell entsperrt (Admin)", client_ip=_get_client_ip(request), token_name=_get_token_name(request))


@app.get("/admin/wol_config")
async def get_wol_config(request: Request):
    _check_admin(request)
    return _wol_cfg

@app.post("/admin/wol_config")
async def set_wol_config(request: Request):
    _check_admin(request)
    global _wol_cfg
    try:
        data = await request.json()
        _wol_cfg = data
        with open(CONFIG_DIR / "wol.yaml", "w") as f:
            yaml.safe_dump(_wol_cfg, f)
        return {"ok": True, "config": _wol_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/maintenance/wake-dana")
async def maintenance_wake_dana(request: Request):
    """Sendet sofort (ohne Cooldown) ein Wake-on-LAN Magic Packet an dana."""
    _check_admin(request)
    global _last_wol_sent
    ok = await asyncio.to_thread(_send_wol_packet)
    _last_wol_sent = time.monotonic()
    _log_admin_action("wake-dana", "admin", client_ip=_get_client_ip(request), detail="manuell ausgelöst")
    return {"ok": ok}


class GamingOverrideRequestProxy(BaseModel):
    override: str

@app.post("/maintenance/gaming_override")
async def set_gaming_override(request: Request, req: GamingOverrideRequestProxy):
    _check_admin(request)
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"{GPU_AGENT_URL}/gaming_override", json={"override": req.override})
        r.raise_for_status()
        _log_admin_action("gaming_override", "admin", _get_client_ip(request), _get_token_name(request), f"override={req.override}")
        return r.json()


@app.get("/status/gpu")
async def status_gpu():
    """Zeigt pro GPU: geladenes Modell, aktive Requests (von wem), VRAM, Auslastung."""
    # Beispiel zum Anpassen an die eigene Hardware: {0: "RTX 4090 (GPU 0)", 1: "RTX 3060 (GPU 1)"}
    GPU_NAMES = {0: "GPU 0", 1: "GPU 1"}
    gpus_hw   = {g["index"]: g for g in _hw_stats.get("gpus", [])}

    result = {}
    for idx in (0, 1):
        hw = gpus_hw.get(idx, {})
        models_on_gpu = [m for m in _loaded_models if m.get("upstream") == idx]
        active_on_gpu = [
            {"model": v["model"], "client": v["client_ip"],
             "elapsed_s": round(time.time() - v["t_start"], 1)}
            for v in _active_requests.values() if v.get("upstream") == idx
        ]
        result[f"gpu{idx}"] = {
            "name":        GPU_NAMES.get(idx, f"GPU {idx}"),
            "vram_used_mb": hw.get("ram_used", 0),
            "vram_total_mb": hw.get("ram_total", 0),
            "vram_pct":    round(hw.get("ram_used", 0) / hw.get("ram_total", 1) * 100, 1) if hw.get("ram_total") else 0,
            "load_pct":    hw.get("load_pct", 0),
            "temp_c":      hw.get("temp_c", 0),
            "models":      [{"name": m["name"], "vram_mb": round(m.get("size_vram", 0) / 1024 / 1024)} for m in models_on_gpu],
            "active_requests": active_on_gpu,
            "stop_all":    _stop_all,
            "ollama_locked": _ollama_locked,
        }
    result["stop_all"] = _stop_all
    result["ollama_locked"] = _ollama_locked
    return result


@app.get("/status/gpu/html", response_class=__import__("fastapi").responses.HTMLResponse)
async def status_gpu_html(request: Request, token: str = ""):
    """Einfache HTML-Ansicht des GPU-Status mit Stop-All-Buttons.

    Verlangt den Admin-Token wie /admin/* und /maintenance/* - diese Seite
    bettet den echten Token in ihre Action-Buttons ein (fetch() aus dem Browser
    kann sonst keine custom Header beim Navigieren mitschicken), darf also
    selbst nicht unauthentifiziert aufrufbar sein, sonst leakt der Token an
    jeden im LAN. Per Query-Param statt Header, weil das eine reine
    Browser-Navigations-URL ist: /status/gpu/html?token=<admin-token>.
    """
    if token != _ADMIN_TOKEN and request.headers.get("X-Admin-Token", "") != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden — add ?token=<admin-token>")
    data = await status_gpu()
    admin_hdr = f"{{'X-Admin-Token':'{_ADMIN_TOKEN}'}}"
    stop_banner = (
        '<div style="background:#ff4455;color:#fff;padding:12px 20px;font-weight:bold;font-size:15px;">'
        '🛑 STOP-ALL AKTIV — alle Inference-Requests werden blockiert'
        f' &nbsp;<a href="#" onclick="fetch(\'/maintenance/resume\',{{method:\'POST\',headers:{admin_hdr}}}).then(()=>location.reload())" '
        'style="color:#fff;text-decoration:underline;">Aufheben</a></div>'
        if _stop_all else ""
    )
    rows = ""
    for idx in (0, 1):
        g   = data[f"gpu{idx}"]
        col = "#005a9e" if idx == 0 else "#cc0000"
        models_html = "".join(
            f'<li style="font-size:13px;"><b>{m["name"]}</b> ({m["vram_mb"]} MB VRAM)</li>'
            for m in g["models"]
        ) or "<li style='color:#888;font-size:13px;'>— kein Modell geladen —</li>"
        reqs_html = "".join(
            f'<li style="font-size:13px;">{r["client"]} → <b>{r["model"]}</b> ({r["elapsed_s"]}s)</li>'
            for r in g["active_requests"]
        ) or "<li style='color:#888;font-size:13px;'>— keine aktiven Requests —</li>"
        rows += f"""
        <div style="border:2px solid {col};border-radius:8px;padding:18px;margin-bottom:16px;">
          <h2 style="color:{col};margin:0 0 8px 0;">{g["name"]}</h2>
          <div style="display:flex;gap:24px;font-size:13px;margin-bottom:10px;">
            <span>VRAM: <b>{g["vram_used_mb"]} / {g["vram_total_mb"]} MB</b> ({g["vram_pct"]}%)</span>
            <span>Load: <b>{g["load_pct"]}%</b></span>
            <span>Temp: <b>{g["temp_c"]}°C</b></span>
          </div>
          <div style="display:flex;gap:32px;">
            <div><b>Modelle:</b><ul style="margin:4px 0 0 16px;padding:0;">{models_html}</ul></div>
            <div><b>Aktive Requests:</b><ul style="margin:4px 0 0 16px;padding:0;">{reqs_html}</ul></div>
          </div>
        </div>"""
    stop_btn = (
        f'<button onclick="fetch(\'/maintenance/stop-all\',{{method:\'POST\',headers:{admin_hdr}}}).then(()=>location.reload())" '
        'style="background:#ff4455;color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:14px;">🛑 Stop All</button>'
        if not _stop_all else
        f'<button onclick="fetch(\'/maintenance/resume\',{{method:\'POST\',headers:{admin_hdr}}}).then(()=>location.reload())" '
        'style="background:#2e7d32;color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:14px;">✅ Resume</button>'
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>GPU Status — llmproxy</title>
<meta http-equiv="refresh" content="5">
</head><body style="font-family:sans-serif;background:#f5f5f5;padding:0;margin:0;">
{stop_banner}
<div style="padding:20px;">
<h1 style="font-size:20px;margin:0 0 16px 0;">GPU Status
  <span style="font-size:13px;color:#888;font-weight:normal;">— aktualisiert alle 5s</span>
  &nbsp;&nbsp;{stop_btn}
</h1>
{rows}
</div></body></html>"""


@app.post("/debug/notify")
async def debug_notify(request: Request):
    body  = await request.json()
    event = body.get("event", "test")
    await _notify(event, f"🔔 Test: {event}", f"llmproxy debug notification: {event}")
    return {"sent": True, "event": event}


# ── Passthrough ────────────────────────────────────────────────────────────────

@app.get("/api/ps")
async def proxy_api_ps():
    return {"models": _loaded_models}

@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "HEAD"])
async def passthrough(request: Request, path: str):
    # Catch-all für alles, was keine dedizierte Route hat (/api/pull, /api/delete,
    # /api/create, /api/version, ...). Immer UPSTREAM_0 - das ist für diese eher
    # seltenen/administrativen Aufrufe unkritisch. /api/tags und /api/show haben
    # eigene, modell-katalog-bewusste Routen weiter oben und laufen NICHT hier durch.
    url = f"{OLLAMA_UPSTREAM_0}/{path}"
    logger.info(f"[proxy] {request.method} /{path} -> UPSTREAM_0")

    async def _gen():
        async with _client.stream(
            method=request.method, url=url,
            headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "origin", "referer")},
            content=await request.body(),
            params=dict(request.query_params),
        ) as upstream:
            yield upstream.status_code
            yield dict(upstream.headers)
            async for chunk in upstream.aiter_bytes():
                yield chunk

    gen = _gen()
    status_code = await gen.__anext__()
    headers     = await gen.__anext__()
    headers = {k: v for k, v in headers.items()
               if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
    return StreamingResponse(gen, status_code=status_code, headers=headers)


# ── ComfyUI Proxy ──────────────────────────────────────────────────────────────

comfy_app    = FastAPI()
_comfy_client = httpx.AsyncClient(base_url=COMFYUI_UPSTREAM, timeout=600.0)


@comfy_app.post("/prompt")
async def proxy_comfy_prompt(request: Request):
    client_ip = _get_client_ip(request)
    t0   = time.monotonic()
    body = await request.json()
    resp = await _comfy_client.post("/prompt", json=body)
    _db_insert_request(model="comfyui_render", prompt_tokens=1, completion_tokens=0, total_tokens=1,
                       duration_s=round(time.monotonic()-t0,3), client_ip=client_ip,
                       endpoint="/prompt", status_code=resp.status_code)
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


@comfy_app.websocket("/ws")
async def comfy_ws_proxy(websocket: WebSocket):
    await websocket.accept()
    query = str(websocket.query_params)
    upstream_url = f"ws://{COMFYUI_HOST}:8188/ws?{query}" if query else f"ws://{COMFYUI_HOST}:8188/ws"
    headers = {k: v for k, v in websocket.headers.items()
               if k.lower() not in ("host", "origin", "referer")}
    try:
        async with websockets.connect(upstream_url, extra_headers=headers) as ws:
            async def c2u():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.receive":
                            if "text" in msg:
                                await ws.send(msg["text"])
                            elif "bytes" in msg:
                                await ws.send(msg["bytes"])
                        elif msg["type"] == "websocket.disconnect":
                            break
                except Exception:
                    pass

            async def u2c():
                try:
                    while True:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except Exception:
                    pass

            await asyncio.gather(c2u(), u2c())
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@comfy_app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "HEAD"])
async def comfy_passthrough(request: Request, path: str):
    async def _gen():
        async with _comfy_client.stream(
            method=request.method, url=f"/{path}",
            headers={k: v for k, v in request.headers.items()
                     if k.lower() not in ("host", "origin", "referer")},
            content=await request.body(),
            params=dict(request.query_params),
        ) as upstream:
            yield upstream.status_code
            yield dict(upstream.headers)
            async for chunk in upstream.aiter_bytes():
                yield chunk

    gen = _gen()
    status_code = await gen.__anext__()
    headers     = await gen.__anext__()
    headers = {k: v for k, v in headers.items()
               if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
    return StreamingResponse(gen, status_code=status_code, headers=headers)


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_proxies():
    _db_init()

    # Load baselines into memory
    try:
        for row in _db().execute("SELECT model, median_tps FROM model_baselines"):
            _model_baselines[row[0]] = row[1]
    except Exception:
        pass

    # Restore unread notification count from DB
    global _notify_unread
    try:
        cur = _db().execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL")
        _notify_unread = cur.fetchone()[0]
    except Exception:
        pass

    asyncio.create_task(_poll_gaming_mode())
    asyncio.create_task(_poll_comfyui_queue())
    asyncio.create_task(_poll_hardware())
    asyncio.create_task(_poll_loaded_models())
    asyncio.create_task(_poll_model_catalog())
    asyncio.create_task(_sse_broadcast_loop())
    asyncio.create_task(_update_baselines())
    asyncio.create_task(_evict_idle_models())
    asyncio.create_task(_update_client_profiles())
    asyncio.create_task(_checkpoint_wal())

    ssl_kwargs = {}
    if _SSL_CERT.exists() and _SSL_KEY.exists():
        ssl_kwargs = {"ssl_certfile": str(_SSL_CERT), "ssl_keyfile": str(_SSL_KEY)}
    else:
        logger.warning(f"  TLS cert not found at {_SSL_CERT} — serving plain HTTP")

    cfg_ollama = uvicorn.Config(app,       host="0.0.0.0", port=LISTEN_PORT,  log_level="warning", **ssl_kwargs)
    cfg_comfy  = uvicorn.Config(comfy_app, host="0.0.0.0", port=COMFYUI_PORT, log_level="warning", **ssl_kwargs)

    logger.info("llmproxy starting:")
    logger.info(f"  Ollama  :{LISTEN_PORT}  → {OLLAMA_UPSTREAM_0} & {OLLAMA_UPSTREAM_1} (tls={'yes' if ssl_kwargs else 'no'})")
    logger.info(f"  ComfyUI :{COMFYUI_PORT} → {COMFYUI_UPSTREAM}")
    logger.info(f"  DB      : {DB_PATH}")

    await asyncio.gather(
        uvicorn.Server(cfg_ollama).serve(),
        uvicorn.Server(cfg_comfy).serve(),
    )


# ── Admin control endpoints ────────────────────────────────────────────────────

def _load_or_create_admin_token() -> str:
    p = Path("/opt/llmproxy/.admin_token")
    if p.exists():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    p.chmod(0o600)
    logger.warning(f"[admin] no admin token found — generated a new one at {p}")
    return tok

_ADMIN_TOKEN = _load_or_create_admin_token()

def _load_or_create_chargeback_token() -> str:
    p = Path("/opt/llmproxy/.chargeback_token")
    if p.exists():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    p.chmod(0o600)
    logger.warning(f"[chargeback] no chargeback token found — generated a new one at {p}")
    return tok

_CHARGEBACK_TOKEN = _load_or_create_chargeback_token()

def _load_or_create_secret_key() -> bytes:
    """Fernet key protecting the `secrets` table at rest (client bearer
    tokens preserved-not-rotated, frontier provider api_keys) -- same
    generate-once-and-persist pattern as the admin/chargeback tokens."""
    p = Path("/opt/llmproxy/.secret_key")
    if p.exists():
        return p.read_bytes().strip()
    key = Fernet.generate_key()
    # Create with 0600 atomically -- unlike write_bytes()+chmod(), there's no
    # window where the key material is readable at default (world-readable)
    # permissions before the mode gets tightened. This key protects every
    # value in the `secrets` table, so it's worth the extra care even though
    # the existing .admin_token/.chargeback_token use the simpler pattern.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    logger.warning(f"[secrets] no secret key found — generated a new one at {p}")
    return key

_FERNET = Fernet(_load_or_create_secret_key())

def _encrypt_secret(value: str) -> bytes:
    return _FERNET.encrypt(value.encode("utf-8"))

def _decrypt_secret(blob: bytes) -> str:
    return _FERNET.decrypt(blob).decode("utf-8")

def _get_secret(name: str) -> str | None:
    row = _db().execute("SELECT value_encrypted FROM secrets WHERE name = ?", (name,)).fetchone()
    return _decrypt_secret(row[0]) if row else None

def _set_secret(name: str, value: str):
    con = _db()
    con.execute(
        "INSERT INTO secrets (name, value_encrypted, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value_encrypted=excluded.value_encrypted, updated_at=excluded.updated_at",
        (name, _encrypt_secret(value), datetime.datetime.utcnow().isoformat())
    )
    con.commit()

GAMING_SERVICES  = ["ollama", "comfyui", "comfyui2", "open-webui"]
RESTORE_SERVICES = ["ollama", "comfyui", "comfyui2"]

def _authenticate(request: Request, roles: set[str]):
    """Unified RBAC check for /admin/* and chargeback endpoints. Preferred
    path: `Authorization: Bearer <key_id>.<secret>` verified against the
    `api_keys` table (bcrypt), enforcing the caller's role is in `roles`.

    Deprecated fallback (kept only until scripts/migrate_auth.py has run
    and every existing automated client -- dashboard, hermes-agent, any
    external BI tool -- has been reissued a real API key; remove this
    block afterwards, see the approved auth-overhaul plan's Sequencing
    section): the old shared X-Admin-Token/X-Chargeback-Token headers
    still work, mapped to the 'admin'/'finance' roles respectively, so
    nothing already deployed breaks mid-rollout.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and "." in auth_header[7:]:
        key_id, _, secret = auth_header[7:].partition(".")
        row = _db().execute(
            "SELECT secret_hash, role FROM api_keys WHERE key_id = ? AND disabled = 0", (key_id,)
        ).fetchone()
        if row and bcrypt.checkpw(secret.encode("utf-8"), row[0].encode("utf-8")) and row[1] in roles:
            _db().execute("UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                           (datetime.datetime.utcnow().isoformat(), key_id))
            _db().commit()
            return

    if request.headers.get("X-Admin-Token", "") == _ADMIN_TOKEN and "admin" in roles:
        return
    if request.headers.get("X-Chargeback-Token", "") == _CHARGEBACK_TOKEN and "finance" in roles:
        return

    raise HTTPException(status_code=403, detail="Forbidden")

def _check_admin(request: Request):
    _authenticate(request, {"admin"})

def _check_chargeback(request: Request):
    _authenticate(request, {"admin", "finance"})

# ── User accounts + API keys (RBAC) ─────────────────────────────────────────────
# Unauthenticated by design (it's the login endpoint) -- same network-trust
# posture as every other /admin/* route today (LAN reachability, no rate
# limiting), not a new exposure.
@app.post("/admin/auth/verify")
async def auth_verify(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    row = _db().execute(
        "SELECT password_hash, role, disabled FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not row or row[2] or not bcrypt.checkpw(password.encode("utf-8"), row[0].encode("utf-8")):
        return {"ok": False}
    _db().execute("UPDATE users SET last_login_at = ? WHERE username = ?",
                   (datetime.datetime.utcnow().isoformat(), username))
    _db().commit()
    return {"ok": True, "role": row[1]}

_ROLES = ("admin", "finance", "viewer")

@app.get("/admin/users")
async def list_users(request: Request):
    _check_admin(request)
    cols = ["id", "username", "role", "created_at", "last_login_at", "disabled"]
    rows = _db().execute(f"SELECT {', '.join(cols)} FROM users ORDER BY username").fetchall()
    return {"users": [dict(zip(cols, r)) for r in rows]}

@app.post("/admin/users")
async def create_user(request: Request):
    _check_admin(request)
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "viewer"
    if not username or not password or role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"username, password and role (one of {_ROLES}) are required")
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    try:
        _db().execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, pw_hash, role, datetime.datetime.utcnow().isoformat())
        )
        _db().commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="username already exists")
    return {"ok": True, "username": username, "role": role}

@app.post("/admin/users/{username}")
async def update_user(username: str, request: Request):
    """Change role/password and/or enable/disable an existing user."""
    _check_admin(request)
    data = await request.json()
    sets, params = [], []
    if "role" in data:
        if data["role"] not in _ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {_ROLES}")
        sets.append("role = ?"); params.append(data["role"])
    if data.get("password"):
        sets.append("password_hash = ?")
        params.append(bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8"))
    if "disabled" in data:
        sets.append("disabled = ?"); params.append(1 if data["disabled"] else 0)
    if not sets:
        raise HTTPException(status_code=400, detail="nothing to update")
    params.append(username)
    _db().execute(f"UPDATE users SET {', '.join(sets)} WHERE username = ?", params)
    _db().commit()
    return {"ok": True}

@app.delete("/admin/users/{username}")
async def delete_user(username: str, request: Request):
    """Soft-delete: disables the account rather than removing the row, so
    admin_actions/audit history referencing it stays meaningful."""
    _check_admin(request)
    _db().execute("UPDATE users SET disabled = 1 WHERE username = ?", (username,))
    _db().commit()
    return {"ok": True}

@app.get("/admin/api_keys")
async def list_api_keys(request: Request):
    _check_admin(request)
    cols = ["key_id", "owner_type", "owner_name", "role", "created_at", "last_used_at", "disabled"]
    rows = _db().execute(f"SELECT {', '.join(cols)} FROM api_keys ORDER BY owner_name").fetchall()
    return {"api_keys": [dict(zip(cols, r)) for r in rows]}

@app.post("/admin/api_keys")
async def create_api_key(request: Request):
    """Returns the plaintext secret once -- it is never recoverable again
    (only the bcrypt hash is stored), same one-time-reveal convention as
    the .admin_token file."""
    _check_admin(request)
    data = await request.json()
    owner_type = data.get("owner_type") or "service"
    owner_name = (data.get("owner_name") or "").strip()
    role = data.get("role") or "finance"
    if owner_type not in ("user", "service") or not owner_name or role not in ("admin", "finance"):
        raise HTTPException(status_code=400,
                             detail="owner_type ('user'|'service'), owner_name and role ('admin'|'finance') are required")
    key_id = "kc_" + secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    secret_hash = bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    _db().execute(
        "INSERT INTO api_keys (key_id, secret_hash, owner_type, owner_name, role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (key_id, secret_hash, owner_type, owner_name, role, datetime.datetime.utcnow().isoformat())
    )
    _db().commit()
    return {"ok": True, "key_id": key_id, "bearer": f"{key_id}.{secret}",
            "note": "This secret is shown once and cannot be recovered — store it now."}

@app.delete("/admin/api_keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request):
    _check_admin(request)
    _db().execute("UPDATE api_keys SET disabled = 1 WHERE key_id = ?", (key_id,))
    _db().commit()
    return {"ok": True}

def _svc(action: str, service: str) -> int:
    r = subprocess.run(["sudo", "systemctl", action, service],
                       capture_output=True, text=True)
    return r.returncode

@app.post("/admin/gaming/on")
async def gaming_on(request: Request):
    """Stop GPU services for gaming."""
    _check_admin(request)
    results = {}
    for svc in GAMING_SERVICES:
        results[svc] = "stopped" if _svc("stop", svc) == 0 else "error"
    await _notify("gaming_mode", "🎮 Gaming Mode AN", "GPU-Services gestoppt für Gaming.")
    return {"mode": "gaming", "services": results}

@app.post("/admin/gaming/off")
async def gaming_off(request: Request):
    """Restart GPU services after gaming."""
    _check_admin(request)
    results = {}
    for svc in RESTORE_SERVICES:
        results[svc] = "started" if _svc("start", svc) == 0 else "error"
    await _notify("gaming_mode", "🤖 Gaming Mode AUS", "GPU-Services neu gestartet.")
    return {"mode": "normal", "services": results}

@app.get("/admin/status")
async def admin_status(request: Request):
    """Service status overview."""
    _check_admin(request)
    all_svcs = GAMING_SERVICES + ["llmproxy"]
    status = {}
    for svc in all_svcs:
        r = subprocess.run(["systemctl", "is-active", svc],
                           capture_output=True, text=True)
        status[svc] = r.stdout.strip()
    return {"services": status}

@app.post("/admin/service/{action}/{service}")
async def service_control(action: str, service: str, request: Request):
    """start/stop/restart any listed service."""
    _check_admin(request)
    allowed_actions  = {"start", "stop", "restart"}
    allowed_services = set(GAMING_SERVICES) | {"parkinson-studio", "llmproxy"}
    if action not in allowed_actions or service not in allowed_services:
        raise HTTPException(status_code=400, detail="Not allowed")
    rc = _svc(action, service)
    return {"service": service, "action": action, "ok": rc == 0}

@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    """Shut down this host."""
    _check_admin(request)
    await _notify("shutdown", "⚡ Host fährt herunter", "Shutdown via admin API ausgelöst.")
    asyncio.get_event_loop().call_later(2, lambda: subprocess.run(["sudo", "shutdown", "-h", "now"]))
    return {"shutting_down": True}

@app.post("/admin/reboot")
async def admin_reboot(request: Request):
    """Reboot this host."""
    _check_admin(request)
    await _notify("reboot", "🔄 Host startet neu", "Reboot via admin API ausgelöst.")
    asyncio.get_event_loop().call_later(2, lambda: subprocess.run(["sudo", "reboot"]))
    return {"rebooting": True}


if __name__ == "__main__":
    try:
        asyncio.run(run_proxies())
    except KeyboardInterrupt:
        pass

