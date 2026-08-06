#!/usr/bin/env python3
"""
llmproxy Live-Dashboard — FastAPI web interface.
Läuft als Docker-Container auf dem Proxy-Host, liest SQLite DB (read-only volume mount)
und konsumiert /status/stream vom llmproxy.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

DB_PATH            = Path(os.environ.get("DB_PATH", "/data/llmproxy.db"))
PROXY_URL          = os.environ.get("LLMPROXY_STATUS_URL", "https://llmproxy.internal.familie-frischkorn.de:11435")
PROXY_STREAM       = f"{PROXY_URL.rstrip('/')}/status/stream"
PROXY_STATUS       = f"{PROXY_URL.rstrip('/')}/status"
PROXY_NOTIFICATIONS= f"{PROXY_URL.rstrip('/')}/notifications"

# Sent on every proxy call; only the /admin/* routes actually require it.
# Prefers the new per-identity service API key (created once by
# scripts/migrate_auth.py, role=admin) over the legacy shared admin token
# -- falls back to the old header if the migration hasn't been run on this
# host yet, so the dashboard keeps working either way.
ADMIN_TOKEN        = os.environ.get("LLMPROXY_ADMIN_TOKEN", "")
SERVICE_KEY        = os.environ.get("LLMPROXY_SERVICE_KEY", "")
_ADMIN_HEADERS     = {"Authorization": f"Bearer {SERVICE_KEY}"} if SERVICE_KEY else {"X-Admin-Token": ADMIN_TOKEN}

# Signs the login session cookie. Must come from the environment (mirrored
# from /opt/llmproxy/.dashboard_session_secret on the proxy host by the
# app_llmproxy Ansible role, same flow as LLMPROXY_ADMIN_TOKEN) -- the
# dashboard container itself has no writable persistent volume, so a
# secret generated in-container would be lost on every redeploy.
SESSION_SECRET     = os.environ.get("LLMPROXY_SESSION_SECRET", "")
if not SESSION_SECRET:
    import secrets as _secrets
    SESSION_SECRET = _secrets.token_urlsafe(32)
    print("[auth] WARNING: LLMPROXY_SESSION_SECRET not set -- using an ephemeral "
          "secret for this process only. Every login will be invalidated on "
          "restart. Set LLMPROXY_SESSION_SECRET (see docker-compose.yml).")

# ── RBAC route gating ────────────────────────────────────────────────────────
# Path-prefix based, since the app's ~60 routes are plain @app.get/@app.post
# handlers (no APIRouter/Depends convention here to hang per-route auth off
# of) -- a request-level middleware is the least invasive way to cover all
# of them without touching every function signature.
_ROLES = ("admin", "finance", "viewer")

_PUBLIC_PREFIXES = ("/login", "/static", "/favicon.ico", "/healthz")

_ADMIN_ONLY_PREFIXES = (
    "/admin", "/settings",
    "/api/admin/clients", "/api/admin/tokens", "/api/admin/frontier", "/api/admin/fallback",
    "/api/admin/guardrails", "/api/admin/bans", "/api/admin/unban", "/api/admin/actions",
    "/api/admin/models", "/api/admin/wol_config", "/api/admin/ping_test", "/api/admin/test_ollama",
    "/api/admin/llm_config", "/api/admin/test_frontier", "/api/admin/fetch_models",
    "/api/logging", "/api/cleanup", "/api/purge-vram",
    "/api/proxy/maintenance", "/api/proxy/gaming_override",
)

_FINANCE_OR_ADMIN_PREFIXES = ("/chargeback", "/api/admin/chargeback")

def _required_roles(path: str) -> set[str]:
    if path.startswith(_FINANCE_OR_ADMIN_PREFIXES):
        return {"admin", "finance"}
    if path.startswith(_ADMIN_ONLY_PREFIXES):
        return {"admin"}
    return set(_ROLES)  # any authenticated user

_FORBIDDEN_HTML = """<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<title>Kein Zugriff — llmproxy</title>
<style>body{background:#0f1117;color:#cdd6f4;font-family:'Segoe UI',system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1e1e2e;border:1px solid #313244;border-radius:8px;padding:2rem;text-align:center;max-width:24rem}
a{color:#89b4fa}</style></head><body><div class="card">
<div style="font-size:2rem;margin-bottom:.5rem">🔒</div>
<div style="font-weight:600;margin-bottom:.5rem">Kein Zugriff</div>
<div style="font-size:.875rem;color:#7f849c;margin-bottom:1rem">Deine Rolle hat keine Berechtigung für diesen Bereich.</div>
<a href="/">Zurück zur Live-Ansicht</a></div></body></html>"""

class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        role = request.session.get("role")
        if not role:
            # Not logged in at all -> send to login, remembering where they
            # were headed so they land there after a successful login.
            if path.startswith("/api/"):
                return JSONResponse({"error": "Not authenticated"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)

        if role not in _required_roles(path):
            # Logged in, but this role can't see this path. Must NOT
            # redirect to /login: an authenticated user hitting /login
            # immediately bounces back to `next` (see login_page), which
            # would loop forever against the very path that's forbidden.
            if path.startswith("/api/"):
                return JSONResponse({"error": "Forbidden"}, status_code=403)
            return HTMLResponse(_FORBIDDEN_HTML, status_code=403)

        return await call_next(request)

app = FastAPI(title="llmproxy Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Middleware order matters: the LAST one added is OUTERMOST (runs first),
# so SessionMiddleware must be added after AuthGateMiddleware for
# request.session to already be populated when the auth check runs.
app.add_middleware(AuthGateMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                    max_age=60 * 60 * 12)  # 12h — re-login once per workday


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for the Docker healthcheck -- every
    other route now requires a session, so this exists specifically to
    avoid the container being marked unhealthy for a reason unrelated to
    whether it's actually up (it isn't logged in, which is normal)."""
    return {"status": "ok"}


def _safe_next(path: str) -> str:
    """Only ever redirect to a same-origin relative path. A bare
    `startswith("/")` check still lets through protocol-relative URLs like
    `//evil.com/phish` (browsers treat `//` as scheme-relative, i.e. an
    absolute URL to a different host) or backslash variants some browsers
    normalize to forward slashes -- both would otherwise let `next=` on
    the login form redirect off-site after a successful login."""
    if not path or not path.startswith("/") or path.startswith(("//", "/\\")):
        return "/"
    return path

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if request.session.get("role"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": None})

@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_path = form.get("next") or "/"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{PROXY_URL}/admin/auth/verify",
                                   json={"username": username, "password": password})
            data = r.json()
    except Exception:
        data = {"ok": False}
    if not data.get("ok"):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next_path, "error": "Ungültiger Benutzername oder Passwort."},
            status_code=401
        )
    request.session["username"] = username
    request.session["role"] = data["role"]
    return RedirectResponse(_safe_next(next_path), status_code=303)

@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


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
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
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
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=5, pool=5), headers=_ADMIN_HEADERS) as client:
                async with client.stream("GET", PROXY_STREAM) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield f"data: {{\"error\": \"{e}\"}}\n\n"
    return StreamingResponse(_forward(), media_type="text/event-stream")


# ── Inference Stats View ─────────────────────────────────────────────────────
# Ersetzt die frühere Hardware-"Trends"-Ansicht (Temp/Load/VRAM/Power) — dafür
# ist jetzt hwmon zuständig (eigenes Projekt). Hier stattdessen: Inferenz-Metriken
# pro Request (tokens/s, num_ctx, ttft) aus der requests-Tabelle.

@app.get("/inference", response_class=HTMLResponse)
async def inference(request: Request):
    return templates.TemplateResponse("inference.html", {"request": request})


@app.get("/trends")
async def trends_redirect():
    """Alter Tab-Name — Redirect für Lesezeichen/gecachte Links."""
    return RedirectResponse(url="/inference", status_code=308)


@app.get("/api/stats/inference")
async def api_stats_inference(minutes: int = 60, limit: int = 500):
    since = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    rows = _query("""
        SELECT ts, model, tokens_per_second, num_ctx, ttft_s, duration_s,
               prompt_tokens, completion_tokens, total_tokens
        FROM requests
        WHERE ts >= ? AND status_code = 200
        ORDER BY id DESC
        LIMIT ?
    """, [since, limit])
    rows.reverse()  # chronologisch aufsteigend fürs Charting
    return JSONResponse(rows)


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

@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})

@app.get("/api/admin/clients")
async def api_admin_clients():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/clients")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/clients")
async def api_admin_clients_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/clients", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/tokens")
async def api_admin_tokens():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/tokens")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/tokens")
async def api_admin_tokens_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/tokens", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/frontier")
async def api_admin_frontier():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/frontier")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/frontier")
async def api_admin_frontier_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/frontier", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/fallback")
async def api_admin_fallback():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/fallback")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/fallback")
async def api_admin_fallback_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/fallback", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/guardrails")
async def api_admin_guardrails():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/guardrails")
            return r.json()
    except Exception:
        return {}

@app.post("/api/admin/guardrails")
async def api_admin_guardrails_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/guardrails", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/guardrails/simulate")
async def api_admin_guardrails_simulate(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/guardrails/simulate", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/guardrails/simulate-batch")
async def api_admin_guardrails_simulate_batch(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=30.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/guardrails/simulate-batch", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/bans")
async def api_admin_bans():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/bans")
            return r.json()
    except Exception:
        return {"bans": {}}

@app.post("/api/admin/unban")
async def api_admin_unban(request: Request):
    try:
        data = await request.json()
        token_name = data.get("token_name", "")
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/unban", params={"token_name": token_name})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/actions")
async def api_admin_actions(limit: int = 100):
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/actions", params={"limit": limit})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/models")
async def api_admin_models():
    # Getrennte try/except pro Quelle: /api/tags hängt vom GPU-Host ab und kann
    # bei down-Ollama Sekunden dauern (zwei sequentielle Upstream-Timeouts im
    # Proxy) - das darf die Frontier-Modelle nicht mit wegreissen, die von
    # frontier.yaml kommen und unabhängig davon sofort verfügbar sind.
    local_models = []
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=_ADMIN_HEADERS) as client:
            r_local = await client.get(f"{PROXY_URL}/api/tags")
            local_models = [m.get("name") for m in r_local.json().get("models", [])]
    except Exception:
        pass

    frontier_models = []
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r_front = await client.get(f"{PROXY_URL}/admin/frontier")
            frontier_cfg = r_front.json()
            if frontier_cfg.get("enabled"):
                for p, c in frontier_cfg.get("providers", {}).items():
                    frontier_models.extend(c.get("models", []))
    except Exception:
        pass

    return {"local_models": list(set(local_models)), "frontier_models": list(set(frontier_models))}

@app.get("/api/admin/clients_usage")
async def api_admin_clients_usage():
    today = date.today().isoformat()
    rows = _query("SELECT token_name, tokens_used, tokens_used_local, tokens_used_frontier FROM budgets WHERE date=?", [today])
    return {
        r["token_name"]: {
            "used": r["tokens_used"],
            "used_local": r["tokens_used_local"],
            "used_frontier": r["tokens_used_frontier"]
        } for r in rows
    }


# ── Usage Audit ───────────────────────────────────────────────────────────────

@app.get("/audit", response_class=HTMLResponse)
async def audit_view(request: Request):
    return templates.TemplateResponse("audit.html", {"request": request})

@app.get("/api/admin/usage_by_client_day")
async def api_admin_usage_by_client_day(token_name: str = "", date_from: str = "", date_to: str = ""):
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/usage_by_client_day",
                                  params={"token_name": token_name, "date_from": date_from, "date_to": date_to})
            return r.json()
    except Exception as e:
        return {"rows": [], "error": str(e)}

@app.post("/api/admin/audit/run")
async def api_admin_audit_run(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=200.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/audit/run", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/audit_config")
async def api_admin_audit_config():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/audit_config")
            return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── Request Log View ─────────────────────────────────────────────────────────

@app.get("/log", response_class=HTMLResponse)
async def log_view(request: Request):
    return templates.TemplateResponse("log.html", {"request": request})

@app.get("/api/log")
async def api_log_view(request: Request):
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/log", params=request.query_params)
            return r.json()
    except Exception as e:
        return {"total": 0, "rows": [], "error": str(e)}


# ── Chargeback / cost-allocation view ────────────────────────────────────────────

@app.get("/chargeback", response_class=HTMLResponse)
async def chargeback_view(request: Request):
    return templates.TemplateResponse("chargeback.html", {"request": request})

@app.get("/api/admin/chargeback/summary")
async def api_chargeback_summary(request: Request):
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/chargeback/summary", params=request.query_params)
            return r.json()
    except Exception as e:
        return {"rows": [], "error": str(e)}

@app.get("/api/admin/chargeback/drilldown")
async def api_chargeback_drilldown(request: Request):
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/chargeback/drilldown", params=request.query_params)
            return r.json()
    except Exception as e:
        return {"rows": [], "error": str(e)}

@app.get("/api/admin/chargeback/detail")
async def api_chargeback_detail(request: Request):
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/chargeback/detail", params=request.query_params)
            return r.json()
    except Exception as e:
        return {"total": 0, "rows": [], "error": str(e)}

@app.get("/api/admin/chargeback/export")
async def api_chargeback_export(request: Request):
    async with httpx.AsyncClient(timeout=30.0, headers=_ADMIN_HEADERS) as client:
        r = await client.get(f"{PROXY_URL}/admin/chargeback/export", params=request.query_params)
        return Response(content=r.content, media_type=r.headers.get("content-type", "application/octet-stream"),
                         headers={"Content-Disposition": r.headers.get("content-disposition", "attachment")})

@app.get("/api/admin/chargeback/pricing")
async def api_chargeback_pricing_get():
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/chargeback/pricing")
            return r.json()
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/admin/chargeback/pricing")
async def api_chargeback_pricing_set(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/chargeback/pricing", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/admin/test_frontier")
async def api_admin_test_frontier(request: Request):
    try:
        data = await request.json()
        base_url = data.get("base_url", "").rstrip("/")
        api_key = data.get("api_key", "")
        if not base_url:
            return {"ok": False, "error": "Missing Base URL"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = await client.get(f"{base_url}/models", headers=headers)
            r.raise_for_status()

            models = r.json().get("data", [])
            model_names = [m.get("id") for m in models if m.get("id")]
            return {"ok": True, "count": len(model_names), "preview": model_names[:5]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/test_frontier/{provider}")
async def api_admin_test_frontier_saved(provider: str, request: Request):
    """For an already-saved provider: the API-key form field holds a mask,
    not a usable credential (see /admin/frontier's _FRONTIER_KEY_MASK), so
    testing has to happen on the proxy using its real stored secret."""
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/frontier/test/{provider}",
                                   json={"base_url": data.get("base_url", "")})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _parse_frontier_models(raw_json: dict) -> list[dict]:
    models = raw_json.get("data", [])
    result = []
    for m in models:
        if not m.get("id"): continue
        cost_p = "N/A"
        cost_c = "N/A"
        if "pricing" in m:
            # OpenRouter format
            p = m["pricing"].get("prompt")
            c = m["pricing"].get("completion")
            try:
                cost_p = f"${float(p)*1000000:.2f}/1M" if p else "N/A"
            except: pass
            try:
                cost_c = f"${float(c)*1000000:.2f}/1M" if c else "N/A"
            except: pass
        result.append({
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "cost_prompt": cost_p,
            "cost_completion": cost_c
        })
    result.sort(key=lambda x: x["id"].lower())
    return result

@app.post("/api/admin/fetch_models")
async def api_admin_fetch_models(request: Request):
    try:
        data = await request.json()
        base_url = data.get("base_url", "").rstrip("/")
        api_key = data.get("api_key", "")
        if not base_url:
            return {"ok": False, "error": "Missing Base URL"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = await client.get(f"{base_url}/models", headers=headers)
            r.raise_for_status()
            return {"ok": True, "models": _parse_frontier_models(r.json())}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/fetch_models/{provider}")
async def api_admin_fetch_models_saved(provider: str, request: Request):
    """For an already-saved provider: same masked-key problem as
    /api/admin/test_frontier/{provider} above -- fetch server-side on the
    proxy using its real stored key, then apply the usual parsing here."""
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/frontier/models/{provider}",
                                   json={"base_url": data.get("base_url", "")})
            proxy_data = r.json()
        if not proxy_data.get("ok"):
            return proxy_data
        return {"ok": True, "models": _parse_frontier_models(proxy_data["raw"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/test_ollama")
async def api_admin_test_ollama():
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/api/tags")
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
            return {"ok": True, "count": len(models), "preview": models[:5]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(PROXY_NOTIFICATIONS,
                                 params={"limit": limit, "unread_only": str(unread_only).lower()})
            return r.json()
    except Exception:
        return {"items": [], "unread_count": 0}


@app.post("/api/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: int):
    """Proxy mark-as-read to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_NOTIFICATIONS}/{notif_id}/read")
            return r.json()
    except Exception:
        return {"ok": False}


@app.post("/api/notifications/read-all")
async def mark_all_notifications_read():
    """Proxy mark-all-read to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_NOTIFICATIONS}/read-all")
            return r.json()
    except Exception:
        return {"ok": False}


@app.post("/api/cleanup")
async def cleanup_logs(full: bool = False):
    """Proxy log cleanup (old, or with full=true ALL, failures/notifications/requests) to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/cleanup", params={"full": str(full).lower()})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/purge-vram")
async def purge_vram():
    """Proxy on-demand VRAM eviction (all loaded models) to llmproxy."""
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/evict-models")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/stop-all")
async def proxy_stop_all():
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/stop-all")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/resume")
async def proxy_resume():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/resume")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/ollama-lock")
async def proxy_ollama_lock():
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/ollama-lock")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/ollama-unlock")
async def proxy_ollama_unlock():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/ollama-unlock")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/maintenance/wake-dana")
async def proxy_wake_dana():
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/wake-dana")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/wol_config")
async def api_admin_wol_config_get():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/wol_config")
            return r.json()
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/admin/wol_config")
async def api_admin_wol_config_set(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/wol_config", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/proxy/health")
async def proxy_health():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/health")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/logging")
async def get_logging_config():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/maintenance/logging")
            return r.json()
    except Exception as e:
        return {"enabled": True}

@app.post("/api/logging")
async def set_logging_config(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/logging", json={"enabled": bool(data.get("enabled", True))})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/proxy/gaming_override")
async def proxy_gaming_override(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/gaming_override", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/proxy/maintenance/{action}")
async def proxy_maintenance(action: str):
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/maintenance/{action}")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/wol_config")
async def api_wol_config():
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/wol_config")
            return r.json()
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/admin/wol_config")
async def api_wol_config_save(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/wol_config", json=data)
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/ping_test")
async def api_ping_test(request: Request):
    try:
        data = await request.json()
        hostname = data.get("hostname", "")
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/ping_test", json={"hostname": hostname})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/admin/test_ollama")
async def api_admin_test_ollama():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=_ADMIN_HEADERS) as client:
            r = await client.get(f"{PROXY_URL}/admin/test_ollama")
            return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/admin/llm_config")
async def api_llm_config(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=3.0, headers=_ADMIN_HEADERS) as client:
            r = await client.post(f"{PROXY_URL}/admin/llm_config", json=data)
            # The proxy may return a streaming endpoint that concatenates
            # multiple JSON objects (one per line).  Try the response body as-
            # is first, then fall back to taking only the first complete JSON
            # object if it starts with '{' but contains extra trailing data.
            raw = r.text.strip()
            try:
                resp_json = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # Try extracting just the first valid JSON object from mixed
                # or streaming output (e.g. two consecutive JSON blobs).
                brace_count = 0
                end_idx = None
                for i, ch in enumerate(raw):
                    if ch == "{":
                        if brace_count == 0:
                            start = i
                    elif ch == "}":
                        brace_count += 1
                        if brace_count == 0 and start is not None:
                            end_idx = i + 1
                            break
                try:
                    resp_json = json.loads(raw[start:end_idx])
                except Exception:
                    return {"ok": False, "error": f"Unparseable proxy response: {raw[:500]}"}
            return JSONResponse(content=resp_json) if isinstance(resp_json, dict) else JSONResponse(content={"data": resp_json})
    except Exception as e:
        return {"ok": False, "error": str(e)}
