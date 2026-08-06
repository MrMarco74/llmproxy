"""Imports the real dashboard/app.py against a throwaway session secret
and a non-existent DB_PATH (its _db()/_query() helpers already degrade
gracefully -- see dashboard/app.py:181-199 -- when the file doesn't
exist, so tests don't need a real llmproxy.db).

app.py mounts StaticFiles(directory="static") and Jinja2Templates(
directory="templates") using paths relative to the process CWD, so the
import happens with CWD temporarily switched to dashboard/.
"""
import os
import sys
from pathlib import Path

import pytest

_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"


@pytest.fixture(scope="session")
def dashboard_module():
    os.environ.setdefault("LLMPROXY_SESSION_SECRET", "test-session-secret")
    os.environ.setdefault("LLMPROXY_STATUS_URL", "https://llmproxy.test:11435")
    os.environ.setdefault("DB_PATH", "/nonexistent/llmproxy-test.db")

    original_cwd = os.getcwd()
    sys.path.insert(0, str(_DASHBOARD_DIR))
    os.chdir(_DASHBOARD_DIR)
    try:
        import app  # noqa: E402
    finally:
        os.chdir(original_cwd)

    # app.py's StaticFiles/Jinja2Templates were constructed with the
    # relative paths "static"/"templates", resolved against the CWD at
    # import time. Rewrite them to absolute paths so template/static
    # lookups keep working once CWD reverts to whatever pytest was
    # invoked from.
    app.templates.env.loader.searchpath = [str(_DASHBOARD_DIR / "templates")]
    return app
