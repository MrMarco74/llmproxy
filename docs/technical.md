<p align="center">
  <img src="../assets/logo.png" alt="llmproxy logo" width="96">
</p>

# llmproxy — Technische Referenz

## Inhaltsverzeichnis
1. [Architektur](#1-architektur)
2. [HTTP-Endpoints (Proxy)](#2-http-endpoints-proxy)
3. [Datenbank-Schema](#3-datenbank-schema)
4. [Konfigurations-Referenz](#4-konfigurations-referenz)
5. [Smart Features — Algorithmen](#5-smart-features--algorithmen)
6. [SSE Fan-out Broadcast](#6-sse-fan-out-broadcast)
7. [Deployment](#7-deployment)
8. [Dashboard-API](#8-dashboard-api)

---

## 1. Architektur

### Komponenten

| Komponente | Ort | Port | Technologie |
|---|---|---|---|
| `llmproxy.py` | <proxy-host> (systemd, root) | 11435, 18189 | FastAPI + asyncio |
| `llm_monitor.py` | Client-Maschinen | — | PyQt5 + httpx |
| `dashboard/app.py` | <proxy-host> (Docker) | 18080 | FastAPI + Jinja2 |
| `<gpu-host>-agent.py` | <gpu-host> (systemd, root) | 11436 | FastAPI — liefert GPU/CPU/RAM/Gaming-Mode an den Proxy |
| Ollama | <gpu-host> (lokal) | 11434 | — |
| ComfyUI | <gpu-host> (lokal) | 8188 | — |

> Der Proxy läuft auf der immer-laufenden Worker-VM, Ollama/ComfyUI bleiben auf
> <gpu-host> (GPU-Host) — Forwarding erfolgt über `<gpu-host>:11434`/`<gpu-host>:8188`. Hardware-
> und Gaming-Mode-Daten kommen remote vom `<gpu-host>-agent` (Port 11436), da GPU und
> Steam-Prozess auf <gpu-host> laufen, nicht auf dem Proxy-Host.

### Datenfluss

```
Client → :11435/v1/chat/completions
           ↓
        [_guard_gaming]     <gpu-host>-agent /status (gaming_mode) → 503 wenn aktiv
        [_guard_budget]     SQLite budgets → 429 wenn erschöpft
        [_compute_complexity]  Score 0–1
        [_apply_router]     routing.yaml → ggf. anderes Modell
           ↓
        <gpu-host>:11434/api/chat   (Netzwerk-Forward zum GPU-Host)
           ↓ (response)
        [_db_insert_request]  SQLite requests
        [_add_budget_usage]   SQLite budgets akkumulieren
        [_check_tps_anomaly]  vs. model_baselines
        [ntfy]               falls Anomalie
           ↓
        Client (streaming SSE / JSON)
```

### Background-Tasks (asyncio)

| Task | Intervall | Zweck |
|---|---|---|
| `_poll_gaming_mode` | 10s | pgrep steam, Statuswechsel-Notifications |
| `_poll_hardware` | 2s | psutil CPU/RAM + nvidia-smi GPU-Stats |
| `_poll_loaded_models` | 2s | Ollama `/api/ps` |
| `_sse_broadcast_loop` | 2s | Fan-out an alle SSE-Subscriber |
| `_update_baselines` | 1h | Rolling 7-day tps-Median per Modell |
| `_evict_idle_models` | 60s | VRAM-Check + Ollama DELETE wenn idle |
| `_update_client_profiles` | 6h | Per-IP Aggregate in DB |

---

## 2. HTTP-Endpoints (Proxy)

Alle Endpoints lauschen auf `:11435`.

### LLM-Proxy (mit Gaming-Guard + Budget-Guard)

| Methode | Pfad | Beschreibung |
|---|---|---|
| POST | `/api/chat` | Native Ollama Chat, streaming + non-streaming |
| POST | `/api/generate` | Native Ollama Generate |
| POST | `/v1/chat/completions` | OpenAI-kompatibel → übersetzt zu `/api/chat` |
| POST | `/v1/embeddings` | OpenAI-kompatible Embeddings |
| POST | `/api/embeddings` | Native Ollama Embeddings |

### Status & Monitoring

| Methode | Pfad | Response |
|---|---|---|
| GET | `/health` | `{status, ollama_up, gaming_mode}` |
| GET | `/health/recent` | `{requests: [...]}` — letzte 20 Requests aus DB |
| GET | `/status` | `{hw, models, gaming_mode, recent, budgets}` |
| GET | `/status/stream` | SSE, alle 2s, Fan-out Broadcast |
| GET | `/budget` | `{ip: {used, limit, pct}, ...}` für heutigen Tag |
| GET | `/clients` | Client-Profile aus `client_profiles`-Tabelle |
| GET | `/notifications` | `{items: [...], unread_count: N}` — Parameter: `limit`, `unread_only` |
| POST | `/notifications/{id}/read` | Notification als gelesen markieren |
| POST | `/notifications/read-all` | Alle Notifications als gelesen markieren |

### Maintenance

Alle `/maintenance/*`-Endpunkte (außer `/maintenance/logging` GET, siehe unten — der ist
inzwischen auch geschützt) verlangen den `X-Admin-Token`-Header (gleicher Mechanismus wie
`/admin/*`, siehe `_check_admin`). Das Dashboard sendet ihn bei jedem Proxy-Call bereits mit.

| Methode | Pfad | Beschreibung |
|---|---|---|
| POST | `/maintenance/stop-all` | Blockiert **ausnahmslos alle** Inference-Requests (503) und killt laufende llama-server-Prozesse. Aufheben via `/maintenance/resume`. |
| POST | `/maintenance/resume` | Hebt `/maintenance/stop-all` wieder auf. |
| POST | `/maintenance/ollama-lock` | Sperrt gezielt nur den Zugriff auf **lokale** Ollama-Modelle und entlädt sie sofort aus dem VRAM (z.B. um beide GPUs für ComfyUI freizuräumen). Clients mit `frontier_allowed: true` werden weiterhin über `fallback.yaml` bedient — sowohl `/v1/chat/completions` als auch `/api/chat`/`/api/generate` leiten dabei automatisch auf ein Frontier-Modell um (siehe Abschnitt [`fallback.yaml`](#fallbackyaml)). Ausnahme bleibt der bewusste Ollama-Lock selbst: der blockiert `/api/chat`/`/api/generate` weiterhin hart mit `503`, unabhängig von `frontier_allowed` — das ist Absicht (siehe `ollama_lock.yaml`-Abschnitt), kein fehlender Fallback. Embeddings (`/api/embeddings`, `/v1/embeddings`) kennen so oder so keinen Fallback. Zustand persistiert in `config/ollama_lock.yaml`, übersteht also einen Neustart. Response: `{ok, ollama_locked, evicted}`. |
| POST | `/maintenance/ollama-unlock` | Hebt `/maintenance/ollama-lock` wieder auf. Response: `{ok, ollama_locked}`. |
| POST | `/maintenance/evict-models` | Entlädt alle aktuell geladenen Modelle aus dem VRAM (on-demand, ohne Lock zu setzen). |
| POST | `/maintenance/force-purge` | Zweistufiger Hard-Reset: soft evict, dann `killall llama-server`. |
| POST/GET | `/maintenance/logging` | `{"enabled": bool}` — aktiviert/deaktiviert DB-Logging zur Laufzeit. |
| POST | `/maintenance/gaming_override` | `{"override": "auto"\|"on"\|"off"}` — manueller Override für die Gaming-Mode-Erkennung. |

Der aktuelle Zustand von `stop_all`, `ollama_locked` und `ollama_lock_auto` wird zusätzlich
über `/health` und `/status` ausgegeben (`/status/gpu` nur `ollama_locked`, ohne Herkunft).

#### Automatischer Ollama-Lock bei aktiver ComfyUI-Queue

`_poll_comfyui_queue()` pollt alle 10s `GET <gpu-host>:8188/queue`. Ist `queue_running` oder
`queue_pending` nicht leer, wird `_set_ollama_lock(True, auto=True, ...)` aufgerufen — intern,
ohne `X-Admin-Token` (kein HTTP-Request, direkter Funktionsaufruf). Ist die Queue leer **und**
der aktuelle Lock wurde vom Poller selbst gesetzt (`_ollama_lock_auto == True`), wird er wieder
aufgehoben. Ein manuell per `/maintenance/ollama-lock` gesetzter Lock (`auto=False`) wird vom
Poller **nicht** angetastet, auch wenn die Queue leer ist — erst ein manuelles
`/maintenance/ollama-unlock` gibt ihn wieder frei. Das ComfyUI-Analogon zu `_poll_gaming_mode()`.

### Debug

| Methode | Pfad | Body | Beschreibung |
|---|---|---|---|
| POST | `/debug/notify` | `{"event": "test"}` | Sendet Test-ntfy-Notification |

### Passthrough

Alle anderen Pfade werden transparent an Ollama weitergeleitet (GET/POST/DELETE/HEAD).

### OpenAI → Native Übersetzung

Der `/v1/chat/completions`-Handler übersetzt folgende Felder:

| OpenAI-Feld | Ollama-Äquivalent |
|---|---|
| `messages[].content` (Array) | Text extrahiert, `images` für base64-Bilder |
| `max_tokens` | `options.num_predict` |
| `temperature` | `options.temperature` |
| `top_p` | `options.top_p` |
| `stop` | `options.stop` |
| `tools` | `tools` (direkt) |
| Default-Injection | `options.num_gpu=-1, num_ctx=65536` |

Konsekutive Nachrichten gleicher Rolle werden automatisch zusammengefasst (Squashing).

### Response-Header

Bei Auto-Routing: `X-LLM-Routed-From: <original_model>`

---

## 3. Datenbank-Schema

Datei: `/root/.llmproxy.db` (SQLite, WAL-Mode)

### Tabelle `requests`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ts` | TEXT | ISO-8601 Timestamp (Sekunden) |
| `date` | TEXT | `YYYY-MM-DD` (für Index) |
| `model` | TEXT | Modellname |
| `prompt_tokens` | INTEGER | Eingabe-Token |
| `completion_tokens` | INTEGER | Ausgabe-Token |
| `total_tokens` | INTEGER | Summe |
| `duration_s` | REAL | Gesamtdauer in Sekunden |
| `tokens_per_second` | REAL | completion_tokens / duration_s |
| `ttft_s` | REAL | Time to First Token (nur Streaming) |
| `client_ip` | TEXT | IP des Anfragenden (X-Forwarded-For berücksichtigt) |
| `user_agent` | TEXT | HTTP User-Agent Header |
| `endpoint` | TEXT | z.B. `/v1/chat/completions` |
| `stream` | INTEGER | 0/1 |
| `num_messages` | INTEGER | Anzahl Nachrichten im Context |
| `has_tools` | INTEGER | 0/1 — Tool-Calling aktiviert |
| `num_tool_calls` | INTEGER | Anzahl Tool-Calls im Response |
| `num_ctx` | INTEGER | Verwendetes Context-Window |
| `status_code` | INTEGER | HTTP-Status der Upstream-Response |
| `gaming_blocked` | INTEGER | 0/1 — durch Gaming-Mode geblockt |
| `complexity_score` | REAL | 0.0–1.0, geschätzte Request-Komplexität |
| `predicted_duration_s` | REAL | Vorhergesagte Dauer (vor Forwarding) |
| `routed_from` | TEXT | Originales Modell wenn Auto-Router aktiv |

**Indizes**: `idx_date`, `idx_model`, `idx_ip`, `idx_ts`

### Tabelle `failures`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ts` | TEXT | ISO-8601 Timestamp |
| `model` | TEXT | Modellname |
| `client_ip` | TEXT | IP |
| `endpoint` | TEXT | Endpoint |
| `status_code` | INTEGER | HTTP-Status |
| `failure_reason` | TEXT | Einer der folgenden Werte |
| `last_user_message` | TEXT | Letzte User-Message (max. 500 Zeichen) |

**`failure_reason`-Werte**:

| Wert | Bedeutung |
|---|---|
| `upstream_error` | Ollama hat 500 zurückgegeben oder Verbindungsfehler |
| `tool_ignored` | `has_tools=True` aber `num_tool_calls=0` |
| `tps_anomaly` | `tokens_per_second < 50%` der Baseline |
| `model_evicted` | Modell durch Idle-Eviction entladen |
| `gaming_blocked` | Request wegen Gaming-Mode abgewiesen |

### Tabelle `model_baselines`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `model` | TEXT PK | Modellname |
| `median_tps` | REAL | Median tps (7 Tage) |
| `p10_tps` | REAL | 10. Perzentil |
| `p90_tps` | REAL | 90. Perzentil |
| `sample_count` | INTEGER | Stichprobengröße |
| `updated_at` | TEXT | Letztes Update |

### Tabelle `budgets`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `client_ip` | TEXT | IP-Adresse |
| `date` | TEXT | `YYYY-MM-DD` |
| `tokens_used` | INTEGER | Verbrauchte Tokens heute |

PK: `(client_ip, date)`

### Tabelle `notifications`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ts` | TEXT | ISO-8601 Timestamp |
| `event` | TEXT | Event-Name (z.B. `gaming_mode_start`) |
| `title` | TEXT | Kurztitel |
| `message` | TEXT | Detailtext |
| `priority` | TEXT | `default`, `high`, `urgent` |
| `read_at` | TEXT | ISO-8601 Timestamp wenn gelesen, sonst NULL |

### Tabelle `admin_actions`

Audit-Trail für `/maintenance/*`-Aktionen (siehe `_log_admin_action()`). Unabhängig von
`logging.yaml` immer geschrieben und nicht Teil von `/maintenance/cleanup`'s Löschlogik.

| Spalte | Typ | Beschreibung |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ts` | TEXT | ISO-8601 Timestamp |
| `action` | TEXT | z.B. `stop-all`, `ollama-lock`, `evict-models`, `force-purge`, `cleanup`, `logging`, `gaming_override` |
| `source` | TEXT | `admin` (per `X-Admin-Token`) oder `auto` (ComfyUI-Queue-Poller) |
| `client_ip` | TEXT | IP des Aufrufers, leer bei `source=auto` |
| `detail` | TEXT | Freitext, z.B. evicted-Modelle, gekillte PIDs, gesetzter Override |

Abfragbar über `GET /admin/actions?limit=100` (Admin-Token nötig).

### Tabelle `client_profiles`

| Spalte | Typ | Beschreibung |
|---|---|---|
| `client_ip` | TEXT PK | IP-Adresse |
| `avg_complexity` | REAL | Durchschnittlicher Complexity-Score |
| `top_model` | TEXT | Meistgenutztes Modell |
| `peak_hour` | INTEGER | Aktivste Stunde (0–23) |
| `tool_use_rate` | REAL | Anteil Requests mit Tools (0–1) |
| `avg_messages` | REAL | Durchschnittliche Konversationslänge |
| `total_requests` | INTEGER | Gesamtanzahl Requests (30 Tage) |
| `updated_at` | TEXT | Letztes Update |

---

## 4. Konfigurations-Referenz

### `clients.yaml`

```yaml
clients:
  "<ip>":
    limit: 50000000        # Tokens/Tag
    models: "*"            # Oder Liste: ["modelA", "modelB"]
    blocked: false         # Client global blockieren
    frontier_allowed: true # Zugriff auf externe Frontier-LLMs erlaubt
```

### `frontier.yaml`

```yaml
enabled: true
providers:
  "<provider_name>":
    base_url: "https://api.openai.com/v1"
    api_key: "sk-..."
    models:
      - "gpt-4o"
      - "gpt-3.5-turbo"
```

### `fallback.yaml`

Automatischer Fallback auf ein Frontier-Modell, wenn der zuständige Ollama-Upstream
nicht erreichbar ist — für `/v1/chat/completions` **und** die nativen Endpunkte
`/api/chat`/`/api/generate` (`proxy_native()`; native Request/Response wird dafür
transparent zu/von OpenAI-Schema konvertiert, der Client merkt vom Fallback nichts),
nur für Clients mit `frontier_allowed: true`. Erkennung über zwei Signale: einen
periodischen Health-Check (`_poll_model_catalog()`, alle 30s) plus sofortiges
Umleiten bei einem echten Verbindungsfehler auf dem Live-Request. Ausnahme: der
bewusst gesetzte `ollama_locked`-Zustand (siehe `ollama_lock.yaml`) blockiert die
nativen Endpunkte weiterhin hart ohne Fallback — das ist ein separater,
gewollter Schalter, keine Ausfall-Erkennung. `/api/embeddings` und `/v1/embeddings`
kennen grundsätzlich keinen Fallback, unabhängig vom Lock-Zustand.

```yaml
enabled: true
mapping:
  "<lokales_modell>": "<frontier_modell_aus_frontier.yaml>"
  "*": "<frontier_modell_aus_frontier.yaml>"   # Catchall, greift wenn kein exakter Match existiert
```

### `routing.yaml`

```yaml
routes:
  - if_complexity_below: <float 0–1>  # Score-Schwellwert
    model_pattern: "<string>"         # "*", "model:tag", oder "prefix:"
    route_to: "<string>"              # Ziel-Modellname
```

Regeln werden in Reihenfolge geprüft; erste Übereinstimmung gewinnt.

### `ollama_lock.yaml`

Manueller Schalter, der gezielt nur den Zugriff auf lokale Ollama-Modelle sperrt (z.B.
um beide GPUs exklusiv für ComfyUI freizuräumen) — im Unterschied zu `/maintenance/stop-all`,
das ausnahmslos alles blockiert. Clients mit `frontier_allowed: true` werden bei aktivem
Lock über `/v1/chat/completions` weiterhin bedient (leitet automatisch auf Frontier um).
`/api/chat` und `/api/generate` haben zwar seit Kurzem einen eigenen Fallback für echte
Ollama-Ausfälle (siehe `fallback.yaml`-Abschnitt), werden bei einem *bewusst gesetzten*
Ollama-Lock aber trotzdem hart mit `503` blockiert — das ist Absicht: der Lock-Guard
(`_guard_ollama_lock`) greift vor dem eigentlichen Handler und kennt keine Ausnahme für
diesen Fall. `/api/embeddings` und `/v1/embeddings` kennen so oder so keinen Fallback und
liefern immer hart `503`, sobald der lokale Ollama-Upstream nicht erreichbar ist.

```yaml
locked: <bool>
```

Wird nicht direkt editiert, sondern über die Endpunkte umgeschaltet (siehe unten) — der
Datei-Inhalt ist nur der persistierte Startzustand, der einen Proxy-Neustart übersteht.

### `eviction.yaml`

```yaml
eviction_timeout_min: <int>      # Idle-Zeit in Minuten (Default: 15)
vram_threshold_pct: <int 0–100>  # VRAM-Auslastung für Aktivierung (Default: 80)
never_evict:
  - "<model_name>"               # Exakte Modellnamen
```

### `notifications.yaml`

```yaml
events:
  <event_name>: <bool>     # true/false
```

Verfügbare Events: `gaming_mode_start`, `gaming_mode_end`, `budget_warning`, `budget_exceeded`, `tps_anomaly`, `thermal_warning`, `model_evicted`, `load_shedding`, `weekly_report`

Notifications werden intern in der SQLite-Tabelle `notifications` gespeichert und über den SSE-Stream als `unread_count` an alle Clients übermittelt.

---

## 5. Smart Features — Algorithmen

### Complexity Scorer

```python
score = min(
    (num_messages * 0.02)
  + (estimated_tokens / 50_000)   # estimated_tokens = chars / 4
  + (0.3 if has_tools else 0),
    1.0
)
```

### Model Fingerprinting / Anomalie-Erkennung

Baseline = `AVG(tokens_per_second)` der letzten 7 Tage für das jeweilige Modell (aus DB, stündlich aktualisiert).

Anomalie wenn: `tps < baseline * 0.5`

Typische Ursachen: VRAM-Offload auf RAM, thermisches Throttling, konkurrierender GPU-Load.

### Auto-Router

Pattern-Matching:
- `"*"` → passt auf alle Modelle
- `"qwen3:32b"` → exakter Match
- `"qwen3:"` → Prefix-Match (alle qwen3-Varianten)

Routing wird **nicht** angewendet wenn das Ziel-Modell bereits das Quell-Modell ist.

### Idle Eviction

```
Alle 60s:
  max_vram_pct = max(gpu.ram_used / gpu.ram_total) über alle GPUs
  if max_vram_pct < vram_threshold_pct: skip
  for model in loaded_models:
    last_request = MAX(ts) FROM requests WHERE model = ?
    if last_request < now - eviction_timeout_min:
      DELETE /api/delete → Ollama
```

---

## 6. SSE Fan-out Broadcast

Der Proxy hält eine globale Liste von `asyncio.Queue`-Objekten (`_sse_subscribers`). Für jeden SSE-Client (`GET /status/stream`) wird eine neue Queue angelegt und beim Disconnect wieder entfernt.

Der `_sse_broadcast_loop` Task schreibt alle 2 Sekunden einen Snapshot in alle Queues (`put_nowait`). Full Queues werden als Dead entfernt.

```
_poll_hardware()       → _hw_stats (global)
_poll_loaded_models()  → _loaded_models (global)
_poll_gaming_mode()    → _gaming_mode (global)
          ↓ alle 2s
_sse_broadcast_loop()  → put_nowait() in alle _sse_subscribers
          ↓
[Queue 1] → SSE Client 1 (llm_monitor.py auf PC-1)
[Queue 2] → SSE Client 2 (llm_monitor.py auf PC-2)
[Queue 3] → SSE Client 3 (Dashboard Docker)
```

**Wichtig**: Hardware wird exakt **einmal** abgefragt, unabhängig von der Anzahl der Clients.

---

## 7. Deployment

### Server-Deployment (<proxy-host>)

Proxy + Dashboard laufen auf der immer-laufenden Worker-VM
(`<proxy-host-ip>`), nicht mehr auf <gpu-host> — siehe [[project_<proxy-host>_vm]].
Ollama/ComfyUI bleiben auf <gpu-host>, <gpu-host> muss daher nur noch für Inferenz
selbst eingeschaltet sein.

```bash
# Von der Entwicklungsmaschine:
rsync -av /path/to/llmproxy/ root@<proxy-host>:/tmp/llmproxy-deploy/
ssh root@<proxy-host> 'bash /tmp/llmproxy-deploy/scripts/install.sh'
```

Der Installer kopiert nach `/opt/llmproxy/`, installiert Abhängigkeiten, richtet systemd ein.

### systemd Unit (`llmproxy.service`)

```ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/llmproxy/proxy/llmproxy.py
WorkingDirectory=/opt/llmproxy/proxy
Restart=on-failure
RestartSec=5
```

### Update

```bash
rsync -av --exclude='*.yaml' /path/to/llmproxy/ root@<proxy-host>:/opt/llmproxy/
ssh root@<proxy-host> 'systemctl restart llmproxy'
# Konfig-YAMLs werden mit --exclude nicht überschrieben
```

### Dashboard-Container

```bash
ssh root@<proxy-host> 'cd /opt/llmproxy && docker compose up -d --build'
```

### Logs

```bash
# Proxy-Logs
ssh root@<proxy-host> 'journalctl -u llmproxy -f'

# Dashboard-Logs
ssh root@<proxy-host> 'docker compose -f /opt/llmproxy/docker-compose.yml logs -f'
```

### <gpu-host>-agent (Stats-Quelle auf <gpu-host>)

Der Proxy braucht für Hardware-Monitoring und Gaming-Mode-Erkennung den
`<gpu-host>-agent` (liefert GPU/CPU/RAM/Steam-Status, da diese auf <gpu-host> laufen):

```bash
rsync -av /path/to/llmproxy/ root@<gpu-host>:/tmp/llmproxy-deploy/
ssh root@<gpu-host> 'bash /tmp/llmproxy-deploy/install_<gpu-host>_agent.sh'

# Logs
ssh root@<gpu-host> 'journalctl -u <gpu-host>-agent -f'
# Health-Check
curl http://<gpu-host>:11436/status
```

---

## 8. Dashboard-API

Basis-URL: `http://<proxy-host>:18080`

| Methode | Pfad | Parameter | Response |
|---|---|---|---|
| GET | `/` | — | HTML Live-View |
| GET | `/admin` | — | HTML Admin-View |
| GET | `/history` | `?days=7` | HTML Verlauf |
| GET | `/failures` | `?limit=100` | HTML Failure-Log |
| GET | `/events` | — | SSE (proxied von llmproxy) |
| GET | `/api/stats/summary` | `?days=7` | `{requests, tokens, avg_tps, gaming_blocks, errors}` |
| GET | `/api/admin/clients` | — | Liest `clients.yaml` |
| POST | `/api/admin/clients` | JSON Body | Speichert `clients.yaml` und lädt Konfiguration neu |
| GET | `/api/admin/clients_usage` | — | Liest aktuelle Budget-Nutzung |
| GET | `/api/admin/frontier` | — | Liest `frontier.yaml` |
| POST | `/api/admin/frontier` | JSON Body | Speichert `frontier.yaml` und lädt Konfiguration neu |
| GET | `/api/admin/fallback` | — | Liest `fallback.yaml` (+ `ollama_healthy`) |
| POST | `/api/admin/fallback` | JSON Body | Speichert `fallback.yaml` und lädt Konfiguration neu |
| GET | `/api/admin/actions` | `?limit=100` | Proxied `/admin/actions` — Audit-Trail der `/maintenance/*`-Aktionen |
| POST | `/api/proxy/maintenance/stop-all` | — | Proxied `/maintenance/stop-all` |
| POST | `/api/proxy/maintenance/resume` | — | Proxied `/maintenance/resume` |
| POST | `/api/proxy/maintenance/ollama-lock` | — | Proxied `/maintenance/ollama-lock` — sperrt lokale Ollama-Modelle, entlädt VRAM |
| POST | `/api/proxy/maintenance/ollama-unlock` | — | Proxied `/maintenance/ollama-unlock` — hebt den Ollama-Lock auf |
| GET | `/api/proxy/health` | — | Proxied `/health` (u.a. `ollama_locked`, `stop_all`, `gaming_mode`) |
