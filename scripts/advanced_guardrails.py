import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

new_guardrail_code = """
_guardrails_cfg = _load_yaml("guardrails.yaml", {"enabled": False, "rules": []})
_fail2ban_cfg = _load_yaml("fail2ban.yaml", {"bans": {}})

def _dlp_mask(text: str) -> str:
    # TODO: Connect to Microsoft Presidio or local NLP
    # For now, simulate by replacing standard regexes
    import re
    text = re.sub(r'\b\d{4}-\d{4}-\d{4}-\d{4}\b', '[CREDIT_CARD]', text)
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN]', text)
    return text

def _is_banned(token_name: str) -> bool:
    ban_time = _fail2ban_cfg.get("bans", {}).get(token_name)
    if ban_time:
        import time
        if time.time() < ban_time:
            return True
        else:
            del _fail2ban_cfg["bans"][token_name]
            with open(CONFIG_DIR / "fail2ban.yaml", "w") as f:
                yaml.safe_dump(_fail2ban_cfg, f)
    return False

def _record_violation(token_name: str):
    # Track violations in a global dict (memory only for simplicity, or db)
    # If > 10 in 5 mins -> ban
    if not hasattr(_record_violation, "strikes"):
        _record_violation.strikes = {}
    import time
    now = time.time()
    strikes = _record_violation.strikes.setdefault(token_name, [])
    strikes = [s for s in strikes if now - s < 300]
    strikes.append(now)
    _record_violation.strikes[token_name] = strikes
    
    if len(strikes) >= 10:
        _fail2ban_cfg.setdefault("bans", {})[token_name] = now + 3600
        with open(CONFIG_DIR / "fail2ban.yaml", "w") as f:
            yaml.safe_dump(_fail2ban_cfg, f)
        _log_to_splunk("guardrail_fail2ban", {"token_name": token_name, "duration": 3600})

def _run_guardrails(prompt_text: str, token_name: str, body: dict) -> tuple[bool, str|None, str, dict]:
    \"\"\"Returns (modified, violation_msg, action, new_body)\"\"\"
    if not _guardrails_cfg.get("enabled", False):
        return False, None, "pass", body
        
    if _is_banned(token_name):
        return False, "Token is banned (Fail2Ban)", "deny", body
        
    rules = _guardrails_cfg.get("rules", [])
    prompt_lower = prompt_text.lower()
    
    modified = False
    new_body = body.copy()
    
    for rule in rules:
        trigger = rule.get("trigger", "keyword")
        pattern = rule.get("pattern", "")
        action = rule.get("action", "pass")
        mode = rule.get("mode", "enforce")
        
        hit = False
        if trigger == "dlp":
            masked = _dlp_mask(prompt_text)
            if masked != prompt_text:
                hit = True
                if action == "rewrite" and mode == "enforce":
                    prompt_text = masked
                    modified = True
                    
        elif trigger == "keyword":
            if pattern.lower() in prompt_lower:
                hit = True
                
        elif trigger == "regex":
            import re
            if re.search(pattern, prompt_text, re.IGNORECASE):
                hit = True
                
        if hit:
            msg = f"Triggered {trigger}: {pattern}"
            if mode == "shadow":
                _log_to_splunk("guardrail_shadow_violation", {"token_name": token_name, "rule": rule, "snippet": prompt_text[:200]})
                continue
                
            if action in ("deny", "silent"):
                _record_violation(token_name)
                return False, msg, action, new_body
                
            if action == "redirect":
                new_body["model"] = rule.get("target_model", "local-fallback")
                modified = True
                _log_to_splunk("guardrail_redirected", {"token_name": token_name, "from": body.get("model"), "to": new_body["model"]})
                # continue processing other rules
                
    if modified:
        # Update prompt inside new_body (assuming single last message structure for simplicity)
        if "messages" in new_body and isinstance(new_body["messages"], list):
            new_body["messages"][-1]["content"] = prompt_text
        elif "prompt" in new_body:
            new_body["prompt"] = prompt_text
        _log_to_splunk("guardrail_rewritten", {"token_name": token_name})
            
    return modified, None, "pass", new_body

async def _guard_safeguards(request: Request):
    if request.url.path not in ["/api/chat", "/api/generate", "/v1/chat/completions"]:
        return
        
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes)
    except Exception:
        return
        
    prompt_text = _extract_last_user_message(body)
    if not prompt_text:
        return
        
    token_name = _get_token_name(request)
    client_ip = _get_client_ip(request)
    
    modified, violation, action, new_body = _run_guardrails(prompt_text, token_name, body)
    
    if action in ("deny", "silent"):
        status = 403 if action == "deny" else 200
        detail = {"error": "Request blocked by guardrails", "violation": violation} if action == "deny" else {"status": "ok"}
        
        _db_log_failure(model=body.get("model", "unknown"), client_ip=client_ip, token_name=token_name,
                        endpoint=request.url.path, status_code=status, failure_reason="guardrail_blocked",
                        last_user_message=prompt_text)
        _log_to_splunk("guardrail_violation", {
            "token_name": token_name, "client_ip": client_ip, "violation": violation, "action": action
        })
        raise HTTPException(status_code=status, detail=detail)
        
    if modified:
        request._body = json.dumps(new_body).encode("utf-8")
"""

content = re.sub(
    r'_guardrails_cfg = _load_yaml\("guardrails\.yaml", .*?async def _guard_safeguards\(request: Request\):.*?raise HTTPException\(.*?violation\}\)',
    new_guardrail_code,
    content,
    flags=re.DOTALL
)

fpath.write_text(content)
print("Advanced guardrails updated.")
