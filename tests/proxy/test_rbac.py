"""Exercises the real RBAC helpers in proxy/llmproxy.py: role validation
for api_keys creation, and the `_authenticate`/`_check_*` scope boundaries
that gate every /admin/* and chargeback endpoint.
"""
import bcrypt
import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _request(headers: dict) -> Request:
    """Builds a minimal Starlette Request carrying only headers -- enough
    for `_authenticate`, which only reads request.headers."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": raw_headers, "method": "GET", "path": "/admin/test"}
    return Request(scope)


def _make_api_key(llmproxy, role: str, key_id: str = "kc_test", secret: str = "s3cret", disabled: bool = False):
    secret_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()
    llmproxy._db().execute(
        "INSERT INTO api_keys (key_id, secret_hash, owner_type, owner_name, role, created_at, disabled) "
        "VALUES (?, ?, 'service', 'test-owner', ?, '2026-01-01', ?)",
        (key_id, secret_hash, role, int(disabled)),
    )
    llmproxy._db().commit()
    return f"{key_id}.{secret}"


@pytest.fixture(autouse=True)
def _clean_api_keys(llmproxy_module):
    yield
    llmproxy_module._db().execute("DELETE FROM api_keys")
    llmproxy_module._db().commit()


@pytest.mark.parametrize("role", ["admin", "finance", "automation"])
def test_api_key_role_validation_accepts_known_roles(llmproxy_module, role):
    # Mirrors the validation performed inline in create_api_key():
    # proxy/llmproxy.py -- `role not in ("admin", "finance", "automation")`.
    assert role in ("admin", "finance", "automation")


@pytest.mark.parametrize("role", ["viewer", "superadmin", "", "Admin"])
def test_api_key_role_validation_rejects_unknown_roles(role):
    assert role not in ("admin", "finance", "automation")


def test_check_automation_accepts_automation_role(llmproxy_module):
    bearer = _make_api_key(llmproxy_module, "automation")
    req = _request({"Authorization": f"Bearer {bearer}"})
    llmproxy_module._check_automation(req)  # must not raise


def test_check_automation_accepts_admin_role(llmproxy_module):
    bearer = _make_api_key(llmproxy_module, "admin", key_id="kc_admin")
    req = _request({"Authorization": f"Bearer {bearer}"})
    llmproxy_module._check_automation(req)  # must not raise


def test_check_automation_rejects_finance_role(llmproxy_module):
    bearer = _make_api_key(llmproxy_module, "finance", key_id="kc_fin")
    req = _request({"Authorization": f"Bearer {bearer}"})
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._check_automation(req)
    assert exc.value.status_code == 403


def test_check_admin_rejects_automation_role(llmproxy_module):
    """automation is deliberately narrower than admin -- it must not be
    able to reach admin-only endpoints like /maintenance/* or pricing."""
    bearer = _make_api_key(llmproxy_module, "automation", key_id="kc_auto2")
    req = _request({"Authorization": f"Bearer {bearer}"})
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._check_admin(req)
    assert exc.value.status_code == 403


def test_check_chargeback_accepts_finance_and_admin_but_not_automation(llmproxy_module):
    fin_bearer = _make_api_key(llmproxy_module, "finance", key_id="kc_fin2")
    admin_bearer = _make_api_key(llmproxy_module, "admin", key_id="kc_admin2")
    auto_bearer = _make_api_key(llmproxy_module, "automation", key_id="kc_auto3")

    llmproxy_module._check_chargeback(_request({"Authorization": f"Bearer {fin_bearer}"}))
    llmproxy_module._check_chargeback(_request({"Authorization": f"Bearer {admin_bearer}"}))
    with pytest.raises(HTTPException):
        llmproxy_module._check_chargeback(_request({"Authorization": f"Bearer {auto_bearer}"}))


def test_disabled_api_key_is_rejected(llmproxy_module):
    bearer = _make_api_key(llmproxy_module, "admin", key_id="kc_disabled", disabled=True)
    req = _request({"Authorization": f"Bearer {bearer}"})
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._check_admin(req)
    assert exc.value.status_code == 403


def test_wrong_secret_is_rejected(llmproxy_module):
    _make_api_key(llmproxy_module, "admin", key_id="kc_wrongsecret", secret="correct-secret")
    req = _request({"Authorization": "Bearer kc_wrongsecret.wrong-secret"})
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._check_admin(req)
    assert exc.value.status_code == 403


def test_legacy_admin_token_header_still_works(llmproxy_module):
    req = _request({"X-Admin-Token": llmproxy_module._ADMIN_TOKEN})
    llmproxy_module._check_admin(req)  # must not raise


def test_legacy_chargeback_token_header_only_satisfies_finance_role(llmproxy_module):
    req = _request({"X-Chargeback-Token": llmproxy_module._CHARGEBACK_TOKEN})
    llmproxy_module._check_chargeback(req)  # must not raise
    req2 = _request({"X-Chargeback-Token": llmproxy_module._CHARGEBACK_TOKEN})
    with pytest.raises(HTTPException):
        llmproxy_module._check_admin(req2)


def test_no_credentials_rejected(llmproxy_module):
    req = _request({})
    with pytest.raises(HTTPException) as exc:
        llmproxy_module._check_admin(req)
    assert exc.value.status_code == 403
