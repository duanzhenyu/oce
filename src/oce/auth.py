"""HTTP Bearer 鉴权。"""

import hmac

from fastapi import Header, HTTPException

from oce.application.container import get_container
from oce.domain.tenant import DEFAULT_TENANT_ID
from oce.shared.config import get_settings


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
    )


def _extract_bearer(authorization: str | None) -> str:
    """从 ``Authorization: Bearer <key>`` 取出 key，缺失或格式错误抛 401。"""
    if authorization is None:
        raise _unauthorized("You didn't provide an API key.")
    if not authorization.startswith("Bearer "):
        raise _unauthorized("Invalid API key format. Expected 'Bearer <key>'")
    return authorization.removeprefix("Bearer ")


def _key_matches(key: str, expected: str) -> bool:
    # 空 expected 表示该入口已关闭：compare_digest("", "") 为 True，必须先判空。
    # 按 bytes 比较：str 版 compare_digest 遇到非 ASCII 会抛 TypeError。
    return bool(expected) and hmac.compare_digest(
        key.encode("utf-8"), expected.encode("utf-8")
    )


async def _lookup_tenant(api_key: str) -> str | None:
    """按租户 key 查 active 租户。不缓存：禁用租户或轮换 key 后立即生效。"""
    return await get_container().application.authenticate(api_key)


async def verify_api_key(authorization: str | None = Header(default=None)) -> str:
    """校验数据面 ``Authorization: Bearer <key>``，返回 tenant_id。

    env API_KEY 映射到内置 default 租户（置空即关闭该入口），其余 key 按租户表查。
    """
    api_key = _extract_bearer(authorization)
    if not api_key:
        raise _unauthorized("Invalid API key provided")
    if _key_matches(api_key, get_settings().api_key):
        return DEFAULT_TENANT_ID
    tenant_id = await _lookup_tenant(api_key)
    if tenant_id is None:
        raise _unauthorized("Invalid API key provided")
    return tenant_id


async def verify_admin_key(authorization: str | None = Header(default=None)) -> str:
    """校验 admin ``Authorization: Bearer <key>``。

    未配置 ADMIN_API_KEY 时回落到 API_KEY（个人模式零配置仍可访问）；一旦配置了
    ADMIN_API_KEY，则只认 admin key，普通 API_KEY 不再放行。租户 key 永远不能访问
    admin；两者都为空时 admin 接口整体关闭。
    """
    settings = get_settings()
    key = _extract_bearer(authorization)
    if not _key_matches(key, settings.admin_api_key or settings.api_key):
        raise _unauthorized("Invalid admin key provided")
    return key
