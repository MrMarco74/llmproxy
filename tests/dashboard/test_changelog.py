"""Verifies the changelog page's collapse/expand markup: the newest 3
entries render outside the collapsible wrapper, everything else inside
it, starting hidden.
"""
import httpx
import respx
from fastapi.testclient import TestClient


def _login_viewer(client, dashboard_module):
    with respx.mock(base_url=dashboard_module.PROXY_URL) as mock:
        mock.post("/admin/auth/verify").mock(
            return_value=httpx.Response(200, json={"ok": True, "role": "viewer"})
        )
        client.post("/login", data={"username": "u", "password": "p", "next": "/"},
                     follow_redirects=False)


def test_changelog_wraps_older_entries_in_hidden_container(dashboard_module):
    client = TestClient(dashboard_module.app, base_url="http://testserver")
    _login_viewer(client, dashboard_module)
    r = client.get("/changelog")
    assert r.status_code == 200
    html = r.text

    assert 'id="changelog-older" class="hidden' in html
    assert 'onclick="toggleOlderChangelog()"' in html

    newest_idx = html.index("v2.18.0")
    older_wrapper_idx = html.index('id="changelog-older"')
    older_entry_idx = html.index("v2.15.0")
    assert newest_idx < older_wrapper_idx < older_entry_idx
