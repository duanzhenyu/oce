"""租户 admin 的 SQL 实现（TenantAdminStore 端口）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oce.application.tenant_admin import TenantRecord, TenantUpdate
from oce.infrastructure.persistence.credential_admin_store import _hash_key
from oce.infrastructure.persistence.models import (
    ChainMemberModel,
    ChainModel,
    TenantBlobModel,
    TenantModel,
)
from oce.shared.errors import TenantConflictError

_ACTIVE = "active"


def _to_record(model: TenantModel) -> TenantRecord:
    return TenantRecord(
        tenant_id=model.tenant_id,
        name=model.name,
        status=model.status,
        api_key_last4=model.api_key_last4,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise TenantConflictError() from exc


class SqlTenantAdminStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list(self) -> list[TenantRecord]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(TenantModel).order_by(TenantModel.name))
            ).scalars().all()
            return [_to_record(row) for row in rows]

    async def create(self, name: str, api_key: str) -> TenantRecord:
        model = TenantModel(
            tenant_id=uuid.uuid4().hex,
            name=name,
            api_key_hash=_hash_key(api_key),
            api_key_last4=api_key[-4:],
            status=_ACTIVE,
        )
        async with self._session_factory() as session:
            session.add(model)
            await _commit(session)
            await session.refresh(model)
            return _to_record(model)

    async def update(
        self, tenant_id: str, changes: TenantUpdate
    ) -> TenantRecord | None:
        async with self._session_factory() as session:
            model = await session.get(TenantModel, tenant_id)
            if model is None:
                return None
            if changes.name is not None:
                model.name = changes.name
            if changes.status is not None:
                model.status = changes.status
            model.updated_at = datetime.now(timezone.utc)
            await _commit(session)
            await session.refresh(model)
            return _to_record(model)

    async def rotate_key(self, tenant_id: str, api_key: str) -> TenantRecord | None:
        async with self._session_factory() as session:
            model = await session.get(TenantModel, tenant_id)
            if model is None:
                return None
            model.api_key_hash = _hash_key(api_key)
            model.api_key_last4 = api_key[-4:]
            model.updated_at = datetime.now(timezone.utc)
            await _commit(session)
            await session.refresh(model)
            return _to_record(model)

    async def delete(self, tenant_id: str) -> list[str] | None:
        # SQLite 未开外键，级联不生效：chain 成员、chain、归属都在同一事务里显式删除
        async with self._session_factory() as session:
            if await session.get(TenantModel, tenant_id) is None:
                return None
            others = aliased(TenantBlobModel)
            orphans = (
                await session.execute(
                    select(TenantBlobModel.blob_name)
                    .where(
                        TenantBlobModel.tenant_id == tenant_id,
                        ~select(others.blob_name)
                        .where(
                            others.blob_name == TenantBlobModel.blob_name,
                            others.tenant_id != tenant_id,
                        )
                        .exists(),
                    )
                    .order_by(TenantBlobModel.blob_name)
                )
            ).scalars().all()
            chain_ids = select(ChainModel.chain_id).where(ChainModel.tenant_id == tenant_id)
            await session.execute(
                delete(ChainMemberModel).where(ChainMemberModel.chain_id.in_(chain_ids))
            )
            await session.execute(delete(ChainModel).where(ChainModel.tenant_id == tenant_id))
            await session.execute(
                delete(TenantBlobModel).where(TenantBlobModel.tenant_id == tenant_id)
            )
            await session.execute(delete(TenantModel).where(TenantModel.tenant_id == tenant_id))
            await session.commit()
            return list(orphans)

    async def authenticate(self, api_key: str) -> str | None:
        if not api_key:
            return None
        async with self._session_factory() as session:
            return await session.scalar(
                select(TenantModel.tenant_id).where(
                    TenantModel.api_key_hash == _hash_key(api_key),
                    TenantModel.status == _ACTIVE,
                )
            )
