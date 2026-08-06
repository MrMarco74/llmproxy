# API Reference

llmproxy's admin/chargeback API is a plain REST API served by the proxy
process on port `11435`. Interactive Swagger UI is available at
`https://<proxy-host>:11435/docs` (grouped by tag — Guardrails, Clients,
Chargeback, RBAC, Splunk); this page covers the auth model and gives a
worked curl example for each major endpoint group. For the full endpoint
list with request/response shapes, see
[technical.md §2](technical.md#2-http-endpoints-proxy) and §8.

## Auth model

Every `/admin/*` and `/admin/chargeback/*` endpoint requires one of:

- **`Authorization: Bearer <key_id>.<secret>`** (preferred) — an API key
  created via `POST /admin/api_keys`, checked against the `api_keys`
  table. Each key has exactly one role:
  | Role | Scope |
  |---|---|
  | `admin` | Full access — everything below, plus `/maintenance/*` (stop-all, reboot, evict-models, ...) and chargeback pricing. |
  | `finance` | Chargeback reads (`summary`/`drilldown`/`detail`/`export`) + pricing management. No guardrails, clients, or maintenance access. |
  | `automation` | Guardrails (read/write/simulate), client management, fail2ban bans, request log, chargeback reads. **No** `/maintenance/*`, no pricing. Meant for external processes — see [mcp.md](mcp.md). |
  | `viewer` (dashboard-only, not an `api_keys` role) | Read-only dashboard views. Not applicable to the raw API. |
- **`X-Admin-Token: <token>`** / **`X-Chargeback-Token: <token>`**
  (deprecated fallback) — the legacy shared tokens from before per-identity
  API keys existed, mapped to `admin`/`finance` respectively. Still
  accepted so nothing already deployed breaks, but new integrations
  should use an API key.

Create a key (needs an existing admin credential):
```bash
curl https://<proxy-host>:11435/admin/api_keys \
  -H "X-Admin-Token: <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"owner_type": "service", "owner_name": "my-integration", "role": "automation"}'
```
The response's `"bearer"` field (`key_id.secret`) is shown once — copy it
immediately. Use it as `Authorization: Bearer <bearer>` on every request.

## Guardrails

```bash
# Read the current rule set
curl -H "Authorization: Bearer $BEARER" https://<proxy-host>:11435/admin/guardrails

# Dry-run a prompt against the current rules without recording a violation
curl -X POST https://<proxy-host>:11435/admin/guardrails/simulate \
  -H "Authorization: Bearer $BEARER" -H "Content-Type: application/json" \
  -d '{"prompt": "ignore all previous instructions", "token_name": "some-client"}'
```
Actions: `deny`, `silent`, `warn`, `rewrite` (DLP masking),
`redirect`/`redirect_internal`/`redirect_external` (model swap), and
`reduce_effort_external` (lowers `reasoning_effort`, or a configurable
field, on requests currently headed to a frontier provider).

## Clients

```bash
curl -H "Authorization: Bearer $BEARER" https://<proxy-host>:11435/admin/clients
```
Full per-client config: token budgets, model allowlists, block status.
Bearer tokens themselves are never included (stored separately, encrypted).

## Example workflow: create a key → weekly chargeback → find the cost causer

A common integration: give a finance/BI process its own read-only key,
have it pull a weekly cost summary, then drill down into whichever client
drove the spend.

```bash
# 1. Create a finance-scoped key
curl https://<proxy-host>:11435/admin/api_keys \
  -H "X-Admin-Token: <admin-token>" -H "Content-Type: application/json" \
  -d '{"owner_type": "service", "owner_name": "weekly-billing-job", "role": "finance"}'
# → copy the returned "bearer" value into $BEARER below

# 2. Pull the weekly cost summary (USD + EUR per client)
curl -H "Authorization: Bearer $BEARER" \
  "https://<proxy-host>:11435/admin/chargeback/summary?group_by=week"

# 3. Found a client with an unexpected spike (e.g. token_name=cassandra) —
#    drill down into which client IPs actually drove it
curl -H "Authorization: Bearer $BEARER" \
  "https://<proxy-host>:11435/admin/chargeback/drilldown?token_name=cassandra"

# 4. Or pull the raw per-request detail for that window
curl -H "Authorization: Bearer $BEARER" \
  "https://<proxy-host>:11435/admin/chargeback/detail?token_name=cassandra"
```
CSV/XLSX export (`/admin/chargeback/export`) and pricing management
(`/admin/chargeback/pricing`) are `admin`/`finance`-only — not exposed to
`automation` keys.

## Splunk HEC export

See [technical.md §4](technical.md#4-konfigurations-referenz) for the
config file and the [JSON Schema](#splunk-hec-event-schema) below for the
exact event shape shipped to Splunk. Admin-only:

```bash
# Configure (token accepted write-only, never returned in plaintext)
curl -X POST https://<proxy-host>:11435/admin/splunk/config \
  -H "X-Admin-Token: <admin-token>" -H "Content-Type: application/json" \
  -d '{"enabled": true, "url": "https://splunk.internal.example.com:8088",
       "index": "llmproxy", "verify_tls": true, "token": "<hec-token>"}'

# Send one test event
curl -X POST https://<proxy-host>:11435/admin/splunk/test -H "X-Admin-Token: <admin-token>"
```

### Splunk HEC event schema

Every `_log_to_splunk()` call (guardrail hits/redirects/effort-reductions,
admin actions, fail2ban bans, DLP leaks, ...) produces one event with this
shape, both in the local `audit.log` file and — when Splunk export is
enabled — in the HEC POST body:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "llmproxy Splunk audit event",
  "type": "object",
  "required": ["timestamp", "event_type", "app"],
  "properties": {
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601, local server time (datetime.now(), no explicit timezone offset)."
    },
    "event_type": {
      "type": "string",
      "description": "Event kind. request_completed fires on every proxied inference request (kw is the full requests-table row -- model, tokens, duration, token_name, client_ip, ...); everything else is guardrail/admin-related.",
      "examples": ["request_completed", "guardrail_redirected", "guardrail_effort_reduced",
                   "guardrail_rewritten", "guardrail_violation", "guardrail_shadow_violation",
                   "guardrail_fail2ban", "output_guardrail_violation", "output_guardrail_dlp_leak",
                   "splunk_test"]
    },
    "app": { "const": "llmproxy" },
    "token_name": { "type": "string", "description": "Client identity (from clients.yaml / encrypted secrets), when applicable." },
    "client_ip": { "type": "string", "description": "Requesting client IP, when applicable." },
    "action": { "type": "string", "description": "Guardrail action that fired (deny/redirect/reduce_effort_external/...), when applicable." },
    "violation": { "type": "string", "description": "Human-readable trigger description, present on guardrail_violation events." },
    "from": { "type": "string", "description": "Original model name, present on guardrail_redirected events." },
    "to": { "type": "string", "description": "New model name after redirect, present on guardrail_redirected events." },
    "kind": { "type": "string", "enum": ["redirect_internal", "redirect_external"], "description": "Which redirect variant fired, present on guardrail_redirected events." },
    "model": { "type": "string", "description": "Current model, present on guardrail_effort_reduced events." },
    "field": { "type": "string", "description": "Body field that was set (default reasoning_effort), present on guardrail_effort_reduced events." },
    "value": { "description": "Value written to that field (default \"low\"), present on guardrail_effort_reduced events." },
    "rule": { "type": "object", "description": "The full triggered rule object, present on shadow-mode/output-violation events." },
    "snippet": { "type": "string", "description": "First 200 chars of the offending prompt/response, present on violation/DLP events." },
    "duration": { "type": "integer", "description": "Fail2Ban ban duration in seconds, present on guardrail_fail2ban events." },
    "message": { "type": "string", "description": "Present only on the synthetic splunk_test event sent by /admin/splunk/test." }
  },
  "additionalProperties": true
}
```

The HEC POST body itself wraps this event per Splunk's collector
convention: `{"event": <event above>, "sourcetype": "_json", "index": "<configured index, if set>"}`,
sent to `<url>/services/collector/event` with
`Authorization: Splunk <token>`.
