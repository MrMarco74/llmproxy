<p align="center">
  <img src="assets/logo.png" alt="llmproxy logo" width="120">
</p>

# llmproxy

A logging and management proxy for Ollama + ComfyUI. It runs as a systemd service on a proxy host, forwards requests over the network to a dedicated GPU host, and enriches them with statistics, smart features, and observability.

**Why separate from the GPU host?** This allows the Dashboard, Logging, and Stats to be available 24/7 without requiring the power-hungry GPU host to be running continuously. Hardware and "Gaming Mode" data (GPU load, Steam status) are delivered via a lightweight HTTP agent (`gpu-agent`) running on the GPU host.

The GPU host's address is configurable via the `LLMPROXY_GPU_HOST` environment variable (default: `gpu-host`) — set it to your GPU machine's hostname or IP, e.g. via a systemd unit override or `/etc/hosts` entry.

## Architecture

```text
Local Network (e.g. 192.168.1.0/24)
│
├── Your PC / Roo Cline / Open WebUI
│     └── → <proxy-host>:11435  (llmproxy)
│
├── <proxy-host> (Always-on VM/Server)
│     ├── llmproxy           :11435  (systemd)   ← this proxy
│     │     ├── → <gpu-host>:11434   (Ollama, network forward)
│     │     ├── → <gpu-host>:8188    (ComfyUI, :18189 network forward)
│     │     └── → <gpu-host>:11436   (gpu-agent: GPU/CPU/RAM/Gaming-Mode)
│     ├── llmproxy-dashboard  :18080  (Docker)
│     └── /root/.llmproxy.db           (SQLite, WAL)
│
└── <gpu-host> (Linux Server with GPU — on-demand)
      ├── Ollama       :11434  (local)
      ├── ComfyUI      :8188   (local, :18189)
      └── gpu-agent    :11436  (systemd) — Hardware/Gaming-Mode status for llmproxy

Client Machines (any number)
      └── llm-monitor  (PyQt5 Desktop App, SSE-Consumer)
```

## Features

| Feature | Description |
|---|---|
| **Logging** | All requests are logged in SQLite with 20+ fields (tokens, tps, ttft, client_ip, ...) |
| **Admin Dashboard** | Web-based dashboard to manage clients, tokens, frontier endpoints, and view logs/trends. |
| **Frontier LLMs** | Transparently proxy requests to external OpenAI-compatible APIs (e.g. Gemini, OpenRouter) |
| **Client Management** | Daily token budgets, model whitelisting, and blocklists per IP via `clients.yaml` |
| **Gaming-Mode** | Detects active gaming sessions (e.g., `pgrep steam`) — LLM endpoints return HTTP 503 while gaming |
| **Failure-Logging** | Stores user message context (500 chars) for errors and ignored tool calls in the `failures` table |
| **Hardware-Monitor** | Live CPU/RAM/GPU monitoring broadcasted via SSE |
| **SSE Broadcast** | Fan-out to N concurrent monitor clients (Desktop App + Dashboard) |
| **Notifications** | Internal system: SQLite → SSE unread_count → Dashboard Bell + Desktop Toast |
| **Complexity Scorer** | Estimates request complexity and predicted duration |
| **Model Fingerprinting** | Baseline-tps per model, anomaly detection if performance drops by > 50% |
| **Auto-Router** | Reroutes simple requests to smaller models (`routing.yaml`) |
| **Idle Eviction** | Unloads inactive models from VRAM after a configured threshold (`eviction.yaml`) |
| **Chargeback API** | Cost/usage reporting per client with IP-level drilldown and CSV/XLSX export, protected by a dedicated read-only token (`pricing.yaml`) |

## Quickstart

### Server (`<proxy-host>`)

```bash
# Transfer files and install
rsync -av . root@<proxy-host>:/tmp/llmproxy-deploy/
ssh root@<proxy-host> 'bash /tmp/llmproxy-deploy/scripts/install.sh'

# Check status
ssh root@<proxy-host> 'systemctl status llmproxy'
curl http://<proxy-host>:11435/health
```

### Hardware Agent (`<gpu-host>`)

Provides GPU/CPU/RAM and Gaming-Mode data to the proxy. Without it, hardware gauges and the gaming mode feature will be disabled.

```bash
rsync -av . root@<gpu-host>:/tmp/llmproxy-deploy/
ssh root@<gpu-host> 'bash /tmp/llmproxy-deploy/scripts/install_gpu_agent.sh'

# Check status
ssh root@<gpu-host> 'systemctl status gpu-agent'
curl http://<gpu-host>:11436/status
```

### Client App (Any Linux Machine)

```bash
git clone <repo-url>
cd llmproxy
bash scripts/install_monitor.sh --url http://<proxy-host>:11435
# → App appears in your application menu as "LLM Monitor"
```

### Live Dashboard (Docker, on `<proxy-host>`)

```bash
ssh root@<proxy-host>
cd /opt/llmproxy
docker compose up -d
# → http://<proxy-host>:18080
```

## Configuration Files

Default config files live in `config/` in this repo. `install.sh` copies them to `/opt/llmproxy/` on the proxy host on first install (existing files there are never overwritten):

- `clients.yaml` — Token budgets, model whitelists, and block rules per IP.
- `frontier.yaml` — Upstream OpenAI-compatible API credentials (e.g. Gemini, Groq, OpenRouter).
- `routing.yaml` — Auto-router rules based on request complexity.
- `eviction.yaml` — VRAM idle eviction configuration.
- `notifications.yaml` — Notification events configuration.
- `pricing.yaml` — €/1k-token pricing per model, used by the chargeback API (`/admin/chargeback/*`) to cost out frontier-LLM usage. Local Ollama models and any model without a price entry are counted as 0€ (surfaced as `unpriced_models` in API responses).

## Project Structure

```text
llmproxy/
├── proxy/          llmproxy.py + systemd unit (the logging proxy itself)
├── gpu-agent/       Stats sidecar for the GPU host (hardware + gaming-mode)
├── monitor/         Desktop app (PyQt5) + icon assets
├── config/          Default YAML configs, copied to /opt/llmproxy/ on install
├── scripts/         Install/deploy/maintenance shell scripts
├── dashboard/       FastAPI + Docker live dashboard
└── docs/            User guide & technical reference (German)
```

## Documentation

- [User Guide](docs/user-guide.md) — Installation, Configuration, FAQ (German)
- [Technical Reference](docs/technical.md) — Endpoints, DB Schema, Architecture (German)
