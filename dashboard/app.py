#!/usr/bin/env python3
"""
llmproxy Live-Dashboard — FastAPI web interface.
Läuft als Docker-Container auf dem Proxy-Host, liest SQLite DB (read-only volume mount)
und konsumiert /status/stream vom llmproxy.
"""

import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DB_PATH            = Path(os.environ.get("DB_PATH", "/data/llmproxy.db"))
PROXY_URL          = os.environ.get("LLMPROXY_STATUS_URL", "http://host.docker.internal:11435")
PROXY_STREAM       = f"{PROXY_URL.rstrip('/')}/status/stream"
PROXY_STATUS       = f"{PROXY_URL.rstrip('/')}/status"
PROXY_NOTIFICATIONS= f"{PROXY_URL.rstrip('/')}/notifications"

app = FastAPI(title="llmproxy Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _query(sql: str, params: list = None) -> list[dict]:
    con = _db()
    if not con:
        return []
    try:
        cur = con.execute(sql, params or [])
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        con.close()


# ── Live View ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def live(request: Request):
    # Initial status snapshot
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(PROXY_STATUS)
            snapshot = r.json() if r.status_code == 200 else {}
    except Exception:
        snapshot = {}
    return templates.TemplateResponse("index.html", {"request": request, "snapshot": snapshot})


@app.get("/events")
async def events():
    """Proxy the SSE stream from llmproxy to browser clients."""
    async def _forward():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=5, pool=5)) as client:
                async with client.stream("GET", PROXY_STREAM) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield f"data: {{\"error\": \"{e}\"}}\n\n"
    return StreamingResponse(_forward(), media_type="text/event-stream")


# ── Trends View ───────────────────────────────────────────────────────────────

@app.get("/trends", response_class=HTMLResponse)
async def trends(request: Request):
    return templates.TemplateResponse("trends.html", {"request": request})


# ── History View ──────────────────────────────────────────────────────────────

@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, days: int = 7):
    since = (date.today() - timedelta(days=days)).isoformat()

    by_model = _query("""
        SELECT model, COUNT(*) as requests,
               SUM(total_tokens) as tokens,
               AVG(tokens_per_second) as avg_tps,
               AVG(ttft_s) as avg_ttft
        FROM requests WHERE date >= ? AND status_code=200
        GROUP BY model ORDER BY tokens DESC
    """, [since])

    by_ip = _query("""
        SELECT client_ip, COUNT(*) as requests,
               SUM(total_tokens) as tokens,
               ROUND(AVG(CAST(status_code != 200 AS REAL))*100,1) as failure_rate_pct
        FROM requests WHERE date >= ?
        GROUP BY client_ip ORDER BY requests DESC
    """, [since])

    timeline = _query("""
        SELECT date,
               COUNT(*) as requests,
               SUM(total_tokens) as tokens,
               AVG(tokens_per_second) as avg_tps,
               SUM(gaming_blocked) as gaming_blocks
        FROM requests WHERE date >= ?
        GROUP BY date ORDER BY date
    """, [since])

    tool_stats = _query("""
        SELECT model,
               SUM(CAST(has_tools AS INT)) as with_tools,
               SUM(CASE WHEN has_tools=1 AND num_tool_calls=0 THEN 1 ELSE 0 END) as ignored,
               COUNT(*) as total
        FROM requests WHERE date >= ? AND status_code=200
        GROUP BY model HAVING with_tools > 0
    """, [since])

    # tps scatter: num_ctx vs tps
    scatter = _query("""
        SELECT model, num_ctx, tokens_per_second
        FROM requests
        WHERE date >= ? AND num_ctx IS NOT NULL AND tokens_per_second > 0 AND status_code=200
        ORDER BY RANDOM() LIMIT 500
    """, [since])

    return templates.TemplateResponse("history.html", {
        "request":    request,
        "days":       days,
        "by_model":   by_model,
        "by_ip":      by_ip,
        "timeline":   timeline,
        "tool_stats": tool_stats,
        "scatter":    scatter,
    })


# ── Failures View ─────────────────────────────────────────────────────────────

@app.get("/failures", response_class=HTMLResponse)
async def failures_view(request: Request, limit: int = 100):
    failures = _query("""
        SELECT ts, model, client_ip, endpoint, status_code,
               failure_reason, last_user_message
        FROM failures ORDER BY id DESC LIMIT ?
    """, [limit])

    by_reason = _query("""
        SELECT failure_reason, COUNT(*) as count
        FROM failures GROUP BY failure_reason ORDER BY count DESC
    """)

    by_model = _query("""
        SELECT model, COUNT(*) as count
        FROM failures GROUP BY model ORDER BY count DESC LIMIT 10
    """)

    return templates.TemplateResponse("failures.html", {
        "request":   request,
        "failures":  failures,
        "by_reason": by_reason,
        "by_model":  by_model,
        "limit":     limit,
    })


# ── Setup / Download ──────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    archive_exists = Path("static/llm-monitor-setup.tar.gz").exists()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "proxy_url": PROXY_URL.rstrip("/"),
        "archive_exists": archive_exists,
    })

@app.get("/changelog", response_class=HTMLResponse)
async def changelog_view(request: Request):
    return templates.TemplateResponse("changelog.html", {"request": request})


# ── Request Log View ─────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_view(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/api/admin/clients")
async def api_admin_clients():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{PROXY_URL}/admin/clients")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/clients")
async def api_admin_clients_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{PROXY_URL}/admin/clients", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/frontier")
async def api_admin_frontier():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{PROXY_URL}/admin/frontier")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/frontier")
async def api_admin_frontier_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{PROXY_URL}/admin/frontier", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/models")
async def api_admin_models():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r_local = await client.get(f"{PROXY_URL}/api/tags")
            local_models = [m.get("name") for m in r_local.json().get("models", [])]
            r_front = await client.get(f"{PROXY_URL}/admin/frontier")
            frontier_cfg = r_front.json()
            frontier_models = []
            if frontier_cfg.get("enabled"):
                for p, c in frontier_cfg.get("providers", {}).items():
                    frontier_models.extend(c.get("models", []))
            return {"local_models": list(set(local_models)), "frontier_models": list(set(frontier_models))}
    except Exception as e:
        return {"local_models": [], "frontier_models": []}

@app.get("/api/admin/clients_usage")
async def api_admin_clients_usage():
    today = date.today().isoformat()
    rows = _query("SELECT client_ip, tokens_used FROM budgets WHERE date=?", [today])
    return {r["client_ip"]: r["tokens_used"] for r in rows}


# ── Request Log View ─────────────────────────────────────────────────────────

@app.get("/log", response_class=HTMLResponse)
async def log_view(request: Request, page: int = 1, limit: int = 50):
    offset = (page - 1) * limit
    rows = _query("""
        SELECT id, date, ts, client_ip, hostname, model, total_tokens,
               duration_s, status_code, prompt_text, response_text
        FROM requests
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """, [limit, offset])
    total_row = _query("SELECT COUNT(*) as n FROM requests")
    total = total_row[0].get("n", 0) if total_row else 0
    return templates.TemplateResponse("log.html", {
        "request": request,
        "rows":    rows,
        "page":    page,
        "limit":   limit,
        "total":   total,
    })


# ── API endpoints for AJAX ────────────────────────────────────────────────────

@app.get("/api/stats/summary")
async def stats_summary(days: int = 7):
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = _query("""
        SELECT COUNT(*) as requests, SUM(total_tokens) as tokens,
               AVG(tokens_per_second) as avg_tps, SUM(gaming_blocked) as gaming_blocks,
               SUM(CASE WHEN status_code != 200 THEN 1 ELSE 0 END) as errors
        FROM requests WHERE date >= ?
    """, [since])
    return rows[0] if rows else {}


@app.get("/api/notifications")
async def get_notifications(limit: int = 50, unread_only: bool = False):
    """Proxy notification list from llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(PROXY_NOTIFICATIONS,
                                 params={"limit": limit, "unread_only": str(unread_only).lower()})
            return r.json()
    except Exception:
        return {"items": [], "unread_count": 0}


@app.post("/api/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: int):
    """Proxy mark-as-read to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{PROXY_NOTIFICATIONS}/{notif_id}/read")
            return r.json()
    except Exception:
        return {"ok": False}


@app.post("/api/notifications/read-all")
async def mark_all_notifications_read():
    """Proxy mark-all-read to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{PROXY_NOTIFICATIONS}/read-all")
            return r.json()
    except Exception:
        return {"ok": False}


@app.post("/api/cleanup")
async def cleanup_logs(full: bool = False):
    """Proxy log cleanup (old, or with full=true ALL, failures/notifications/requests) to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/cleanup", params={"full": str(full).lower()})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/purge-vram")
async def purge_vram():
    """Proxy on-demand VRAM eviction (all loaded models) to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/evict-models")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/stop-all")
async def proxy_stop_all():
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/stop-all")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/resume")
async def proxy_resume():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/resume")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/proxy/health")
async def proxy_health():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{PROXY_URL}/health")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/logging")
async def get_logging_config():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{PROXY_URL}/maintenance/logging")
            return r.json()
    except Exception as e:
        return {"enabled": True}

@app.post("/api/logging")
async def set_logging_config(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/logging", json={"enabled": bool(data.get("enabled", True))})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/gaming_override")
async def proxy_gaming_override(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/gaming_override", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
