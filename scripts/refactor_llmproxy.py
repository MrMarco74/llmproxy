import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

# Update _db_log_failure signature and logic
content = content.replace("def _db_log_failure(*, model: str, client_ip: str, endpoint: str,",
                          "def _db_log_failure(*, model: str, client_ip: str, token_name: str = \"\", endpoint: str,")
content = content.replace("INSERT INTO failures (ts, model, client_ip, endpoint, status_code, failure_reason, last_user_message)",
                          "INSERT INTO failures (ts, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message)")
content = content.replace("[now, model, client_ip, endpoint, status_code, failure_reason, last_user_message[:500]]",
                          "[now, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message[:500]]")

# Update _log_admin_action signature and logic
content = content.replace("def _log_admin_action(action: str, source: str, client_ip: str = \"\", detail: str = \"\"):",
                          "def _log_admin_action(action: str, source: str, client_ip: str = \"\", token_name: str = \"\", detail: str = \"\"):")
content = content.replace("INSERT INTO admin_actions (ts, action, source, client_ip, detail)",
                          "INSERT INTO admin_actions (ts, action, source, client_ip, token_name, detail)")
content = content.replace("[now, action, source, client_ip, detail]",
                          "[now, action, source, client_ip, token_name, detail]")

# Update _db_get_recent
content = content.replace("SELECT ts, model, client_ip, endpoint", "SELECT ts, model, client_ip, token_name, endpoint")

# Update _active_requests and _req_start
content = content.replace("_active_requests: dict = {}   # req_id → {model, client_ip, endpoint, t_start}",
                          "_active_requests: dict = {}   # req_id → {model, client_ip, token_name, endpoint, t_start}")
content = content.replace("def _req_start(model: str, client_ip: str, endpoint: str, upstream_idx: int | None = None) -> int:",
                          "def _req_start(model: str, client_ip: str, token_name: str, endpoint: str, upstream_idx: int | None = None) -> int:")
content = content.replace("_active_requests[rid] = {\"model\": model, \"client_ip\": client_ip,",
                          "_active_requests[rid] = {\"model\": model, \"client_ip\": client_ip, \"token_name\": token_name,")

# Update SSE broadcasting
content = content.replace("\"client_ip\": v[\"client_ip\"],", "\"client_ip\": v[\"client_ip\"], \"token_name\": v.get(\"token_name\", \"\"),")

# Update client_profiles SQL
content = content.replace("SELECT client_ip,", "SELECT token_name,")
content = content.replace("GROUP BY client_ip", "GROUP BY token_name")
content = content.replace("INSERT OR REPLACE INTO client_profiles VALUES (?,?,?,?,?,?,?,?)",
                          "INSERT OR REPLACE INTO client_profiles (token_name, avg_complexity, top_model, peak_hour, tool_use_rate, avg_messages, total_requests, updated_at) VALUES (?,?,?,?,?,?,?,?)")

# Update budgets
content = content.replace("def _check_budget(client_ip: str, is_frontier: bool = False) -> tuple[bool, int, int]:",
                          "def _check_budget(token_name: str, is_frontier: bool = False) -> tuple[bool, int, int]:")
content = content.replace("limit = _get_budget(client_ip, is_frontier)", "limit = _get_budget(token_name, is_frontier)")
content = content.replace("WHERE client_ip=? AND date=?", "WHERE token_name=? AND date=?")
content = content.replace("SELECT client_ip, tokens_used", "SELECT token_name, tokens_used")
content = content.replace("budgets = {r[0]: {\"used\": r[1], \"limit\": _get_budget(r[0])} for r in cur.fetchall()}",
                          "budgets = {r[0]: {\"used\": r[1], \"limit\": _get_budget(r[0])} for r in cur.fetchall()}")
content = content.replace("def _add_budget_usage(client_ip: str, tokens: int, is_frontier: bool = False):",
                          "def _add_budget_usage(token_name: str, tokens: int, is_frontier: bool = False):")
content = content.replace("INSERT INTO budgets (client_ip, date, tokens_used", "INSERT INTO budgets (token_name, date, tokens_used")
content = content.replace("ON CONFLICT(client_ip, date)", "ON CONFLICT(token_name, date)")
content = content.replace("[client_ip, today, tokens, tokens, tokens, tokens]", "[token_name, today, tokens, tokens, tokens, tokens]")
content = content.replace("def _check_budget_warnings(client_ip: str, is_frontier: bool = False):",
                          "def _check_budget_warnings(token_name: str, is_frontier: bool = False):")

fpath.write_text(content)
print("Basic refactoring done.")
