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

__version__ = "2.4.0"

import asyncio
import datetime
import json
import logging
import os
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
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("llmproxy")

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

DB_PATH           = Path("/root/.llmproxy.db")
CONFIG_DIR        = Path("/opt/llmproxy")

DEFAULT_OLLAMA_OPTIONS = {"num_gpu": -1, "num_ctx": 65536}

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
_frontier_cfg     = _load_yaml("frontier.yaml",      {"enabled": False, "providers": {}})
_routing_cfg      = _load_yaml("routing.yaml",        {"routes": []})
_eviction_cfg     = _load_yaml("eviction.yaml",        {"eviction_timeout_min": 15, "vram_threshold_pct": 80, "never_evict": []})
_notify_cfg       = _load_yaml("notifications.yaml",  {"events": {}})
_logging_cfg      = _load_yaml("logging.yaml",        {"enabled": True})

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
            client_ip           TEXT,
            date                TEXT,
            tokens_used         INTEGER DEFAULT 0,
            PRIMARY KEY (client_ip, date)
        );
        CREATE TABLE IF NOT EXISTS client_profiles (
            client_ip           TEXT PRIMARY KEY,
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
        CREATE INDEX IF NOT EXISTS idx_date     ON requests(date);
        CREATE INDEX IF NOT EXISTS idx_model    ON requests(model);
        CREATE INDEX IF NOT EXISTS idx_ip       ON requests(client_ip);
        CREATE INDEX IF NOT EXISTS idx_ts       ON requests(ts);
        CREATE INDEX IF NOT EXISTS idx_notif_ts ON notifications(ts);
    """)
    for col_def in ["hostname TEXT", "prompt_text TEXT", "response_text TEXT"]:
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

def _db_log_failure(*, model: str, client_ip: str, endpoint: str,
                    status_code: int, failure_reason: str, last_user_message: str = ""):
    if not _logging_cfg.get("enabled", True):
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _db().execute(
            "INSERT INTO failures (ts, model, client_ip, endpoint, status_code, failure_reason, last_user_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [now, model, client_ip, endpoint, status_code, failure_reason, last_user_message[:500]]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[db] failure log error: {e}")

def _db_get_recent(n: int = 20) -> list[dict]:
    try:
        cur = _db().execute(
            "SELECT ts, model, client_ip, endpoint, prompt_tokens, completion_tokens, "
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

# ── Global state ───────────────────────────────────────────────────────────────

_gaming_mode:     bool = False
_gaming_override: str  = "auto"
_stop_all:        bool = False   # Maintenance-Modus: blockiert alle Inference-Requests
_hw_stats:        dict = {}
_loaded_models:   list = []
# Modell-Katalog pro Upstream (0/1), gefüllt von _poll_model_catalog() via /api/tags.
# Nicht zu verwechseln mit _loaded_models (aktuell im VRAM) - das hier ist "installiert,
# aber ggf. gerade nicht geladen". Wird gebraucht, damit das Routing weiss, welches
# Modell wo überhaupt existiert (z.B. Vision-Modelle, die nur auf einer GPU liegen).
_model_catalog:   dict[int, set] = {0: set(), 1: set()}
_sse_subscribers: list[asyncio.Queue] = []
_model_baselines: dict = {}   # model → median_tps (in-memory cache)
_load_shed_queue: asyncio.Queue | None = None   # set in run_proxies()
_active_requests: dict = {}   # req_id → {model, client_ip, endpoint, t_start}
_req_counter:     int  = 0


def _req_start(model: str, client_ip: str, endpoint: str, upstream_idx: int | None = None) -> int:
    global _req_counter
    _req_counter += 1
    rid = _req_counter
    _active_requests[rid] = {"model": model, "client_ip": client_ip,
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
    global _model_catalog
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            for idx, upstream in ((0, OLLAMA_UPSTREAM_0), (1, OLLAMA_UPSTREAM_1)):
                try:
                    r = await client.get(f"{upstream}/api/tags")
                    if r.status_code == 200:
                        names = {m.get("name") or m.get("model") for m in r.json().get("models", [])}
                        _model_catalog[idx] = {n for n in names if n}
                except Exception:
                    pass  # letzten bekannten Katalog für diese GPU behalten
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
                "active":       [{"model": v["model"], "client_ip": v["client_ip"],
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
                SELECT client_ip,
                       AVG(complexity_score) as avg_cx,
                       MAX(model) as top_model,
                       CAST(strftime('%H', ts) AS INTEGER) as hour,
                       AVG(CAST(has_tools AS REAL)) as tool_rate,
                       AVG(num_messages) as avg_msgs,
                       COUNT(*) as total
                FROM requests
                WHERE date >= date('now', '-30 days')
                GROUP BY client_ip
            """)
            now = datetime.datetime.now().isoformat(timespec="seconds")
            for row in cur.fetchall():
                ip, avg_cx, top_model, hour, tool_rate, avg_msgs, total = row
                _db().execute(
                    "INSERT OR REPLACE INTO client_profiles VALUES (?,?,?,?,?,?,?,?)",
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

def _get_budget(client_ip: str) -> int:
    cfg = _get_client_config(client_ip)
    return cfg.get("limit", 5_000_000)

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


def _check_budget(client_ip: str) -> tuple[bool, int, int]:
    """Returns (allowed, used, limit)."""
    today = datetime.date.today().isoformat()
    limit = _get_budget(client_ip)
    try:
        cur = _db().execute(
            "SELECT tokens_used FROM budgets WHERE client_ip=? AND date=?", [client_ip, today]
        )
        row = cur.fetchone()
        used = row[0] if row else 0
    except Exception:
        used = 0
    return used < limit, used, limit


def _add_budget_usage(client_ip: str, tokens: int):
    today = datetime.date.today().isoformat()
    try:
        _db().execute(
            "INSERT INTO budgets (client_ip, date, tokens_used) VALUES (?,?,?) "
            "ON CONFLICT(client_ip, date) DO UPDATE SET tokens_used = tokens_used + ?",
            [client_ip, today, tokens, tokens]
        )
        _db().commit()
    except Exception:
        pass


async def _check_budget_warnings(client_ip: str):
    today = datetime.date.today().isoformat()
    limit = _get_budget(client_ip)
    try:
        cur = _db().execute(
            "SELECT tokens_used FROM budgets WHERE client_ip=? AND date=?", [client_ip, today]
        )
        row = cur.fetchone()
        used = row[0] if row else 0
        pct = used / limit * 100 if limit > 0 else 0
        if 80 <= pct < 100:
            await _notify("budget_warning", f"⚠️ Budget-Warnung {client_ip}",
                          f"{client_ip}: {pct:.0f}% des Tages-Budgets verbraucht ({used:,}/{limit:,} Tokens)")
        elif pct >= 100:
            await _notify("budget_exceeded", f"🚫 Budget überschritten {client_ip}",
                          f"{client_ip}: Tages-Budget erschöpft ({limit:,} Tokens). Reset um Mitternacht.", "high")
    except Exception:
        pass


def _check_tps_anomaly(model: str, tps: float | None) -> bool:
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


async def _guard_stop_all(request: Request):
    if _stop_all:
        raise HTTPException(
            status_code=503,
            detail={"error": "llmproxy is in stop-all maintenance mode — all inference blocked",
                    "stop_all": True, "retry_after": 60},
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


async def _guard_budget(request: Request):
    client_ip = _get_client_ip(request)
    if _is_blocked(client_ip):
        raise HTTPException(status_code=403, detail={"error": "client is blocked"})
    allowed, used, limit = _check_budget(client_ip)
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

# ── Native Ollama: /api/chat and /api/generate ─────────────────────────────────

@app.post("/api/chat",     dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_budget)])
@app.post("/api/generate", dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_budget)])
async def proxy_native(request: Request):
    path      = request.url.path
    client_ip = _get_client_ip(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    stream    = body.get("stream", True)
    num_ctx   = body.get("options", {}).get("num_ctx")
    cx_score  = _compute_complexity(body)
    pred_dur  = _predict_duration(model, cx_score)
    model, _, target_gpu = _apply_router(model, cx_score)
    upstream_url = _select_upstream(model, target_gpu)
    upstream_idx = 1 if upstream_url == OLLAMA_UPSTREAM_1 else 0

    client_cfg = _get_client_config(client_ip)
    if not _is_model_in_list(model, client_cfg.get("models", "*")):
        raise HTTPException(status_code=403, detail={"error": f"model {model} not allowed for client {client_ip}"})

    logger.info(f"[proxy] {request.method} {path} model={model} cx={cx_score} from={client_ip}")
    _rid = _req_start(model, client_ip, path, upstream_idx)
    t0 = time.monotonic()

    prompt_text = (_extract_last_user_message(body) or body.get("prompt", ""))[:2000]
    hostname = _resolve_hostname(client_ip)

    if not stream:
        try:
            try:
                resp = await _client.post(f"{upstream_url}{path}", json=body)
            except Exception:
                _db_log_failure(model=model, client_ip=client_ip, endpoint=path,
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
                               tokens_per_second=tps, client_ip=client_ip, user_agent=ua,
                               endpoint=path, stream=0, num_ctx=num_ctx,
                               complexity_score=cx_score, predicted_duration_s=pred_dur,
                               status_code=resp.status_code,
                               hostname=hostname, prompt_text=prompt_text, response_text=resp_text)
            _add_budget_usage(client_ip, pt+ct)
            await _check_budget_warnings(client_ip)
            if _check_tps_anomaly(model, tps):
                _db_log_failure(model=model, client_ip=client_ip, endpoint=path,
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
        except Exception:
            _db_log_failure(model=model, client_ip=client_ip, endpoint=path,
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
                           client_ip=client_ip, user_agent=ua, endpoint=path, stream=1,
                           num_ctx=num_ctx, complexity_score=cx_score, predicted_duration_s=pred_dur,
                           hostname=hostname, prompt_text=prompt_text, response_text=resp_text)
        _add_budget_usage(client_ip, pt+ct)
        await _check_budget_warnings(client_ip)
        if _check_tps_anomaly(model, tps):
            _db_log_failure(model=model, client_ip=client_ip, endpoint=path,
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

    native: dict = {
        "model":   body.get("model", ""),
        "messages": messages,
        "stream":  body.get("stream", False),
        "options": {**DEFAULT_OLLAMA_OPTIONS, **body.get("options", {})},
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
                "arguments": args if isinstance(args, str) else json.dumps(args),
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


async def _proxy_frontier_openai(request, body, model, stream, client_ip, ua, cx_score, base_url, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    prompt_text = _extract_last_user_message(body)[:2000]
    hostname = _resolve_hostname(client_ip)
    pred_dur = _predict_duration(model, cx_score)
    num_messages = len(body.get("messages", []))
    has_tools = bool(body.get("tools"))
    
    t0 = time.monotonic()
    _rid = _req_start(model, client_ip, "/v1/chat/completions (frontier)")
    
    def _log(pt, ct, dur, status=200, ttft=None, ntc=0, resp_text=""):
        tps = round(ct / dur, 1) if dur > 0 and ct > 0 else None
        _db_insert_request(
            model=model, prompt_tokens=pt, completion_tokens=ct, total_tokens=pt+ct,
            duration_s=round(dur, 3), tokens_per_second=tps, ttft_s=round(ttft, 3) if ttft else None,
            client_ip=client_ip, user_agent=ua, endpoint="/v1/chat/completions",
            stream=int(stream), num_messages=num_messages, has_tools=int(has_tools),
            num_tool_calls=ntc, num_ctx=None, status_code=status,
            complexity_score=cx_score, predicted_duration_s=pred_dur,
            routed_from=None, hostname=hostname, prompt_text=prompt_text, response_text=resp_text[:2000]
        )
        _add_budget_usage(client_ip, pt+ct)
        return tps

    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    if not stream:
        try:
            resp = await _client.post(endpoint, json=body, headers=headers)
        except Exception:
            _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions", status_code=500, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
            _req_end(_rid)
            raise
        dur = time.monotonic() - t0
        _req_end(_rid)
        if resp.status_code != 200:
            _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions", status_code=resp.status_code, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
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
        await _check_budget_warnings(client_ip)
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
            _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions", status_code=500, failure_reason="frontier_upstream_error", last_user_message=prompt_text)
            raise
        finally:
            _req_end(_rid)
        dur = time.monotonic() - t0
        resp_text = "".join(content_acc)
        _log(pt_acc, ct_acc, dur, ttft=ttft_s, ntc=ntc_acc, resp_text=resp_text)
        await _check_budget_warnings(client_ip)
        
    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/v1/chat/completions", dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_budget)])
async def proxy_openai(request: Request):
    client_ip = _get_client_ip(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    stream    = body.get("stream", False)
    cx_score  = _compute_complexity(body)

    client_cfg = _get_client_config(client_ip)
    frontier = _get_frontier_target(model)
    
    if frontier:
        if not client_cfg.get("frontier_allowed", False):
            raise HTTPException(status_code=403, detail={"error": "Frontier models are not allowed for this client"})
        allowed_frontier = client_cfg.get("frontier_models", "*")
        if not _is_model_in_list(model, allowed_frontier):
            raise HTTPException(status_code=403, detail={"error": f"frontier model {model} not allowed for client {client_ip}"})
        return await _proxy_frontier_openai(request, body, model, stream, client_ip, ua, cx_score, frontier[0], frontier[1])
    else:
        allowed_local = client_cfg.get("models", "*")
        if not _is_model_in_list(model, allowed_local):
            raise HTTPException(status_code=403, detail={"error": f"local model {model} not allowed for client {client_ip}"})

    # Auto-router
    routed_model, routed_from, target_gpu = _apply_router(model, cx_score)
    if routed_from:
        body["model"] = routed_model
        model = routed_model
    upstream_url = _select_upstream(model, target_gpu)
    upstream_idx = 1 if upstream_url == OLLAMA_UPSTREAM_1 else 0

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
    _rid   = _req_start(model, client_ip, "/v1/chat/completions", upstream_idx)

    def _log(pt, ct, dur, status=200, ttft=None, ntc=0, resp_text=""):
        tps = round(ct / dur, 1) if dur > 0 and ct > 0 else None
        _db_insert_request(
            model=model, prompt_tokens=pt, completion_tokens=ct, total_tokens=pt+ct,
            duration_s=round(dur, 3), tokens_per_second=tps,
            ttft_s=round(ttft, 3) if ttft else None,
            client_ip=client_ip, user_agent=ua, endpoint="/v1/chat/completions",
            stream=int(stream), num_messages=num_messages, has_tools=int(has_tools),
            num_tool_calls=ntc, num_ctx=num_ctx, status_code=status,
            complexity_score=cx_score, predicted_duration_s=pred_dur,
            routed_from=routed_from,
            hostname=hostname, prompt_text=prompt_text, response_text=resp_text[:2000],
        )
        _add_budget_usage(client_ip, pt+ct)
        return tps

    if not stream:
        try:
            try:
                resp = await _client.post(f"{upstream_url}/api/chat", json=native_body)
            except Exception:
                _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions",
                                status_code=500, failure_reason="upstream_error",
                                last_user_message=_extract_last_user_message(body))
                raise
            duration_s = time.monotonic() - t0
            if resp.status_code != 200:
                _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions",
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
                _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions",
                                status_code=200, failure_reason="tool_ignored",
                                last_user_message=prompt_text)
            if _check_tps_anomaly(model, tps):
                await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                              f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")

            await _check_budget_warnings(client_ip)
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
                        yield f"data: {{\"error\":\"{d['error']}\"}}\n\n".encode()
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
            _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions",
                            status_code=500, failure_reason="upstream_error",
                            last_user_message=prompt_text)
            raise
        finally:
            _req_end(_rid)

        dur = time.monotonic() - t0
        resp_text = "".join(content_acc)
        tps = _log(pt_acc, ct_acc, dur, ttft=ttft_s, ntc=ntc_acc, resp_text=resp_text)
        if has_tools and ntc_acc == 0:
            _db_log_failure(model=model, client_ip=client_ip, endpoint="/v1/chat/completions",
                            status_code=200, failure_reason="tool_ignored",
                            last_user_message=prompt_text)
        if _check_tps_anomaly(model, tps):
            await _notify("tps_anomaly", f"⚠️ TPS-Anomalie: {model}",
                          f"{model}: {tps} tps (Baseline: {_model_baselines.get(model):.1f})")
        await _check_budget_warnings(client_ip)

    headers = {"X-LLM-Routed-From": routed_from} if routed_from else {}
    return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)


# ── Embeddings ─────────────────────────────────────────────────────────────────

@app.post("/v1/embeddings")
@app.post("/api/embeddings")
async def proxy_embeddings(request: Request):
    path      = request.url.path
    client_ip = _get_client_ip(request)
    ua        = request.headers.get("user-agent", "")
    body      = await request.json()
    model     = body.get("model", "unknown")
    client_cfg = _get_client_config(client_ip)
    if not _is_model_in_list(model, client_cfg.get("models", "*")):
        raise HTTPException(status_code=403, detail={"error": f"model {model} not allowed for client {client_ip}"})

    t0        = time.monotonic()
    _rid      = _req_start(model, client_ip, path)
    
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
                       duration_s=round(duration_s,3), client_ip=client_ip, user_agent=ua,
                       endpoint=path, status_code=resp.status_code)
    _add_budget_usage(client_ip, pt)
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
    merged: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for upstream in (OLLAMA_UPSTREAM_0, OLLAMA_UPSTREAM_1):
            try:
                r = await client.get(f"{upstream}/api/tags")
                if r.status_code == 200:
                    for m in r.json().get("models", []):
                        name = m.get("name") or m.get("model")
                        if name and name not in merged:
                            merged[name] = m
            except Exception:
                continue
    return {"models": list(merged.values())}


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
            "gaming_mode": _gaming_mode, "stop_all": _stop_all}


@app.get("/health/recent")
async def health_recent():
    return {"requests": _db_get_recent(20)}


@app.get("/status")
async def status():
    recent = _db_get_recent(5)
    # Budget summary
    today = datetime.date.today().isoformat()
    try:
        cur = _db().execute("SELECT client_ip, tokens_used FROM budgets WHERE date=?", [today])
        budgets = {r[0]: {"used": r[1], "limit": _get_budget(r[0])} for r in cur.fetchall()}
    except Exception:
        budgets = {}
    return {
        "hw":           _hw_stats,
        "models":       _loaded_models,
        "gaming_mode":  _gaming_mode,
        "stop_all":     _stop_all,
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
        cur = _db().execute("SELECT client_ip, tokens_used FROM budgets WHERE date=?", [today])
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
    global _logging_cfg
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        _logging_cfg["enabled"] = enabled
        with open(CONFIG_DIR / "logging.yaml", "w") as f:
            yaml.safe_dump(_logging_cfg, f)
        return {"ok": True, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/maintenance/logging")
async def get_logging_config():
    return {"enabled": _logging_cfg.get("enabled", True)}


@app.get("/admin/clients")
async def get_clients_config():
    return _client_cfg

@app.post("/admin/clients")
async def set_clients_config(request: Request):
    global _client_cfg
    try:
        data = await request.json()
        _client_cfg = data
        with open(CONFIG_DIR / "clients.yaml", "w") as f:
            yaml.safe_dump(_client_cfg, f)
        return {"ok": True, "config": _client_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/admin/frontier")
async def get_frontier_config():
    return _frontier_cfg

@app.post("/admin/frontier")
async def set_frontier_config(request: Request):
    global _frontier_cfg
    try:
        data = await request.json()
        _frontier_cfg = data
        with open(CONFIG_DIR / "frontier.yaml", "w") as f:
            yaml.safe_dump(_frontier_cfg, f)
        return {"ok": True, "config": _frontier_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}



@app.post("/maintenance/cleanup")
async def maintenance_cleanup(full: bool = False):
    """Delete old log rows and reclaim disk space.

    full=False (default): delete rows older than LOG_RETENTION_DAYS.
    full=True: purge ALL rows from failures/notifications/requests.
    """
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
        else:
            cur = db.execute("DELETE FROM failures WHERE ts < datetime('now', ?)",
                              [f"-{LOG_RETENTION_DAYS['failures']} days"])
            deleted["failures"] = cur.rowcount
            cur = db.execute("DELETE FROM notifications WHERE ts < datetime('now', ?)",
                              [f"-{LOG_RETENTION_DAYS['notifications']} days"])
            deleted["notifications"] = cur.rowcount
            cur = db.execute("DELETE FROM requests WHERE date < date('now', ?)",
                              [f"-{LOG_RETENTION_DAYS['requests']} days"])
            deleted["requests"] = cur.rowcount
        db.commit()
        db.execute("VACUUM")

        cur = db.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL")
        _notify_unread = cur.fetchone()[0]

        size_after = DB_PATH.stat().st_size
        return {
            "ok": True,
            "deleted": deleted,
            "size_before_mb": round(size_before / 1024 / 1024, 2),
            "size_after_mb": round(size_after / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/maintenance/strip-prompts")
async def maintenance_strip_prompts(older_than_days: int = 7):
    """Löscht prompt_text/response_text aus älteren Einträgen (nach Auswertung).

    Spart Speicher ohne Performance-Metriken zu verlieren.
    """
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
        return {"ok": True, "stripped_rows": cur.rowcount, "cutoff": cutoff}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/maintenance/evict-models")
async def maintenance_evict_models():
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
                logger.error(f"[evict-models] failed to evict {name}: {e}")
    return {"ok": True, "evicted": evicted}


@app.post("/maintenance/force-purge")
async def maintenance_force_purge():
    """Zweistufiger Hard-Reset: erst soft evict (keep_alive=0), dann killall llama-server.
    Nützlich wenn eine laufende Inference nicht reagiert.
    Ollama startet llama-server beim nächsten Request automatisch neu."""
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
    return {"ok": True, "soft_evicted": evicted, "hard_killed_pids": killed_pids}


@app.post("/maintenance/stop-all")
async def maintenance_stop_all():
    """Sperrt alle Inference-Requests (503) und killt laufende Prozesse.
    Mit /maintenance/resume wieder aufheben."""
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
    return {"ok": True, "stop_all": True, "evicted": evicted, "killed_pids": killed}


@app.post("/maintenance/resume")
async def maintenance_resume():
    """Hebt den Stop-All-Modus auf — Requests werden wieder durchgelassen."""
    global _stop_all
    _stop_all = False
    logger.info("[stop-all] deaktiviert — Inference wieder erlaubt")
    await _notify("resume", "✅ Stop-All aufgehoben", "Inference wieder aktiv.")
    return {"ok": True, "stop_all": False}

class GamingOverrideRequestProxy(BaseModel):
    override: str

@app.post("/maintenance/gaming_override")
async def set_gaming_override(req: GamingOverrideRequestProxy):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"{GPU_AGENT_URL}/gaming_override", json={"override": req.override})
        r.raise_for_status()
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
        }
    result["stop_all"] = _stop_all
    return result


@app.get("/status/gpu/html", response_class=__import__("fastapi").responses.HTMLResponse)
async def status_gpu_html():
    """Einfache HTML-Ansicht des GPU-Status."""
    data = await status_gpu()
    stop_banner = (
        '<div style="background:#ff4455;color:#fff;padding:12px 20px;font-weight:bold;font-size:15px;">'
        '🛑 STOP-ALL AKTIV — alle Inference-Requests werden blockiert'
        ' &nbsp;<a href="#" onclick="fetch(\'/maintenance/resume\',{method:\'POST\'}).then(()=>location.reload())" '
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
        '<button onclick="fetch(\'/maintenance/stop-all\',{{method:\'POST\'}}).then(()=>location.reload())" '
        'style="background:#ff4455;color:#fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:14px;">🛑 Stop All</button>'
        if not _stop_all else
        '<button onclick="fetch(\'/maintenance/resume\',{{method:\'POST\'}}).then(()=>location.reload())" '
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
    asyncio.create_task(_poll_hardware())
    asyncio.create_task(_poll_loaded_models())
    asyncio.create_task(_poll_model_catalog())
    asyncio.create_task(_sse_broadcast_loop())
    asyncio.create_task(_update_baselines())
    asyncio.create_task(_evict_idle_models())
    asyncio.create_task(_update_client_profiles())
    asyncio.create_task(_checkpoint_wal())

    cfg_ollama = uvicorn.Config(app,       host="0.0.0.0", port=LISTEN_PORT,  log_level="warning")
    cfg_comfy  = uvicorn.Config(comfy_app, host="0.0.0.0", port=COMFYUI_PORT, log_level="warning")

    logger.info("llmproxy starting:")
    logger.info(f"  Ollama  :{LISTEN_PORT}  → {OLLAMA_UPSTREAM_0} & {OLLAMA_UPSTREAM_1}")
    logger.info(f"  ComfyUI :{COMFYUI_PORT} → {COMFYUI_UPSTREAM}")
    logger.info(f"  DB      : {DB_PATH}")

    await asyncio.gather(
        uvicorn.Server(cfg_ollama).serve(),
        uvicorn.Server(cfg_comfy).serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run_proxies())
    except KeyboardInterrupt:
        pass

# ── Admin control endpoints ────────────────────────────────────────────────────

_ADMIN_TOKEN = Path("/opt/llmproxy/.admin_token").read_text().strip()

GAMING_SERVICES  = ["ollama", "comfyui", "comfyui2", "open-webui"]
RESTORE_SERVICES = ["ollama", "comfyui", "comfyui2"]

def _check_admin(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if token != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

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
