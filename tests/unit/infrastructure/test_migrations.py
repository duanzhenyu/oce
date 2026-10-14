"""编程式迁移 run_migrations 与迁移脚本的 SQLite（个人模式）兼容性测试。"""

from __future__ import annotations

from pathlib import Path

import oce
import pytest
from sqlalchemy import create_engine, inspect, text


_SCRIPT_LOCATION = Path(oce.__file__).resolve().parent / "alembic"
_PRE_TENANT_REVISION = "e2f4a6c8d0b1"


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    return cfg


def _head_revision() -> str:
    """动态读取迁移链 head，避免每新增一个迁移都要改测试硬编码。"""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


@pytest.fixture
def sqlite_url(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> str:
    """临时 SQLite 文件库；让 settings 与环境变量指向它（env.py 从 settings 读 URL）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'oce.db').as_posix()}"
    monkeypatch.setenv("DB_URL", url)
    from oce.shared.config import get_settings

    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


def _sync_engine(url: str):
    return create_engine(url.replace("sqlite+aiosqlite://", "sqlite://", 1))


def test_run_migrations_creates_head_schema(sqlite_url: str) -> None:
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations(sqlite_url)

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            tables = set(inspect(conn).get_table_names())
            version = conn.execute(
                text("SELECT version_num FROM oce_alembic_version")
            ).scalar()
    finally:
        engine.dispose()

    assert {"blobs", "chunks", "blob_chunks", "symbol_occurrences"}.issubset(tables)
    assert {"tenants", "tenant_blobs"}.issubset(tables)
    # 监控迁移链（含检索审计）也应被建出
    assert {
        "api_call_metrics",
        "token_usage_metrics",
        "resource_samples",
        "retrieval_metrics",
    }.issubset(tables)
    assert version == _head_revision()


def test_run_migrations_is_idempotent(sqlite_url: str) -> None:
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations(sqlite_url)
    run_migrations(sqlite_url)  # 第二次不应抛“表已存在”


def test_run_migrations_stamps_legacy_create_all_db(sqlite_url: str) -> None:
    """旧版 create_all 库（无版本表）应被 stamp 为 head，而不是重放建表报错。"""
    from oce.infrastructure.persistence import models  # noqa: F401
    from oce.infrastructure.persistence.migrations import run_migrations
    from oce.shared.database.session import Base

    engine = _sync_engine(sqlite_url)
    Base.metadata.create_all(engine)  # 模拟 create_all 时代的库
    engine.dispose()

    run_migrations(sqlite_url)

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT version_num FROM oce_alembic_version")
            ).scalar()
    finally:
        engine.dispose()
    assert version == _head_revision()


def test_symbol_occurrences_insert_auto_id_on_sqlite(sqlite_url: str) -> None:
    """封面回归：迁移里 id 必须走 INTEGER 自增，created_at 必须用 func.now()。"""
    from oce.infrastructure.persistence.migrations import run_migrations

    run_migrations(sqlite_url)

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO symbol_occurrences "
                    "(identifier, blob_name, content_hash, kind, start_line, end_line) "
                    "VALUES ('parse_document', 'blob-1', 'hash-1', 'definition', 1, 5)"
                )
            )
            row = conn.execute(
                text("SELECT id FROM symbol_occurrences")
            ).fetchone()
    finally:
        engine.dispose()

    assert row is not None
    assert row.id == 1


def test_tenant_migration_assigns_existing_data_to_default(sqlite_url: str) -> None:
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, _PRE_TENANT_REVISION)
    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO blobs (blob_name, path, content_size, file_type, status) "
                    "VALUES ('blob-1', 'a.py', 1, 'text', 'ready'), "
                    "('blob-2', 'b.py', 1, 'text', 'pending')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chains (chain_id, version, total_blobs, total_chunks) "
                    "VALUES ('chain-1', 1, 0, 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            tenants = conn.execute(
                text("SELECT tenant_id, name, status, api_key_hash FROM tenants")
            ).all()
            owned = conn.execute(
                text("SELECT tenant_id, blob_name FROM tenant_blobs ORDER BY blob_name")
            ).all()
            chain_tenant = conn.execute(text("SELECT tenant_id FROM chains")).scalar()
    finally:
        engine.dispose()

    assert [tuple(row) for row in tenants] == [("default", "default", "active", None)]
    assert [tuple(row) for row in owned] == [("default", "blob-1"), ("default", "blob-2")]
    assert chain_tenant == "default"


def test_tenant_migration_downgrade_round_trip(sqlite_url: str) -> None:
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRE_TENANT_REVISION)

    engine = _sync_engine(sqlite_url)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            chain_columns = {column["name"] for column in inspector.get_columns("chains")}
    finally:
        engine.dispose()

    assert not {"tenants", "tenant_blobs"} & tables
    assert "tenant_id" not in chain_columns
    command.upgrade(cfg, "head")
