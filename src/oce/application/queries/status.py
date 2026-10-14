"""Blob 状态和检索范围查询。"""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Query
from oce.application.uow import UnitOfWork, UnitOfWorkFactory
from oce.domain.chain.chain import Chain
from oce.shared.errors import (
    InvalidCheckpointTokenError,
    NeedsResetError,
    ScopeRequiredError,
)


@dataclass(frozen=True)
class FindMissingQuery(Query):
    tenant_id: str
    blob_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindMissingResult:
    unknown: tuple[str, ...] = ()
    nonindexed: tuple[str, ...] = ()


async def _classify(
    uow: UnitOfWork, tenant_id: str, blob_names: tuple[str, ...]
) -> FindMissingResult:
    """本租户无归属的 blob 一律报 unknown：客户端会重传，服务端只补归属、不重复嵌入。"""
    if not blob_names:
        return FindMissingResult()
    owned = await uow.ownership.filter_owned(tenant_id, blob_names)
    blobs = await uow.blobs.get_many([name for name in blob_names if name in owned])
    unknown = tuple(name for name in blob_names if name not in blobs)
    nonindexed = tuple(
        name for name in blob_names if name in blobs and not blobs[name].is_ready()
    )
    return FindMissingResult(unknown, nonindexed)


class FindMissingQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: FindMissingQuery) -> FindMissingResult:
        async with self._uow_factory() as uow:
            return await _classify(uow, query.tenant_id, query.blob_names)


@dataclass(frozen=True)
class BlobStatusQuery(Query):
    tenant_id: str
    blob_names: tuple[str, ...] = ()
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class BlobStatusResult:
    unknown: tuple[str, ...] = ()
    nonindexed: tuple[str, ...] = ()
    checkpoint_not_found: bool = False


class BlobStatusQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: BlobStatusQuery) -> BlobStatusResult:
        async with self._uow_factory() as uow:
            missing = await _classify(uow, query.tenant_id, query.blob_names)
            checkpoint_not_found = False
            if query.checkpoint_id:
                parsed = Chain.parse_checkpoint_token(query.checkpoint_id)
                checkpoint_not_found = not (
                    parsed is not None
                    and await uow.chains.exists(parsed[0], query.tenant_id)
                )
        return BlobStatusResult(
            missing.unknown,
            missing.nonindexed,
            checkpoint_not_found,
        )


@dataclass(frozen=True)
class ResolveScopeQuery(Query):
    tenant_id: str
    checkpoint_id: str | None = None
    added_blobs: tuple[str, ...] = ()
    deleted_blobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolveScopeResult:
    blob_names: frozenset[str]


class ResolveScopeQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: ResolveScopeQuery) -> ResolveScopeResult:
        """把 (本租户 chain 成员 ∪ added) ∩ 本租户归属 − deleted 解析为检索范围。

        全库检索已禁用：客户端必须正面声明工作集。checkpoint_id 或 added_blobs 任一
        有效即可；deleted_blobs 只是减法，不构成声明。checkpoint 无效（格式非法、链
        不存在或属于别的租户）直接报错，避免范围静默变窄；别人的链与不存在同一报错，
        不暴露其存在。归属只由上传授予，知道 blob 名不能扩大范围。结果恒为非 None
        frozenset（可为空集），空集表示工作集为空，检索返回空结果而非全库。
        """
        chain_id: str | None = None
        if query.checkpoint_id:
            parsed = Chain.parse_checkpoint_token(query.checkpoint_id)
            if parsed is None:
                raise InvalidCheckpointTokenError(query.checkpoint_id)
            chain_id = parsed[0]
        elif not query.added_blobs:
            # 无 checkpoint 也无 added_blobs（deleted 不足以构成声明）→ 拒绝全库检索
            raise ScopeRequiredError()
        base: set[str] = set()
        async with self._uow_factory() as uow:
            if chain_id is not None:
                if not await uow.chains.exists(chain_id, query.tenant_id):
                    raise NeedsResetError("checkpoint 链不存在（服务端状态丢失）")
                base = await uow.ownership.owned_chain_members(query.tenant_id, chain_id)
            added = await uow.ownership.filter_owned(query.tenant_id, query.added_blobs)
        scope = (base | added) - set(query.deleted_blobs)
        return ResolveScopeResult(frozenset(scope))
