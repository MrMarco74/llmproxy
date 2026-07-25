import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

# Add splunk logger initialization
splunk_logger_code = """
# Splunk JSON Logger
import logging.handlers

splunk_logger = logging.getLogger("llmproxy_splunk")
splunk_logger.setLevel(logging.INFO)
splunk_logger.propagate = False
try:
    splunk_handler = logging.handlers.WatchedFileHandler("/var/lib/llmproxy/audit.log")
    splunk_handler.setFormatter(logging.Formatter("%(message)s"))
    splunk_logger.addHandler(splunk_handler)
except Exception as e:
    logger.error(f"Failed to initialize Splunk logger: {e}")

def _log_to_splunk(event_type: str, data: dict):
    if not _logging_cfg.get("enabled", True):
        return
    payload = {
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": event_type,
        "app": "llmproxy",
    }
    payload.update(data)
    try:
        splunk_logger.info(json.dumps(payload))
    except Exception as e:
        logger.error(f"[splunk] log error: {e}")
"""

content = content.replace("logger = logging.getLogger(\"llmproxy\")", "logger = logging.getLogger(\"llmproxy\")\n" + splunk_logger_code)

# Add to _db_insert_request
db_insert_logic = """
    try:
        _db().execute(f"INSERT INTO requests ({cols}) VALUES ({placeholders})", list(kw.values()))
        _db().commit()
    except Exception as e:
        logger.error(f"[db] insert error: {e}")
"""
splunk_insert_logic = db_insert_logic + "\n    _log_to_splunk(\"request_completed\", kw)\n"
content = content.replace(db_insert_logic, splunk_insert_logic)

# Add to _db_log_failure
db_log_failure_logic = """
    try:
        _db().execute(
            "INSERT INTO failures (ts, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [now, model, client_ip, token_name, endpoint, status_code, failure_reason, last_user_message[:500]]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[db] failure log error: {e}")
"""
splunk_failure_logic = db_log_failure_logic + """
    _log_to_splunk("request_failed", {
        "model": model, "client_ip": client_ip, "token_name": token_name,
        "endpoint": endpoint, "status_code": status_code, "failure_reason": failure_reason,
        "last_user_message": last_user_message[:500]
    })
"""
content = content.replace(db_log_failure_logic, splunk_failure_logic)

# Add to _log_admin_action
db_admin_logic = """
    try:
        _db().execute(
            "INSERT INTO admin_actions (ts, action, source, client_ip, token_name, detail) VALUES (?, ?, ?, ?, ?, ?)",
            [now, action, source, client_ip, token_name, detail]
        )
        _db().commit()
    except Exception as e:
        logger.error(f"[db] admin_actions log error: {e}")
"""
splunk_admin_logic = db_admin_logic + """
    _log_to_splunk("admin_action", {
        "action": action, "source": source, "client_ip": client_ip, 
        "token_name": token_name, "detail": detail
    })
"""
content = content.replace(db_admin_logic, splunk_admin_logic)

fpath.write_text(content)
print("Splunk logger added.")
