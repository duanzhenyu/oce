"""SQLAlchemy 租户 blob 归属仓储。"""

from __future__ import annotations

from typing import Collection, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.repositories import OwnershipRepository
from oce.infrastructure.persistence.models import (
    ChainMemberModel,
    ChainModel,
    TenantBlobModel,
)

# 评测一次声明数千个 blob，IN 列表与批量 VALUES 都按批切分
_BATCH_SIZE = 1_000


class SqlOwnershipRepository(OwnershipRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _insert(self):
        bind = self.session.get_bind()
        return sqlite_insert if bind.dialect.name == "sqlite" else pg_insert

    async def grant(self, tenant_id: str, blob_names: Sequence[str]) -> None:
        names = sorted(set(blob_names))
        for offset in range(0, len(names), _BATCH_SIZE):
            values = [
                {"tenant_id": tenant_id, "blob_name": name}
                for name in names[offset : offset + _BATCH_SIZE]
            ]
            stmt = self._insert()(TenantBlobModel).values(values)
            await self.session.execute(
                stmt.on_conflict_do_nothing(index_elements=["tenant_id", "blob_name"])
            )

    async def filter_owned(self, tenant_id: str, blob_names: Collection[str]) -> set[str]:
        names = sorted(set(blob_names))
        owned: set[str] = set()
        for offset in range(0, len(names), _BATCH_SIZE):
            rows = await self.session.execute(
                select(TenantBlobModel.blob_name).where(
                    TenantBlobModel.tenant_id == tenant_id,
                    TenantBlobModel.blob_name.in_(names[offset : offset + _BATCH_SIZE]),
                )
            )
            owned.update(rows.scalars())
        return owned

    async def filter_unowned(self, blob_names: Collection[str]) -> set[str]:
        names = sorted(set(blob_names))
        owned: set[str] = set()
        for offset in range(0, len(names), _BATCH_SIZE):
            rows = await self.session.execute(
                select(TenantBlobModel.blob_name)
                .where(TenantBlobModel.blob_name.in_(names[offset : offset + _BATCH_SIZE]))
                .distinct()
            )
            owned.update(rows.scalars())
        return set(names) - owned

    async def owned_chain_members(self, tenant_id: str, chain_id: str) -> set[str]:
        rows = await self.session.execute(
            select(ChainMemberModel.blob_name)
            .join(ChainModel, ChainModel.chain_id == ChainMemberModel.chain_id)
            .join(
                TenantBlobModel,
                TenantBlobModel.blob_name == ChainMemberModel.blob_name,
            )
            .where(
                ChainMemberModel.chain_id == chain_id,
                ChainModel.tenant_id == tenant_id,
                TenantBlobModel.tenant_id == tenant_id,
            )
        )
        return set(rows.scalars())
