import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

# Rename client_id to token_name everywhere except def _get_client_id (I'll rename that too)
content = content.replace("def _get_client_id(request: Request) -> str:", "def _get_token_name(request: Request) -> str:")
content = content.replace("client_id = _get_client_id(request)", "token_name = _get_token_name(request)")
content = content.replace("client_id = _tokens_cfg.get(\"tokens\", {}).get(token)", "token_name = _tokens_cfg.get(\"tokens\", {}).get(token)")
content = content.replace("if not client_id:", "if not token_name:")
content = content.replace("return client_id", "return token_name")

# Endpoint callers
content = content.replace("_rid = _req_start(model, client_ip, path", "_rid = _req_start(model, client_ip, token_name, path")
content = content.replace("_rid   = _req_start(model, client_ip, \"/v1/chat/completions\"", "_rid   = _req_start(model, client_ip, token_name, \"/v1/chat/completions\"")

# db_insert_request
content = content.replace("client_ip=client_ip, user_agent=ua", "client_ip=client_ip, token_name=token_name, user_agent=ua")

# log_failure
content = content.replace("client_ip=client_ip, endpoint=path", "client_ip=client_ip, token_name=token_name, endpoint=path")
content = content.replace("client_ip=client_ip, endpoint=\"/v1/chat/completions\"", "client_ip=client_ip, token_name=token_name, endpoint=\"/v1/chat/completions\"")

# admin actions
content = content.replace("_log_admin_action(\"logging\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"logging\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"cleanup\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"cleanup\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"strip-prompts\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"strip-prompts\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"evict-models\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"evict-models\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"force-purge\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"force-purge\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"stop-all\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"stop-all\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"resume\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"resume\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("_log_admin_action(\"gaming_override\", \"admin\", _get_client_ip(request)", "_log_admin_action(\"gaming_override\", \"admin\", _get_client_ip(request), _get_token_name(request)")
content = content.replace("client_ip=_get_client_ip(request)", "client_ip=_get_client_ip(request), token_name=_get_token_name(request)")

# check_budget_warnings and add_budget_usage
content = content.replace("_add_budget_usage(client_id", "_add_budget_usage(token_name")
content = content.replace("_check_budget_warnings(client_id", "_check_budget_warnings(token_name")
content = content.replace("_guard_budget_sync(client_id", "_guard_budget_sync(token_name")
content = content.replace("client_cfg = _get_client_config(client_id)", "client_cfg = _get_client_config(token_name)")

# replace other client_id mentions
content = content.replace("client_id=", "token_name=")
content = content.replace("client_id:", "token_name:")

fpath.write_text(content)
print("Endpoint refactoring done.")
