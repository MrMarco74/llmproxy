"""Points llmproxy_mcp.client at a mocked llmproxy HTTP API (via respx)
instead of a real network address -- proxy/llmproxy.py imports heavy DLP
deps (presidio-analyzer, spacy) at module load time that this package's
tests don't need, so we mock the HTTP layer rather than run the real app
in-process (unlike labcontrol_mcp's tests, which can afford an in-process
FastAPI TestClient since labcontrol has no such import weight).
"""

import os

os.environ.setdefault("LLMPROXY_URL", "https://llmproxy.test:11435")
os.environ.setdefault("LLMPROXY_API_KEY", "kc_test123.test-secret-value")

import pytest
import respx


@pytest.fixture
def mock_llmproxy():
    with respx.mock(base_url="https://llmproxy.test:11435") as mock:
        yield mock
