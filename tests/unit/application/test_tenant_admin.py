"""租户 admin handler：key 生成、default 租户保护、删除租户后的孤儿 blob 清理。"""

from __future__ import annotations

import pytest

from oce.application.commands.ingest import DeleteBlobsCommand
from oce.application.tenant_admin import (
    CreateTenantCommand,
    CreateTenantCommandHandler,
    DeleteTenantCommand,
    DeleteTenantCommandHandler,
    RotateTenantKeyCommand,
    RotateTenantKeyCommandHandler,
    TenantRecord,
    TenantUpdate,
    UpdateTenantCommand,
    UpdateTenantCommandHandler,
)
from oce.shared.errors import DefaultTenantImmutableError


def _record(tenant_id: str = "t1", last4: str = "abcd") -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        name="team",
        status="active",
        api_key_last4=last4,
        created_at=None,
        updated_at=None,
    )


class StubStore:
    def __init__(self, orphans: list[str] | None = None) -> None:
        self.orphans = orphans
        self.keys: list[str] = []
        self.deleted: list[str] = []

    async def create(self, name, api_key):
        self.keys.append(api_key)
        return _record(last4=api_key[-4:])

    async def update(self, tenant_id, changes):
        return _record(tenant_id)

    async def rotate_key(self, tenant_id, api_key):
        self.keys.append(api_key)
        return None if tenant_id == "ghost" else _record(tenant_id, api_key[-4:])

    async def delete(self, tenant_id):
        self.deleted.append(tenant_id)
        return self.orphans


class RecordingDeleteBlobs:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    async def handle(self, command: DeleteBlobsCommand) -> None:
        assert command.only_if_unowned
        self.batches.append(command.blob_names)


class InflightQueue:
    def __init__(self, inflight: set[str]) -> None:
        self.inflight = inflight

    async def inflight_set(self) -> set[str]:
        return set(self.inflight)


async def test_create_generates_prefixed_key_returned_once():
    store = StubStore()

    created = await CreateTenantCommandHandler(store).handle(CreateTenantCommand("team"))

    assert created.api_key.startswith("oce_")
    assert len(created.api_key) > 40
    assert store.keys == [created.api_key]
    assert created.record.api_key_last4 == created.api_key[-4:]


async def test_rotate_generates_fresh_key_and_missing_returns_none():
    store = StubStore()
    handler = RotateTenantKeyCommandHandler(store)

    first = await handler.handle(RotateTenantKeyCommand("t1"))
    second = await handler.handle(RotateTenantKeyCommand("t1"))

    assert first.api_key != second.api_key
    assert await handler.handle(RotateTenantKeyCommand("ghost")) is None


@pytest.mark.parametrize(
    "handler, command",
    [
        (UpdateTenantCommandHandler, UpdateTenantCommand("default", TenantUpdate(name="x"))),
        (RotateTenantKeyCommandHandler, RotateTenantKeyCommand("default")),
    ],
)
async def test_default_tenant_is_immutable(handler, command):
    store = StubStore()
    with pytest.raises(DefaultTenantImmutableError):
        await handler(store).handle(command)
    assert store.keys == []


async def test_default_tenant_cannot_be_deleted():
    store = StubStore(orphans=[])
    handler = DeleteTenantCommandHandler(store, RecordingDeleteBlobs())
    with pytest.raises(DefaultTenantImmutableError):
        await handler.handle(DeleteTenantCommand("default"))
    assert store.deleted == []


async def test_delete_missing_tenant_returns_false():
    delete_blobs = RecordingDeleteBlobs()
    handler = DeleteTenantCommandHandler(StubStore(orphans=None), delete_blobs)

    assert await handler.handle(DeleteTenantCommand("ghost")) is False
    assert delete_blobs.batches == []


async def test_delete_purges_orphans_in_batches_and_skips_inflight():
    orphans = [f"blob-{index:04d}" for index in range(2_501)]
    delete_blobs = RecordingDeleteBlobs()
    handler = DeleteTenantCommandHandler(
        StubStore(orphans=orphans), delete_blobs, InflightQueue({"blob-0000"})
    )

    assert await handler.handle(DeleteTenantCommand("t1")) is True

    assert [len(batch) for batch in delete_blobs.batches] == [1_000, 1_000, 500]
    purged = [name for batch in delete_blobs.batches for name in batch]
    assert purged == orphans[1:]
