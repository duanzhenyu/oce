"""SqlTenantAdminStore：CRUD、key 轮换与鉴权、删除租户的归属清理。"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.application.commands.ingest import DeleteBlobsCommand, DeleteBlobsCommandHandler
from oce.application.tenant_admin import TenantUpdate
from oce.domain.blob.blob import Blob
from oce.infrastructure.persistence.models import ChainMemberModel, ChainModel
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chain_repo import SqlChainRepository
from oce.infrastructure.persistence.sql_ownership_repo import SqlOwnershipRepository
from oce.infrastructure.persistence.tenant_admin_store import SqlTenantAdminStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.shared.database.session import Base
from oce.shared.errors import TenantConflictError
from tests.conftest import make_sha256


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory, SqlTenantAdminStore(factory)
    await engine.dispose()


async def test_create_returns_masked_record_and_key_authenticates(env):
    _, store = env
    record = await store.create("team-a", "oce_secret-key-abcd")

    assert record.name == "team-a"
    assert record.status == "active"
    assert record.api_key_last4 == "abcd"
    assert await store.authenticate("oce_secret-key-abcd") == record.tenant_id
    assert await store.authenticate("oce_other") is None
    assert await store.authenticate("") is None


async def test_plaintext_key_is_never_stored(env):
    factory, store = env
    await store.create("team-a", "oce_plaintext-sentinel")

    async with factory() as session:
        rows = (await session.execute(text("SELECT * FROM tenants"))).all()
    assert rows
    assert all("oce_plaintext-sentinel" not in str(value) for row in rows for value in row)


async def test_disabled_tenant_cannot_authenticate(env):
    _, store = env
    record = await store.create("team-a", "oce_key-a")

    disabled = await store.update(record.tenant_id, TenantUpdate(status="disabled"))
    assert disabled.status == "disabled"
    assert await store.authenticate("oce_key-a") is None

    await store.update(record.tenant_id, TenantUpdate(status="active"))
    assert await store.authenticate("oce_key-a") == record.tenant_id


async def test_rotate_key_invalidates_old_key(env):
    _, store = env
    record = await store.create("team-a", "oce_old-key-1111")

    rotated = await store.rotate_key(record.tenant_id, "oce_new-key-2222")

    assert rotated.api_key_last4 == "2222"
    assert await store.authenticate("oce_old-key-1111") is None
    assert await store.authenticate("oce_new-key-2222") == record.tenant_id


async def test_name_conflicts_raise(env):
    _, store = env
    await store.create("team-a", "oce_key-a")
    other = await store.create("team-b", "oce_key-b")

    with pytest.raises(TenantConflictError):
        await store.create("team-a", "oce_key-c")
    with pytest.raises(TenantConflictError):
        await store.update(other.tenant_id, TenantUpdate(name="team-a"))
    assert [record.name for record in await store.list()] == ["team-a", "team-b"]


async def test_unknown_tenant_returns_none(env):
    _, store = env
    assert await store.update("ghost", TenantUpdate(name="x")) is None
    assert await store.rotate_key("ghost", "oce_key") is None
    assert await store.delete("ghost") is None


async def test_delete_removes_chains_and_ownership_and_reports_orphans(env):
    factory, store = env
    doomed = await store.create("team-a", "oce_key-a")
    survivor = await store.create("team-b", "oce_key-b")
    exclusive, shared = make_sha256("exclusive"), make_sha256("shared")
    async with factory() as session:
        await SqlBlobRepository(session).save_many(
            [Blob(exclusive, "exclusive.py"), Blob(shared, "shared.py")]
        )
        ownership = SqlOwnershipRepository(session)
        await ownership.grant(doomed.tenant_id, [exclusive, shared])
        await ownership.grant(survivor.tenant_id, [shared])
        chains = SqlChainRepository(session)
        doomed_chain = await chains.create([exclusive, shared], doomed.tenant_id)
        kept_chain = await chains.create([shared], survivor.tenant_id)
        await session.commit()

    orphans = await store.delete(doomed.tenant_id)

    assert orphans == [exclusive]
    assert await store.authenticate("oce_key-a") is None
    async with factory() as session:
        ownership = SqlOwnershipRepository(session)
        assert await ownership.filter_owned(doomed.tenant_id, [exclusive, shared]) == set()
        assert await ownership.filter_owned(survivor.tenant_id, [shared]) == {shared}
        chain_ids = set((await session.execute(select(ChainModel.chain_id))).scalars())
        member_chains = set(
            (await session.execute(select(ChainMemberModel.chain_id))).scalars()
        )
    assert chain_ids == {kept_chain.chain_id}
    assert member_chains == {kept_chain.chain_id}
    assert doomed_chain.chain_id not in chain_ids


class RecordingVectors:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, blob_names: list[str]) -> None:
        self.deleted.extend(blob_names)


async def test_orphan_reclaimed_before_purge_is_preserved(env):
    factory, store = env
    doomed = await store.create("team-a", "oce_key-a")
    reclaimer = await store.create("team-b", "oce_key-b")
    reclaimed, gone = make_sha256("reclaimed"), make_sha256("gone")
    async with factory() as session:
        await SqlBlobRepository(session).save_many([Blob(reclaimed, "r.py"), Blob(gone, "g.py")])
        await SqlOwnershipRepository(session).grant(doomed.tenant_id, [reclaimed, gone])
        await session.commit()

    orphans = await store.delete(doomed.tenant_id)
    assert orphans == sorted([reclaimed, gone])
    async with factory() as session:
        # 物理清理之前，另一租户上传了同一内容
        await SqlOwnershipRepository(session).grant(reclaimer.tenant_id, [reclaimed])
        await session.commit()

    vectors = RecordingVectors()
    handler = DeleteBlobsCommandHandler(lambda: SqlAlchemyUnitOfWork(factory), vectors)
    await handler.handle(DeleteBlobsCommand(tuple(orphans), only_if_unowned=True))

    assert vectors.deleted == [gone]
    async with factory() as session:
        blobs = SqlBlobRepository(session)
        assert await blobs.exists(reclaimed)
        assert not await blobs.exists(gone)
        ownership = SqlOwnershipRepository(session)
        assert await ownership.filter_owned(reclaimer.tenant_id, [reclaimed]) == {reclaimed}
