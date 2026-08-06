"""MCP server exposing a curated subset of llmproxy's admin API for external
automated processes -- DLP/abuse-detection pipelines, incident-response
tooling, cost-control scripts -- to read and regulate guardrails, clients,
and bans without needing full admin access.

Scope is deliberately narrow (the `automation` role on llmproxy's api_keys
table, see proxy/llmproxy.py's _check_automation): guardrails config +
simulation, client management, fail2ban bans, request log, chargeback
reads. It does NOT expose /maintenance/* (stop-all, reboot, evict-models,
etc.) or chargeback pricing -- those stay admin(+finance)-only.

Run with: LLMPROXY_URL=https://llmproxy.internal.example.com:11435 \
    LLMPROXY_API_KEY=<key_id.secret> python -m llmproxy_mcp.server
"""

from mcp.server.mcpserver import MCPServer

from llmproxy_mcp.client import client

mcp = MCPServer("llmproxy")


# --- guardrails ---


@mcp.tool()
def get_guardrails_config() -> dict:
    """Full guardrails config: global_rules + per-client client_rules."""
    with client() as c:
        resp = c.get("/admin/guardrails")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def set_guardrails_config(config: dict) -> dict:
    """Replace the FULL guardrails config (global_rules + client_rules).
    Call get_guardrails_config first and modify its result -- this is a
    full replace, not a merge/patch. Actions: deny, silent, warn, rewrite,
    redirect, redirect_internal, redirect_external, reduce_effort_external.
    """
    with client() as c:
        resp = c.post("/admin/guardrails", json=config)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def simulate_guardrail(prompt: str, token_name: str = "", rules: list | None = None) -> dict:
    """Run one prompt through the guardrail engine without sending it
    anywhere or recording a violation. Pass `rules` to test a candidate
    rule set that hasn't been saved yet; omit it to use the live config
    for `token_name` (or global rules if token_name is blank)."""
    payload: dict = {"prompt": prompt, "token_name": token_name}
    if rules is not None:
        payload["rules"] = rules
    with client() as c:
        resp = c.post("/admin/guardrails/simulate", json=payload)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def simulate_guardrail_batch(token_name: str = "", limit: int = 100, rules: list | None = None) -> dict:
    """Replay the most recent `limit` real prompts (optionally filtered to
    one client) through the guardrail engine -- useful for testing a new
    rule against real traffic before enabling it for real."""
    payload: dict = {"token_name": token_name, "limit": limit}
    if rules is not None:
        payload["rules"] = rules
    with client() as c:
        resp = c.post("/admin/guardrails/simulate-batch", json=payload)
        resp.raise_for_status()
        return resp.json()


# --- clients ---


@mcp.tool()
def list_clients() -> dict:
    """Full clients.yaml config: limits, model allowlists, block status,
    frontier permissions per client. Bearer tokens are not included --
    those live in encrypted storage, not this config."""
    with client() as c:
        resp = c.get("/admin/clients")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def update_clients_config(config: dict) -> dict:
    """Replace the FULL clients config. Call list_clients first and modify
    its result -- this is a full replace, not a per-client patch. Typical
    automation use: flip a client's `blocked` flag in response to an abuse
    signal."""
    with client() as c:
        resp = c.post("/admin/clients", json=config)
        resp.raise_for_status()
        return resp.json()


# --- fail2ban ---


@mcp.tool()
def list_bans() -> dict:
    """Currently-active fail2ban bans: {token_name: unban_unix_timestamp}."""
    with client() as c:
        resp = c.get("/admin/bans")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def unban_client(token_name: str) -> dict:
    """Lift a fail2ban ban early and clear that client's strike count."""
    with client() as c:
        resp = c.post("/admin/unban", params={"token_name": token_name})
        resp.raise_for_status()
        return resp.json()


# --- log / audit ---


@mcp.tool()
def get_log(token_name: str = "", model: str = "", date_from: str = "", date_to: str = "",
            status: str = "", search: str = "", is_frontier: str = "",
            limit: int = 50, offset: int = 0) -> dict:
    """Paginated request log with filters -- includes prompt/response text,
    so treat results as potentially sensitive. `status`: "ok"|"error"|"".
    `is_frontier`: "1"|"0"|"" (local vs. external model)."""
    params = {"token_name": token_name, "model": model, "date_from": date_from,
              "date_to": date_to, "status": status, "search": search,
              "is_frontier": is_frontier, "limit": limit, "offset": offset}
    with client() as c:
        resp = c.get("/admin/log", params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def get_admin_actions(limit: int = 100) -> dict:
    """Audit trail of /maintenance/* actions (who/what/when) -- visibility
    only, this role cannot trigger those actions itself."""
    with client() as c:
        resp = c.get("/admin/actions", params={"limit": limit})
        resp.raise_for_status()
        return resp.json()


# --- chargeback (read-only; pricing/export stay admin+finance only) ---


@mcp.tool()
def get_chargeback_summary(token_name: str = "", date_from: str = "", date_to: str = "",
                            group_by: str = "day") -> dict:
    """Token/cost summary per client (USD + EUR), optionally filtered to
    one client and/or date range. `group_by`: "day"|"month"|"none"."""
    params = {"token_name": token_name, "date_from": date_from, "date_to": date_to, "group_by": group_by}
    with client() as c:
        resp = c.get("/admin/chargeback/summary", params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def get_chargeback_drilldown(token_name: str, date_from: str = "", date_to: str = "") -> dict:
    """Per-client-IP breakdown for one client (token_name is required) --
    who behind a shared client credential is actually generating the
    cost/token volume."""
    params = {"token_name": token_name, "date_from": date_from, "date_to": date_to}
    with client() as c:
        resp = c.get("/admin/chargeback/drilldown", params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def get_chargeback_detail(token_name: str = "", client_ip: str = "", model: str = "",
                           date_from: str = "", date_to: str = "",
                           limit: int = 50, offset: int = 0) -> dict:
    """Row-level chargeback detail (one row per request) with filters."""
    params = {"token_name": token_name, "client_ip": client_ip, "model": model,
              "date_from": date_from, "date_to": date_to, "limit": limit, "offset": offset}
    with client() as c:
        resp = c.get("/admin/chargeback/detail", params=params)
        resp.raise_for_status()
        return resp.json()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
