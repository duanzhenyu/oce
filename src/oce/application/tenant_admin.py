"""租户管理（admin）用例：端口、读模型、CRUD/轮换 key 命令与 key 鉴权查询。

租户和凭据一样是运维配置，不进检索 UoW，由独立 TenantAdminStore 端口承载。key 在
这里生成，明文只回给调用方一次；store 只存 sha256 与尾 4 位。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from oce.application.commands.ingest import DeleteBlobsCommand, DeleteBlobsCommandHandler
from oce.application.messages import Command, Query
from oce.application.queue import Queue
from oce.domain.tenant import DEFAULT_TENANT_ID
from oce.shared.errors import DefaultTenantImmutableError

_KEY_PREFIX = "oce_"
_DELETE_BATCH_SIZE = 1_000


@dataclass(frozen=True)
class TenantRecord:
    """回给上层的租户视图：不含明文 key 与 hash，仅暴露尾 4 位。"""

    tenant_id: str
    name: str
    status: str
    api_key_last4: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class TenantCreated:
    """新建租户或轮换 key 的结果：明文 api_key 只在此出现一次。"""

    record: TenantRecord
    api_key: str


@dataclass(frozen=True)
class TenantUpdate:
    """部分更新：字段为 None 表示不改。"""

    name: str | None = None
    status: str | None = None


class TenantAdminStore(Protocol):
    async def list(self) -> list[TenantRecord]: ...
    async def create(self, name: str, api_key: str) -> TenantRecord: ...
    async def update(
        self, tenant_id: str, changes: TenantUpdate
    ) -> TenantRecord | None: ...
    async def rotate_key(self, tenant_id: str, api_key: str) -> TenantRecord | None: ...
    async def delete(self, tenant_id: str) -> list[str] | None:
        """删除租户及其 chain 与归属，返回已无任何租户拥有的 blob；None 表示不存在。"""
        ...
    async def authenticate(self, api_key: str) -> str | None:
        """按 key 的 hash 查 active 租户，返回 tenant_id。"""
        ...


@dataclass(frozen=True)
class ListTenantsQuery(Query):
    pass


@dataclass(frozen=True)
class AuthenticateTenantQuery(Query):
    api_key: str


@dataclass(frozen=True)
class CreateTenantCommand(Command):
    name: str


@dataclass(frozen=True)
class UpdateTenantCommand(Command):
    tenant_id: str
    changes: TenantUpdate


@dataclass(frozen=True)
class RotateTenantKeyCommand(Command):
    tenant_id: str


@dataclass(frozen=True)
class DeleteTenantCommand(Command):
    tenant_id: str


def _new_api_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _ensure_mutable(tenant_id: str) -> None:
    if tenant_id == DEFAULT_TENANT_ID:
        raise DefaultTenantImmutableError()


class ListTenantsQueryHandler:
    def __init__(self, store: TenantAdminStore) -> None:
        self._store = store

    async def handle(self, _query: ListTenantsQuery) -> list[TenantRecord]:
        return await self._store.list()


class AuthenticateTenantQueryHandler:
    def __init__(self, store: TenantAdminStore) -> None:
        self._store = store

    async def handle(self, query: AuthenticateTenantQuery) -> str | None:
        return await self._store.authenticate(query.api_key)


class CreateTenantCommandHandler:
    def __init__(self, store: TenantAdminStore) -> None:
        self._store = store

    async def handle(self, command: CreateTenantCommand) -> TenantCreated:
        api_key = _new_api_key()
        return TenantCreated(await self._store.create(command.name, api_key), api_key)


class UpdateTenantCommandHandler:
    def __init__(self, store: TenantAdminStore) -> None:
        self._store = store

    async def handle(self, command: UpdateTenantCommand) -> TenantRecord | None:
        _ensure_mutable(command.tenant_id)
        return await self._store.update(command.tenant_id, command.changes)


class RotateTenantKeyCommandHandler:
    def __init__(self, store: TenantAdminStore) -> None:
        self._store = store

    async def handle(self, command: RotateTenantKeyCommand) -> TenantCreated | None:
        _ensure_mutable(command.tenant_id)
        api_key = _new_api_key()
        record = await self._store.rotate_key(command.tenant_id, api_key)
        return None if record is None else TenantCreated(record, api_key)


class DeleteTenantCommandHandler:
    def __init__(
        self,
        store: TenantAdminStore,
        delete_blobs: DeleteBlobsCommandHandler,
        queue: Queue | None = None,
    ) -> None:
        self._store = store
        self._delete_blobs = delete_blobs
        self._queue = queue

    async def handle(self, command: DeleteTenantCommand) -> bool:
        _ensure_mutable(command.tenant_id)
        orphans = await self._store.delete(command.tenant_id)
        if orphans is None:
            return False
        # 在飞 blob 留给 TTL GC：此刻删除，worker 稍后仍会回写向量，留下孤儿（同 GC 口径）。
        # 孤儿在物理删除前于同一事务内复核归属，已被别的租户重新认领的保留。残余竞态与 GC
        # 相同：元数据提交后、旧索引删除前若有人重传并嵌入同一内容，其新索引会被删掉。
        # 删除提交时仍在途的该租户请求可能留下 chain 或悬空归属，已无人能访问；悬空归属会
        # 推迟共享 blob 在之后删除租户时的即时清理，待无人刷新 last_seen 后由 TTL GC 回收。
        inflight = await self._queue.inflight_set() if self._queue is not None else set()
        deletable = [name for name in orphans if name not in inflight]
        for offset in range(0, len(deletable), _DELETE_BATCH_SIZE):
            await self._delete_blobs.handle(
                DeleteBlobsCommand(
                    tuple(deletable[offset : offset + _DELETE_BATCH_SIZE]),
                    only_if_unowned=True,
                )
            )
        return True
