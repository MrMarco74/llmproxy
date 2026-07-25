#!/usr/bin/env python3
import json
import sqlite3
import yaml
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = Path("/var/lib/llmproxy/llmproxy.db")
CONFIG_DIR = BASE_DIR / "config"

def load_guardrails():
    cfg_path = CONFIG_DIR / "guardrails.yaml"
    if not cfg_path.exists():
        return []
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("rules", [])

def simulate(days: int = 7):
    rules = load_guardrails()
    if not rules:
        print("No rules found in guardrails.yaml.")
        return

    print(f"Loaded {len(rules)} rules. Running simulation on past {days} days of failures/requests...")
    
    con = sqlite3.connect(str(DB_PATH))
    prompts = []
    
    try:
        cur = con.execute("SELECT token_name, prompt_text FROM requests WHERE date >= date('now', ?) AND prompt_text IS NOT NULL", [f"-{days} days"])
        for row in cur:
            if row[1]:
                prompts.append({"token": row[0], "text": row[1], "source": "requests"})
    except Exception as e:
        print(f"Warn: could not fetch requests: {e}")
        
    try:
        cur = con.execute("SELECT token_name, last_user_message FROM failures WHERE ts >= datetime('now', ?) AND last_user_message IS NOT NULL", [f"-{days} days"])
        for row in cur:
            if row[1]:
                prompts.append({"token": row[0], "text": row[1], "source": "failures"})
    except Exception as e:
        print(f"Warn: could not fetch failures: {e}")
        
    print(f"Found {len(prompts)} prompts to simulate.")
    
    stats = {"pass": 0, "deny": 0, "silent": 0, "rewrite": 0, "redirect": 0, "shadow": 0}
    rule_hits = {rule.get("pattern", "dlp"): 0 for rule in rules}
    
    for p in prompts:
        text = p["text"]
        token = p["token"]
        text_lower = text.lower()
        
        hit_action = "pass"
        for rule in rules:
            trigger = rule.get("trigger", "keyword")
            pattern = rule.get("pattern", "")
            action = rule.get("action", "pass")
            mode = rule.get("mode", "enforce")
            
            hit = False
            if trigger == "dlp":
                if re.search(r'\b\d{4}-\d{4}-\d{4}-\d{4}\b', text) or re.search(r'\b\d{3}-\d{2}-\d{4}\b', text):
                    hit = True
            elif trigger == "keyword":
                if pattern.lower() in text_lower:
                    hit = True
            elif trigger == "regex":
                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        hit = True
                except Exception:
                    pass
                    
            if hit:
                rule_hits[pattern if trigger != "dlp" else "dlp"] += 1
                if mode == "shadow":
                    stats["shadow"] += 1
                    continue
                hit_action = action
                if action in ("deny", "silent"):
                    break
                
        stats[hit_action] += 1
        
    print("\nSimulation Results:")
    print("-------------------")
    for action, count in stats.items():
        print(f"{action.upper():<10}: {count} ({count/len(prompts)*100:.1f}%)" if len(prompts) > 0 else f"{action.upper():<10}: 0")
        
    print("\nRule Hits:")
    print("----------")
    for pattern, count in rule_hits.items():
        print(f"{pattern:<20}: {count}")

if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    simulate(days)
