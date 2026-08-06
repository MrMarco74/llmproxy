"""Imports the real proxy/llmproxy.py module against an isolated tmp
CONFIG_DIR/DB_PATH (via the LLMPROXY_CONFIG_DIR/LLMPROXY_DB_PATH env vars
the module reads at import time) instead of the real production paths
(/opt/llmproxy, /var/lib/llmproxy) -- so tests never touch or require a
real llmproxy install.

The one genuinely heavy dependency is presidio_analyzer (pulls in spacy
+ language models at import time), which the module imports and
instantiates unconditionally at module load. It's stubbed out here since
none of these tests exercise DLP-entity detection itself -- they cover
the guardrail rule engine, RBAC, and chargeback math around it.
"""
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

_PROXY_DIR = Path(__file__).resolve().parents[2] / "proxy"


def _stub_presidio():
    if "presidio_analyzer" in sys.modules:
        return

    class _StubAnalyzerEngine:
        def analyze(self, text, language="en", entities=None):
            return []

    stub = types.ModuleType("presidio_analyzer")
    stub.AnalyzerEngine = _StubAnalyzerEngine
    sys.modules["presidio_analyzer"] = stub


def _init_db(db_path: Path):
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL,
            created_at TEXT, last_login_at TEXT, disabled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY, key_id TEXT UNIQUE NOT NULL,
            secret_hash TEXT NOT NULL, owner_type TEXT NOT NULL,
            owner_name TEXT NOT NULL, role TEXT NOT NULL,
            created_at TEXT, last_used_at TEXT, disabled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS secrets (
            name TEXT PRIMARY KEY, value_encrypted BLOB NOT NULL, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS guardrail_events (
            id INTEGER PRIMARY KEY, ts TEXT, token_name TEXT, client_ip TEXT,
            action TEXT, trigger TEXT, rule_pattern TEXT, snippet TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY, ts TEXT, date TEXT, model TEXT,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, token_name TEXT, client_ip TEXT,
            status_code INTEGER DEFAULT 200, is_frontier INTEGER DEFAULT 0,
            prompt_text TEXT, response_text TEXT
        );
        CREATE TABLE IF NOT EXISTS budgets (
            token_name TEXT, date TEXT, tokens_used INTEGER DEFAULT 0,
            tokens_used_local INTEGER DEFAULT 0, tokens_used_frontier INTEGER DEFAULT 0,
            spend_usd_frontier REAL DEFAULT 0,
            PRIMARY KEY (token_name, date)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY, ts TEXT, event TEXT, title TEXT,
            message TEXT, priority TEXT, read_at TEXT
        );
    """)
    con.commit()
    con.close()


@pytest.fixture(scope="session")
def llmproxy_module():
    tmp_dir = Path(tempfile.mkdtemp(prefix="llmproxy-test-"))
    db_path = tmp_dir / "llmproxy.db"
    _init_db(db_path)

    os.environ["LLMPROXY_CONFIG_DIR"] = str(tmp_dir)
    os.environ["LLMPROXY_DB_PATH"] = str(db_path)
    os.environ.setdefault("LLMPROXY_GPU_HOST", "gpu-host.test")

    _stub_presidio()

    sys.path.insert(0, str(_PROXY_DIR))
    import llmproxy  # noqa: E402  (import must follow env/stub setup above)
    return llmproxy
