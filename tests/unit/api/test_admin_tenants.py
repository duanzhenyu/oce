"""/admin/tenants CRUD + rotate-key 契约测试。"""

from __future__ import annotations

import httpx
from fastapi import Header

from oce.api.router import get_application
from oce.application.tenant_admin import TenantCreated, TenantRecord
from oce.auth import _unauthorized, verify_admin_key
from oce.main import app
from oce.shared.errors import DefaultTenantImmutableError, TenantConflictError

_AUTH = {"Authorization": "Bearer sk-admin"}
_KEY = "oce_plaintext-key-9876"


async def _mock_admin_auth(authorization: str | None = Header(default=None)) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise _unauthorized("missing admin key")
    return authorization.removeprefix("Bearer ")


def _record(tenant_id: str = "t1", name: str = "team", status: str = "active") -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        name=name,
        status=status,
        api_key_last4=_KEY[-4:],
        created_at=None,
        updated_at=None,
    )


def _guard(tenant_id: str) -> None:
    if tenant_id == "default":
        raise DefaultTenantImmutableError()


class StubTenantApp:
    async def list_tenants(self):
        return [_record("default", "default"), _record()]

    async def create_tenant(self, name):
        if name == "taken":
            raise TenantConflictError()
        return TenantCreated(_record(name=name), _KEY)

    async def update_tenant(self, tenant_id, changes):
        _guard(tenant_id)
        if changes.name == "taken":
            raise TenantConflictError()
        if tenant_id == "ghost":
            return None
        return _record(tenant_id, changes.name or "team", changes.status or "active")

    async def rotate_tenant_key(self, tenant_id):
        _guard(tenant_id)
        return None if tenant_id == "ghost" else TenantCreated(_record(tenant_id), _KEY)

    async def delete_tenant(self, tenant_id):
        _guard(tenant_id)
        return tenant_id != "ghost"


def _client() -> httpx.AsyncClient:
    app.dependency_overrides[get_application] = lambda: StubTenantApp()
    app.dependency_overrides[verify_admin_key] = _mock_admin_auth
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_list_tenants_hides_secrets():
    async with _client() as client:
        response = await client.get("/admin/tenants", headers=_AUTH)
    assert response.status_code == 200
    tenants = response.json()["tenants"]
    assert [tenant["tenant_id"] for tenant in tenants] == ["default", "t1"]
    assert all("api_key" not in tenant and "api_key_hash" not in tenant for tenant in tenants)
    assert _KEY not in response.text


async def test_create_returns_plaintext_key_once():
    async with _client() as client:
        response = await client.post("/admin/tenants", headers=_AUTH, json={"name": "team-x"})
    assert response.status_code == 201
    assert response.json()["api_key"] == _KEY
    assert response.json()["name"] == "team-x"


async def test_create_validation_and_conflict():
    async with _client() as client:
        empty = await client.post("/admin/tenants", headers=_AUTH, json={"name": ""})
        taken = await client.post("/admin/tenants", headers=_AUTH, json={"name": "taken"})
    assert empty.status_code == 422
    assert taken.status_code == 409


async def test_update_tenant_status_codes():
    async with _client() as client:
        ok = await client.patch("/admin/tenants/t1", headers=_AUTH, json={"status": "disabled"})
        bad = await client.patch("/admin/tenants/t1", headers=_AUTH, json={"status": "paused"})
        missing = await client.patch("/admin/tenants/ghost", headers=_AUTH, json={"name": "x"})
        taken = await client.patch("/admin/tenants/t1", headers=_AUTH, json={"name": "taken"})
        default = await client.patch("/admin/tenants/default", headers=_AUTH, json={"name": "x"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "disabled"
    assert "api_key" not in ok.json()
    assert bad.status_code == 422
    assert missing.status_code == 404
    assert taken.status_code == 409
    assert default.status_code == 409


async def test_rotate_key_status_codes():
    async with _client() as client:
        ok = await client.post("/admin/tenants/t1/rotate-key", headers=_AUTH)
        missing = await client.post("/admin/tenants/ghost/rotate-key", headers=_AUTH)
        default = await client.post("/admin/tenants/default/rotate-key", headers=_AUTH)
    assert ok.status_code == 200
    assert ok.json()["api_key"] == _KEY
    assert missing.status_code == 404
    assert default.status_code == 409


async def test_delete_tenant_status_codes():
    async with _client() as client:
        ok = await client.delete("/admin/tenants/t1", headers=_AUTH)
        missing = await client.delete("/admin/tenants/ghost", headers=_AUTH)
        default = await client.delete("/admin/tenants/default", headers=_AUTH)
    assert ok.status_code == 204
    assert missing.status_code == 404
    assert default.status_code == 409


async def test_tenants_require_admin_auth():
    async with _client() as client:
        response = await client.get("/admin/tenants")
    assert response.status_code == 401
