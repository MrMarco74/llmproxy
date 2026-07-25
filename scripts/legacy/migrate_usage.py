import json
import os
import sqlite3
import datetime
import subprocess
from pathlib import Path

# Paths
CCUSAGE_DIR = "/home/mrmarco/Documents/gitlab/ccusage-loc"
CCUSAGE_CACHE = os.path.join(CCUSAGE_DIR, "ccusage_cache.json")
OLLAMA_STATS = os.path.expanduser("~/.ollama_usage_stats.jsonl")
CLAUDE_LOG = os.path.join(CCUSAGE_DIR, ".dual-graph/token_log.jsonl")

DANA_SSH = "root@dana"
REMOTE_DB = "/root/.llmproxy.db"

def run_remote_sql(sqls):
    """Executes a list of SQL statements on dana via Python."""
    # We use a single multi-line string to minimize SSH calls
    script = "import sqlite3; conn = sqlite3.connect('" + REMOTE_DB + "'); cursor = conn.cursor();"
    for sql in sqls:
        # Escape single quotes in SQL for the python command string
        escaped_sql = sql.replace("'", "\\'")
        script += f"cursor.execute('{escaped_sql}');"
    script += "conn.commit(); conn.close();"
    
    cmd = ["ssh", DANA_SSH, f"python3 -c \"{script}\""]
    subprocess.run(cmd, check=True)

def migrate():
    requests_to_insert = []

    # 1. Load Ollama Stats (Aggregated by day/model)
    print("Processing Ollama stats...")
    if os.path.exists(OLLAMA_STATS):
        with open(OLLAMA_STATS, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    date = data["date"]
                    model = data["model"]
                    pt = data.get("prompt_tokens", 0)
                    ct = data.get("completion_tokens", 0)
                    dur = data.get("duration_s", 0.0)
                    ts = f"{date}T12:00:00"
                    
                    requests_to_insert.append({
                        "ts": ts, "date": date, "model": model,
                        "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                        "duration_s": dur, "status_code": 200, "routed_from": "migrated_ollama"
                    })
                except Exception as e:
                    print(f"Error parsing Ollama stat: {e}")

    # 2. Load Claude individual logs (if any)
    # We use these to avoid using aggregated data for the same period if possible
    claude_log_dates = set()
    print("Processing Claude individual logs...")
    if os.path.exists(CLAUDE_LOG):
        with open(CLAUDE_LOG, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    ts = data["timestamp"]
                    date = ts[:10]
                    model = data["model"]
                    pt = data.get("input_tokens", 0) + data.get("cache_creation_input_tokens", 0) + data.get("cache_read_input_tokens", 0)
                    ct = data.get("output_tokens", 0)
                    
                    requests_to_insert.append({
                        "ts": ts, "date": date, "model": model,
                        "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                        "status_code": 200, "routed_from": "migrated_claude_log"
                    })
                    claude_log_dates.add(date)
                except Exception as e:
                    print(f"Error parsing Claude log: {e}")

    # 3. Load ccusage cache (Aggregated history)
    print("Processing ccusage cache...")
    if os.path.exists(CCUSAGE_CACHE):
        with open(CCUSAGE_CACHE, "r") as f:
            cache = json.load(f)
            for entry in cache.get("history", []):
                date = entry["date"]
                # Skip Claude if we already have detailed logs for this date
                if date in claude_log_dates:
                    continue
                
                for mb in entry.get("modelBreakdowns", []):
                    model = mb["modelName"]
                    # Map common names
                    if "sonnet" in model.lower():
                        model = "claude-3-5-sonnet"
                    elif "haiku" in model.lower():
                        model = "claude-3-haiku"
                    elif "opus" in model.lower():
                        model = "claude-3-opus"
                        
                    pt = mb.get("inputTokens", 0) + mb.get("cacheCreationTokens", 0) + mb.get("cacheReadTokens", 0)
                    ct = mb.get("outputTokens", 0)
                    ts = f"{date}T12:00:00"
                    
                    requests_to_insert.append({
                        "ts": ts, "date": date, "model": model,
                        "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                        "status_code": 200, "routed_from": "migrated_ccusage_cache"
                    })

    print(f"Prepared {len(requests_to_insert)} records for migration.")
    
    # Chunking to avoid massive command lines
    CHUNK_SIZE = 50
    for i in range(0, len(requests_to_insert), CHUNK_SIZE):
        chunk = requests_to_insert[i:i+CHUNK_SIZE]
        sqls = []
        for r in chunk:
            cols = ", ".join(r.keys())
            vals = ", ".join([f"'{v}'" if isinstance(v, str) else str(v) for v in r.values()])
            sqls.append(f"INSERT INTO requests ({cols}) VALUES ({vals})")
        
        print(f"Migrating chunk {i//CHUNK_SIZE + 1}...")
        run_remote_sql(sqls)

if __name__ == "__main__":
    migrate()
