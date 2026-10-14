"""PathIndexClient 过滤表达式安全测试。"""

import pytest

from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.shared.config.settings import MilvusSettings

INJECTED_BLOB = 'x"] or blob_name not in ["'
VALID_BLOB = "a" * 64


class FakeCollection:
    def __init__(self):
        self.search_exprs: list[str] = []
        self.delete_exprs: list[str] = []

    def search(self, *, expr, **kwargs):
        self.search_exprs.append(expr)
        return [[]]

    def delete(self, expr):
        self.delete_exprs.append(expr)


@pytest.fixture
def client():
    instance = PathIndexClient(MilvusSettings(endpoint="http://localhost:19530"))
    instance.collection = FakeCollection()
    instance._initialized = True
    return instance


async def test_empty_scope_returns_nothing_without_search(client):
    assert await client.search_paths([0.1], allowed_blob_names=[]) == []
    assert client.collection.search_exprs == []


async def test_none_scope_searches_without_filter(client):
    await client.search_paths([0.1], allowed_blob_names=None)
    assert client.collection.search_exprs == [None]


async def test_search_paths_rejects_injected_blob_name(client):
    with pytest.raises(ValueError):
        await client.search_paths([0.1], allowed_blob_names=[VALID_BLOB, INJECTED_BLOB])
    assert client.collection.search_exprs == []


async def test_delete_rejects_injected_blob_name(client):
    with pytest.raises(ValueError):
        await client.delete_by_blob_names([INJECTED_BLOB])
    assert client.collection.delete_exprs == []


async def test_valid_blob_names_build_in_filter(client):
    await client.search_paths([0.1], allowed_blob_names=[VALID_BLOB])
    await client.delete_by_blob_names([VALID_BLOB])
    expected = f'blob_name in ["{VALID_BLOB}"]'
    assert client.collection.search_exprs == [expected]
    assert client.collection.delete_exprs == [expected]
