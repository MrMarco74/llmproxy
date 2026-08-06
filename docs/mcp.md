# MCP Server

`mcp_server/` is a standalone MCP (Model Context Protocol) server that
lets an external automated process — a DLP/abuse-detection pipeline,
incident-response tooling, a cost-control script, an AI agent — read and
regulate llmproxy's guardrails, clients, and bans without needing full
admin access.

## Why this exists

llmproxy already has a REST API (see [api.md](api.md)) that a script could
call directly with `httpx`/`curl`. The MCP server exists for the other
case: when the caller is itself an LLM agent (Claude Code, another
MCP-capable client) that should be able to *reason about and act on*
llmproxy's guardrail/client/ban state as part of a larger task — e.g. "a
client just tripped three DLP violations this hour, look at what
triggered and decide whether to tighten a rule or ban them" — without
handing that agent a raw admin credential.

## Scope: the `automation` role

The MCP server authenticates as an `api_keys` entry with role
`automation` — a role built specifically for this purpose, deliberately
narrower than `admin`:

**Can:** read/write guardrails config, simulate guardrail rules, manage
client config, view/lift fail2ban bans, read the request log, read
chargeback data (cost-aware regulation).

**Cannot:** touch `/maintenance/*` (stop-all, reboot, evict-models, force-purge,
...) or chargeback pricing — those stay `admin`(+`finance`)-only. An
`automation` key hitting either gets a 403, same as any other unauthorized
role.

This means a compromised or misbehaving MCP client can, at worst, change
routing/filtering rules — it can't take the GPU host offline or rewrite
what a client is billed.

## Architecture

`mcp_server/llmproxy_mcp/` is a separate Python package (own
`pyproject.toml`), **not** part of the main proxy app's dependency tree —
it's an out-of-process stdio-transport MCP server that speaks plain HTTPS
to llmproxy's existing REST API (`llmproxy_mcp/client.py` wraps an
`httpx.Client` with `Authorization: Bearer <key_id>.<secret>`,
`llmproxy_mcp/server.py` exposes 13 `@mcp.tool()` functions, each a thin
wrapper over one REST endpoint — no new execution path, no bypassing the
guardrail engine).

## Setup

Full install/configuration instructions (creating the API key, `pip
install -e .`, registering with Claude Code or another MCP client, and
the complete tool list) live in
**[`mcp_server/README.md`](../mcp_server/README.md)** — this page covers
the *why*, that one covers the *how*.

## Tests

`mcp_server/tests/` (separate from the root `tests/` suite covering
`proxy/`+`dashboard/`) mocks llmproxy's HTTP responses via `respx` and
verifies each tool's request shape. Run with:
```bash
cd mcp_server
pip install -e . pytest respx
pytest tests/ -q
```
