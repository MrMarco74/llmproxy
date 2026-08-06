"""Exercises the real _guard_gaming FastAPI dependency in
proxy/llmproxy.py: a no-op when the GPU host isn't in gaming mode, and a
503 (plus a logged blocked request) when it is.
"""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _fake_request(path: str = "/api/chat", client_ip: str = "9.9.9.9") -> Request:
    scope = {
        "type": "http", "method": "POST", "path": path,
        "headers": [], "client": (client_ip, 12345),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _restore_gaming_mode(llmproxy_module):
    original = llmproxy_module._gaming_mode
    yield
    llmproxy_module._gaming_mode = original
    llmproxy_module._db().execute("DELETE FROM requests")
    llmproxy_module._db().commit()


def test_guard_gaming_noop_when_not_gaming(llmproxy_module):
    llmproxy_module._gaming_mode = False
    asyncio.run(llmproxy_module._guard_gaming(_fake_request()))  # must not raise


def test_guard_gaming_blocks_with_503_when_gaming(llmproxy_module):
    llmproxy_module._gaming_mode = True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(llmproxy_module._guard_gaming(_fake_request()))
    assert exc.value.status_code == 503
    assert exc.value.detail["gaming_mode"] is True
    assert exc.value.headers["Retry-After"] == "60"


def test_guard_gaming_logs_blocked_request(llmproxy_module):
    llmproxy_module._gaming_mode = True
    with pytest.raises(HTTPException):
        asyncio.run(llmproxy_module._guard_gaming(_fake_request(path="/api/chat", client_ip="9.9.9.9")))
    row = llmproxy_module._db().execute(
        "SELECT model, client_ip, endpoint, status_code, gaming_blocked FROM requests "
        "WHERE client_ip = '9.9.9.9'"
    ).fetchone()
    assert row is not None
    assert row[0] == "blocked"
    assert row[2] == "/api/chat"
    assert row[3] == 503
    assert row[4] == 1
