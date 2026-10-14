"""多租户隔离：归属只由上传授予，检索范围、对账与 checkpoint 均按租户收敛。"""

from __future__ import annotations

import pytest

from oce.application.bus import CommandBus, QueryBus
from oce.application.commands.checkpoint import (
    CheckpointCommand,
    CheckpointCommandHandler,
)
from oce.application.commands.ingest import (
    EmbedPendingCommand,
    EmbedPendingCommandHandler,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
)
from oce.application.queries.search import SearchQuery, SearchResult
from oce.application.queries.status import (
    BlobStatusQuery,
    BlobStatusQueryHandler,
    FindMissingQuery,
    FindMissingQueryHandler,
    ResolveScopeQuery,
    ResolveScopeQueryHandler,
)
from oce.application.service import BlobUpload, RetrievalApplication, compute_blob_name
from oce.domain.blob.blob import BlobStatus
from oce.domain.chain.chain import Chain
from oce.domain.chunk import RecursiveChunker
from oce.shared.errors import NeedsResetError

from tests.unit.application.fakes import (
    FakeEmbedder,
    FakeSearchStore,
    FakeUnitOfWorkFactory,
)

FILE = BlobUpload("src/secret.py", "def secret():\n    return 42\n")
SECRET = compute_blob_name(FILE.path, FILE.content)


class RecordingSearch:
    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    async def handle(self, query: SearchQuery) -> SearchResult:
        self.queries.append(query)
        return SearchResult()


class RecordingEmbed:
    """记录同步嵌入请求的 blob 名，再交给真实 handler。"""

    def __init__(self, inner: EmbedPendingCommandHandler) -> None:
        self.inner = inner
        self.requested: list[tuple[str, ...] | None] = []

    async def handle(self, command: EmbedPendingCommand):
        self.requested.append(command.blob_names)
        return await self.inner.handle(command)


class World:
    """真实 handler + 内存仓储；app 同步嵌入，background_app 只入库。"""

    def __init__(self) -> None:
        self.factory = FakeUnitOfWorkFactory()
        self.store = FakeSearchStore()
        chunker = RecursiveChunker(chunk_size=6000, chunk_overlap=200)
        embedder = FakeEmbedder()
        commands = CommandBus()
        commands.register(
            IngestBlobsCommand,
            IngestBlobsCommandHandler(self.factory, chunker, embedder, self.store),
        )
        self.embed = RecordingEmbed(
            EmbedPendingCommandHandler(self.factory, chunker, embedder, self.store)
        )
        commands.register(EmbedPendingCommand, self.embed)
        commands.register(CheckpointCommand, CheckpointCommandHandler(self.factory))
        self.search = RecordingSearch()
        queries = QueryBus()
        queries.register(SearchQuery, self.search)
        queries.register(FindMissingQuery, FindMissingQueryHandler(self.factory))
        queries.register(BlobStatusQuery, BlobStatusQueryHandler(self.factory))
        queries.register(ResolveScopeQuery, ResolveScopeQueryHandler(self.factory))
        self.app = RetrievalApplication(commands, queries)
        self.background_app = RetrievalApplication(
            commands, queries, background_indexing=True
        )

    def status(self, name: str) -> BlobStatus:
        return self.factory.uow.blobs.blobs[name].status

    def last_scope(self) -> frozenset[str] | None:
        return self.search.queries[-1].allowed_blob_names

    async def members(self, checkpoint_id: str) -> set[str]:
        chain_id = checkpoint_id.rsplit(":", 1)[0]
        return await self.factory.uow.chains.get_members(chain_id)


@pytest.fixture
def world() -> World:
    return World()


async def test_naming_foreign_blob_in_added_does_not_widen_scope(world):
    await world.app.batch_upload([FILE], tenant_id="a")

    await world.app.retrieve("secret", tenant_id="b", added_blobs=[SECRET])
    assert world.last_scope() == frozenset()

    await world.app.retrieve("secret", tenant_id="a", added_blobs=[SECRET])
    assert world.last_scope() == frozenset({SECRET})


async def test_foreign_checkpoint_is_indistinguishable_from_missing(world):
    await world.app.batch_upload([FILE], tenant_id="a")
    created = await world.app.checkpoint(
        tenant_id="a", checkpoint_id=None, added_blobs=[SECRET], deleted_blobs=[]
    )
    ghost = f"{Chain.create([]).chain_id}:1"

    with pytest.raises(NeedsResetError) as foreign:
        await world.app.retrieve(
            "secret", tenant_id="b", checkpoint_id=created.new_checkpoint_id
        )
    with pytest.raises(NeedsResetError) as missing:
        await world.app.retrieve("secret", tenant_id="b", checkpoint_id=ghost)
    with pytest.raises(NeedsResetError):
        await world.app.checkpoint(
            tenant_id="b",
            checkpoint_id=created.new_checkpoint_id,
            added_blobs=[],
            deleted_blobs=[SECRET],
        )
    status = await world.app.blob_status(
        tenant_id="b", blob_names=[], checkpoint_id=created.new_checkpoint_id
    )

    assert str(foreign.value) == str(missing.value)
    assert status.checkpoint_not_found is True
    assert await world.members(created.new_checkpoint_id) == {SECRET}

    await world.app.retrieve("secret", tenant_id="a", checkpoint_id=created.new_checkpoint_id)
    assert world.last_scope() == frozenset({SECRET})


async def test_reupload_grants_ownership_without_reembedding(world):
    await world.app.batch_upload([FILE], tenant_id="a")
    assert world.status(SECRET) == BlobStatus.READY
    upserted = len(world.store.upserted)

    missing = await world.app.find_missing([SECRET], tenant_id="b")
    status = await world.app.blob_status(
        tenant_id="b", blob_names=[SECRET], checkpoint_id=None
    )
    assert missing.unknown == (SECRET,)
    assert status.unknown == (SECRET,)

    result = await world.app.batch_upload([FILE], tenant_id="b")

    assert result.embedded_count == 0
    assert len(world.store.upserted) == upserted
    after = await world.app.find_missing([SECRET], tenant_id="b")
    assert (after.unknown, after.nonindexed) == ((), ())
    await world.app.retrieve("secret", tenant_id="b", added_blobs=[SECRET])
    assert world.last_scope() == frozenset({SECRET})


async def test_foreign_chain_members_are_filtered_by_ownership(world):
    await world.app.batch_upload([FILE], tenant_id="a")
    created = await world.app.checkpoint(
        tenant_id="b", checkpoint_id=None, added_blobs=[SECRET], deleted_blobs=[]
    )

    await world.app.retrieve("secret", tenant_id="b", checkpoint_id=created.new_checkpoint_id)

    assert world.last_scope() == frozenset()


async def test_upload_with_checkpoint_only_advances_own_chain(world):
    created = await world.app.checkpoint(
        tenant_id="a", checkpoint_id=None, added_blobs=[], deleted_blobs=[]
    )

    with pytest.raises(NeedsResetError):
        await world.app.batch_upload(
            [FILE], tenant_id="b", checkpoint_id=created.new_checkpoint_id
        )
    assert await world.members(created.new_checkpoint_id) == set()

    await world.app.batch_upload(
        [FILE], tenant_id="a", checkpoint_id=created.new_checkpoint_id
    )
    assert await world.members(created.new_checkpoint_id) == {SECRET}


async def test_sync_retrieval_embeds_only_owned_added_blobs(world):
    await world.background_app.batch_upload([FILE], tenant_id="a")
    assert world.status(SECRET) == BlobStatus.PENDING

    await world.app.retrieve("secret", tenant_id="b", added_blobs=[SECRET])
    assert world.embed.requested == []
    assert world.status(SECRET) == BlobStatus.PENDING

    await world.app.retrieve("secret", tenant_id="a", added_blobs=[SECRET])
    assert world.embed.requested == [(SECRET,)]
    assert world.status(SECRET) == BlobStatus.READY
