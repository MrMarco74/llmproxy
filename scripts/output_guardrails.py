import re
from pathlib import Path

fpath = Path("proxy/llmproxy.py")
content = fpath.read_text()

output_guardrail_code = """
def _check_output_safeguards(text: str, token_name: str) -> bool:
    # Basic output guardrail check (returns True if safe, False if blocked)
    if not _guardrails_cfg.get("enabled", False):
        return True
        
    rules = _guardrails_cfg.get("rules", [])
    text_lower = text.lower()
    for rule in rules:
        if rule.get("trigger") == "output_keyword":
            if rule.get("pattern", "").lower() in text_lower:
                _log_to_splunk("output_guardrail_violation", {"token_name": token_name, "rule": rule, "snippet": text[:200]})
                return False
        elif rule.get("trigger") == "output_dlp":
            if re.search(r'\b\d{4}-\d{4}-\d{4}-\d{4}\b', text): # e.g. leaked credit card
                _log_to_splunk("output_guardrail_dlp_leak", {"token_name": token_name, "snippet": text[:200]})
                return False
    return True
"""

content = content.replace("def _run_guardrails(", output_guardrail_code + "\n\ndef _run_guardrails(")

# Non-streaming override for OpenAI
openai_non_streaming = """
    else:
        content = await resp.aread()
        try:
            resp_json = json.loads(content)
            if "choices" in resp_json and len(resp_json["choices"]) > 0:
                out_text = resp_json["choices"][0].get("message", {}).get("content", "")
                if not _check_output_safeguards(out_text, token_name):
                    return Response(content=json.dumps({"error": "Output blocked by guardrails"}), status_code=403, media_type="application/json")
        except Exception:
            pass
"""
content = content.replace("    else:\n        content = await resp.aread()", openai_non_streaming)

fpath.write_text(content)
print("Output guardrails added.")
