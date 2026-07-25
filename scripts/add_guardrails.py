import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

guardrail_code = """
_guardrails_cfg = _load_yaml("guardrails.yaml", {"enabled": False, "global_blocklist": [], "client_overrides": {}})

def _check_safeguards(prompt_text: str, token_name: str) -> str | None:
    if not _guardrails_cfg.get("enabled", False):
        return None
        
    global_rules = _guardrails_cfg.get("global_blocklist", [])
    client_rules = _guardrails_cfg.get("client_overrides", {}).get(token_name, [])
    all_rules = global_rules + client_rules
    
    prompt_lower = prompt_text.lower()
    for rule in all_rules:
        # Simple substring or regex match (assuming string for now, or regex if starts with ^)
        if rule.startswith("^") or rule.startswith("("):
            try:
                import re
                if re.search(rule, prompt_text, re.IGNORECASE):
                    return f"Triggered regex rule: {rule}"
            except Exception:
                pass
        else:
            if rule.lower() in prompt_lower:
                return f"Triggered keyword rule: {rule}"
    return None

async def _guard_safeguards(request: Request):
    if request.url.path not in ["/api/chat", "/api/generate", "/v1/chat/completions"]:
        return
        
    try:
        body = await request.json()
    except Exception:
        return
        
    prompt_text = _extract_last_user_message(body)
    if not prompt_text:
        return
        
    token_name = _get_token_name(request)
    client_ip = _get_client_ip(request)
    
    violation = _check_safeguards(prompt_text, token_name)
    if violation:
        _db_log_failure(model=body.get("model", "unknown"), client_ip=client_ip, token_name=token_name,
                        endpoint=request.url.path, status_code=403, failure_reason="guardrail_blocked",
                        last_user_message=prompt_text)
        _log_to_splunk("guardrail_violation", {
            "token_name": token_name,
            "client_ip": client_ip,
            "violation": violation,
            "prompt_snippet": prompt_text[:200]
        })
        raise HTTPException(status_code=403, detail={"error": "Request blocked by guardrails", "violation": violation})
"""

# Insert _guardrails_cfg loader
content = content.replace("_fallback_cfg     = _load_yaml(\"fallback.yaml\",        {\"enabled\": False, \"mapping\": {}})",
                          "_fallback_cfg     = _load_yaml(\"fallback.yaml\",        {\"enabled\": False, \"mapping\": {}})\n" + guardrail_code)

# Add Depends(_guard_safeguards) to endpoints
content = content.replace("dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_ollama_lock)])",
                          "dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_ollama_lock), Depends(_guard_safeguards)])")
content = content.replace("dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked)])",
                          "dependencies=[Depends(_guard_stop_all), Depends(_guard_gaming), Depends(_guard_blocked), Depends(_guard_safeguards)])")

# Admin endpoints for guardrails
admin_endpoints = """
@app.get("/admin/guardrails")
async def get_guardrails_config(request: Request):
    _check_admin(request)
    return _guardrails_cfg

@app.post("/admin/guardrails")
async def set_guardrails_config(request: Request):
    _check_admin(request)
    global _guardrails_cfg
    try:
        data = await request.json()
        _guardrails_cfg = data
        with open(CONFIG_DIR / "guardrails.yaml", "w") as f:
            yaml.safe_dump(_guardrails_cfg, f)
        return {"ok": True, "config": _guardrails_cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)}
"""
content = content.replace("@app.get(\"/admin/actions\")", admin_endpoints + "\n@app.get(\"/admin/actions\")")

fpath.write_text(content)
print("Guardrails added.")
