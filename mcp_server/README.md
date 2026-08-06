# llmproxy-mcp

MCP server that lets an external automated process (DLP/abuse-detection
pipelines, incident-response tooling, cost-control scripts — anything that
can reach llmproxy over the network) read and regulate guardrails, clients,
and bans through llmproxy's admin API, without needing full admin access.

It's a thin stdio wrapper around llmproxy's existing HTTP API
(`/admin/guardrails`, `/admin/clients`, `/admin/bans`, `/admin/log`,
`/admin/chargeback/*`) — no separate execution path, no bypassing the
guardrail engine. Scope is capped by the `automation` role on llmproxy's
`api_keys` table: it can read and edit guardrails/clients/bans, but it
**cannot** touch `/maintenance/*` (stop-all, reboot, evict-models, etc.) or
chargeback pricing — those stay admin(+finance)-only.

## 1. Create an API key

From a machine that can reach llmproxy with an existing admin credential:

```bash
curl https://<llmproxy-host>:11435/admin/api_keys \
  -H "X-Admin-Token: <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"owner_type": "service", "owner_name": "abuse-detector", "role": "automation"}'
```

The response includes `"bearer"` (`key_id.secret`) — copy it now, it is
never shown again. Pick `owner_name` per process/environment (e.g.
`abuse-detector`, `cost-guard`) so `/admin/actions` audit entries stay
attributable. Revoke a key later via `DELETE /admin/api_keys/{key_id}`
(admin-only; `GET /admin/api_keys` lists ids).

## 2. Install

```bash
cd mcp_server
python -m venv .venv
.venv/bin/pip install -e .
```

### Tests

```bash
.venv/bin/pip install -e . pytest respx
.venv/bin/pytest tests/ -q
```

`tests/` mocks llmproxy's HTTP responses (via `respx`) rather than running
the real proxy in-process — `proxy/llmproxy.py` imports heavy DLP deps
(presidio-analyzer, spacy) at module load time that aren't needed to test
this package's request-shaping logic.

## 3. Configure a client

Environment variables the server needs:

- `LLMPROXY_URL` — e.g. `https://llmproxy.internal.example.com:11435`
- `LLMPROXY_API_KEY` — the `key_id.secret` bearer value from step 1

### Claude Code

```bash
claude mcp add llmproxy \
  --env LLMPROXY_URL=https://llmproxy.internal.example.com:11435 \
  --env LLMPROXY_API_KEY=<key_id.secret> \
  -- /path/to/mcp_server/.venv/bin/python -m llmproxy_mcp.server
```

or add to `.mcp.json`:

```json
{
  "mcpServers": {
    "llmproxy": {
      "command": "/path/to/mcp_server/.venv/bin/python",
      "args": ["-m", "llmproxy_mcp.server"],
      "env": {
        "LLMPROXY_URL": "https://llmproxy.internal.example.com:11435",
        "LLMPROXY_API_KEY": "<key_id.secret>"
      }
    }
  }
}
```

Any other MCP client (stdio transport) configures the same way — point it
at `python -m llmproxy_mcp.server` with those two env vars set.

## Tools

- `get_guardrails_config()` / `set_guardrails_config(config)` — full
  guardrails config (global + per-client rules). Actions available:
  `deny`, `silent`, `warn`, `notify`, `rewrite`, `redirect`,
  `redirect_internal`, `redirect_external`, `reduce_effort_external`.
  Triggers available: `keyword`, `regex`, `dlp`, `output_keyword`,
  `output_dlp`, `max_length`, `spend_threshold`. Full field reference:
  [../docs/technical.md](../docs/technical.md#guardrailsyaml).
- `simulate_guardrail(prompt, token_name="", rules=None)` /
  `simulate_guardrail_batch(token_name="", limit=100, rules=None)` — test a
  rule set against one prompt or the most recent real traffic, without
  recording a violation.
- `list_clients()` / `update_clients_config(config)` — full client config
  (limits, model allowlists, block status). No bearer tokens included
  (encrypted separately). Typical automation use: flip a client's
  `blocked` flag in response to an abuse signal.
- `list_bans()` / `unban_client(token_name)` — fail2ban state.
- `get_log(...)` — paginated request log with filters. Includes
  prompt/response text, so treat results as potentially sensitive.
- `get_admin_actions(limit=100)` — audit trail of `/maintenance/*` actions
  (visibility only; this role can't trigger them).
- `get_chargeback_summary(...)` / `get_chargeback_drilldown(token_name, ...)`
  / `get_chargeback_detail(...)` — cost/token data (USD + EUR) for
  cost-aware regulation. Pricing config and CSV/XLSX export are not
  exposed here (admin/finance only).
