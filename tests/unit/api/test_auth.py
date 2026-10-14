"""verify_api_key / verify_admin_key 鉴权语义测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oce import auth
from oce.auth import verify_admin_key, verify_api_key


def _settings(*, api_key: str, admin_api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(api_key=api_key, admin_api_key=admin_api_key)


def _tenants(monkeypatch, keys: dict[str, str]) -> list[str]:
    """替换租户 key 查询（避免装配真实容器），返回被查询过的 key。"""
    looked_up: list[str] = []

    async def lookup(api_key: str) -> str | None:
        looked_up.append(api_key)
        return keys.get(api_key)

    monkeypatch.setattr(auth, "_lookup_tenant", lookup)
    return looked_up


async def test_env_api_key_maps_to_default_tenant(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))
    looked_up = _tenants(monkeypatch, {})
    assert await verify_api_key("Bearer sk-main") == "default"
    assert looked_up == []
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer sk-wrong")
    assert exc.value.status_code == 401


async def test_tenant_key_maps_to_its_tenant(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))
    _tenants(monkeypatch, {"oce_tenant-a": "tenant-a"})
    assert await verify_api_key("Bearer oce_tenant-a") == "tenant-a"


async def test_empty_key_rejected_when_env_api_key_disabled(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key=""))
    looked_up = _tenants(monkeypatch, {"": "default", "oce_tenant-a": "tenant-a"})
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer ")
    assert exc.value.status_code == 401
    assert looked_up == []
    assert await verify_api_key("Bearer oce_tenant-a") == "tenant-a"


async def test_non_ascii_key_is_rejected_not_crashed(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))
    _tenants(monkeypatch, {})
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer sk-mäin")
    assert exc.value.status_code == 401


async def test_admin_key_falls_back_to_api_key_when_unset(monkeypatch):
    # 未配置 ADMIN_API_KEY：admin 接口回落 API_KEY
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", admin_api_key="")
    )
    assert await verify_admin_key("Bearer sk-main") == "sk-main"


async def test_admin_key_is_exclusive_once_configured(monkeypatch):
    # 配置 ADMIN_API_KEY 后：只认 admin key，普通 API_KEY 不再放行
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: _settings(api_key="sk-main", admin_api_key="sk-admin"),
    )
    assert await verify_admin_key("Bearer sk-admin") == "sk-admin"
    with pytest.raises(HTTPException) as exc:
        await verify_admin_key("Bearer sk-main")
    assert exc.value.status_code == 401


async def test_admin_key_missing_header_rejected(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", admin_api_key="sk-admin")
    )
    with pytest.raises(HTTPException) as exc:
        await verify_admin_key(None)
    assert exc.value.status_code == 401


async def test_admin_rejects_empty_key_when_no_key_configured(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="", admin_api_key="")
    )
    for header in ("Bearer ", "Bearer anything"):
        with pytest.raises(HTTPException) as exc:
            await verify_admin_key(header)
        assert exc.value.status_code == 401


async def test_admin_never_accepts_tenant_key(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", admin_api_key="")
    )
    _tenants(monkeypatch, {"oce_tenant-a": "tenant-a"})
    with pytest.raises(HTTPException) as exc:
        await verify_admin_key("Bearer oce_tenant-a")
    assert exc.value.status_code == 401
