from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_mining.mining.maintenance.database_upgrade.contract import (
    BRIDGE_51_TABLES,
    FORMAL_TABLES,
    MIGRATION_LEDGER_TABLE,
    PRE51_REQUIRED_TABLES,
    PREVIOUS_RELEASE_TABLES,
)
from knowledge_mining.mining.maintenance.database_upgrade.database import (
    DatabaseUpgradeError,
    validate_database_name,
)
from knowledge_mining.mining.maintenance.database_upgrade.__main__ import _default_target
from knowledge_mining.mining.maintenance.database_upgrade.manifest import load_manifest
from knowledge_mining.mining.maintenance.database_upgrade.validation import (
    SchemaValidationError,
    compare_retained_table_counts,
    expected_rebase_target_tables,
    validate_schema,
    validate_supported_rebase_source,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


# pg_indexes 默认返回（= 全部 REQUIRED_INDEXES 的合格定义；末两行为 009/010 的 HNSW）
_DEFAULT_INDEX_ROWS: list[tuple[str, str]] = [
    ("idx_user_domains_domain", "CREATE INDEX idx_user_domains_domain ON user_domains (domain)"),
    ("idx_mcp_keys_user", "CREATE INDEX idx_mcp_keys_user ON mcp_keys (user_id)"),
    ("idx_mcp_keys_hash", "CREATE UNIQUE INDEX idx_mcp_keys_hash ON mcp_keys (key_hash)"),
    ("idx_mcp_keys_active_name", "CREATE UNIQUE INDEX idx_mcp_keys_active_name ON mcp_keys (user_id, domain, name) WHERE status = 'active'"),
    ("idx_knowledge_access_domain_time", "CREATE INDEX idx_knowledge_access_domain_time ON knowledge_access_records (domain, occurred_at DESC, id DESC)"),
    ("idx_knowledge_access_actor_time", "CREATE INDEX idx_knowledge_access_actor_time ON knowledge_access_records (actor_user_id, occurred_at DESC, id DESC)"),
    ("idx_knowledge_access_source_tool_time", "CREATE INDEX idx_knowledge_access_source_tool_time ON knowledge_access_records (source, tool_name, occurred_at DESC)"),
    ("idx_knowledge_access_status_time", "CREATE INDEX idx_knowledge_access_status_time ON knowledge_access_records (status, occurred_at DESC)"),
    ("idx_knowledge_access_kb_ids", "CREATE INDEX idx_knowledge_access_kb_ids ON knowledge_access_records USING GIN (kb_ids)"),
    ("idx_are_v2_vec_hnsw", "CREATE INDEX idx_are_v2_vec_hnsw ON public.asset_retrieval_embeddings_v2 USING hnsw (embedding_vector_vec vector_cosine_ops)"),
    ("idx_wiki_units_hnsw", "CREATE INDEX idx_wiki_units_hnsw ON public.kb_wiki_page_units USING hnsw (embedding vector_cosine_ops)"),
]


class _ValidationConnection:
    def __init__(
        self,
        *,
        tables: list[str],
        extensions: list[str],
        migration,
        index_rows: list[tuple[str, str]] | None = None,
        invalid_indexes: list[tuple[str]] | None = None,
        staging_ann_indexes: list[tuple[str]] | None = None,
    ):
        self.tables = tables
        self.extensions = extensions
        self.migration = migration
        self.index_rows = index_rows
        self.invalid_indexes = invalid_indexes or []
        self.staging_ann_indexes = staging_ann_indexes or []

    def execute(self, query, params=None):
        text = str(query)
        if "to_regclass" in text:
            return _Cursor([(True,)])
        if "migration_id = %s AND checksum = %s" in text:
            return _Cursor([(True,)])
        if "migration_id, checksum" in text:
            return _Cursor([(self.migration.migration_id, self.migration.checksum)])
        if "pg_catalog.pg_tables" in text:
            return _Cursor([(name,) for name in self.tables])
        if "pg_extension" in text:
            return _Cursor([(name,) for name in self.extensions])
        if "pg_indexes" in text:
            return _Cursor(
                self.index_rows if self.index_rows is not None else _DEFAULT_INDEX_ROWS
            )
        # 009：staging 是挖掘写入面，禁止近邻索引（查询同时含 pg_index/pg_am，先判 pg_am）
        if "JOIN pg_am am" in text:
            return _Cursor(list(self.staging_ann_indexes))
        # 009：关键索引 indisvalid/indisready 复核（定义文本正确 ≠ 可用）
        if "FROM pg_index i" in text:
            return _Cursor(list(self.invalid_indexes))
        raise AssertionError(f"unexpected SQL: {text}")


def _manifest(tmp_path: Path):
    sql = tmp_path / "001.sql"
    sql.write_text("SELECT 1;\n", encoding="utf-8")
    checksum = hashlib.sha256(sql.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "schema_version: test-v1\nmigrations:\n"
        "  - id: core/001\n"
        "    path: 001.sql\n"
        f"    checksum: {checksum}\n",
        encoding="utf-8",
    )
    return load_manifest(manifest)


@pytest.mark.parametrize("name", ["new_db", "_cmkb_202609", "Db123"])
def test_database_name_accepts_safe_postgresql_identifiers(name: str) -> None:
    assert validate_database_name(name) == name


@pytest.mark.parametrize("name", ["", "bad-name", "x;DROP DATABASE y", "9starts_wrong"])
def test_database_name_rejects_unsafe_values(name: str) -> None:
    with pytest.raises(DatabaseUpgradeError):
        validate_database_name(name)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("agentickb", "agentickb_converged"),
        ("agentickb_converged", "agentickb"),
    ],
)
def test_default_rebase_target_alternates_between_two_fixed_databases(
    source: str, target: str
) -> None:
    assert _default_target(source) == target


def test_default_rebase_target_refuses_irreversible_name_truncation() -> None:
    with pytest.raises(DatabaseUpgradeError, match="过长"):
        _default_target("x" * 54)


def test_schema_validation_accepts_applied_manifest_and_required_extensions(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "knowledge_bases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    report = validate_schema(connection, manifest, enforce_exact_count=False)

    assert report.schema_version == "test-v1"
    assert report.extensions == ("pg_trgm", "vector")


def test_schema_validation_rejects_retired_table(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "asset_publish_releases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    with pytest.raises(SchemaValidationError, match="退役表仍存在"):
        validate_schema(connection, manifest, enforce_exact_count=False)


def test_schema_validation_requires_v2_hnsw_index(tmp_path: Path) -> None:
    """009：v2 向量表缺近邻索引 = 校验失败——1.1.14 之前"升级成功却没 ANN 索引"
    正是缺这道断言才长期无症状（40 万向量全量读盘 100s+）。"""
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "knowledge_bases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
        index_rows=[row for row in _DEFAULT_INDEX_ROWS if row[0] != "idx_are_v2_vec_hnsw"],
    )

    with pytest.raises(SchemaValidationError, match="idx_are_v2_vec_hnsw"):
        validate_schema(connection, manifest, enforce_exact_count=False)


def test_schema_validation_rejects_wrong_operator_class_hnsw(tmp_path: Path) -> None:
    """009：同名索引但操作符类漂移（如误建 l2）——距离语义不符，必须拒绝。"""
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "knowledge_bases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
        index_rows=[row for row in _DEFAULT_INDEX_ROWS if row[0] != "idx_are_v2_vec_hnsw"]
        + [
            (
                "idx_are_v2_vec_hnsw",
                "CREATE INDEX idx_are_v2_vec_hnsw ON public.asset_retrieval_embeddings_v2 "
                "USING hnsw (embedding_vector_vec vector_l2_ops)",
            )
        ],
    )

    with pytest.raises(SchemaValidationError, match="定义漂移"):
        validate_schema(connection, manifest, enforce_exact_count=False)


def test_schema_validation_rejects_invalid_required_index(tmp_path: Path) -> None:
    """009：CONCURRENTLY 中断残留的 INVALID 索引定义文本完好但 planner 永不使用——
    必须按 indisvalid/indisready 拒绝，不能只看定义。"""
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "knowledge_bases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
        invalid_indexes=[("idx_are_v2_vec_hnsw",)],
    )

    with pytest.raises(SchemaValidationError, match="不可用"):
        validate_schema(connection, manifest, enforce_exact_count=False)


def test_schema_validation_rejects_ann_index_on_staging(tmp_path: Path) -> None:
    """009：staging 是挖掘写入面（每批次 DELETE+INSERT），出现近邻索引即校验失败。"""
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=[MIGRATION_LEDGER_TABLE, "knowledge_bases"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
        staging_ann_indexes=[("idx_asset_retrieval_embeddings_v2_staging_vec_hnsw",)],
    )

    with pytest.raises(SchemaValidationError, match="staging"):
        validate_schema(connection, manifest, enforce_exact_count=False)


def test_rebase_source_requires_the_complete_supported_pre51_schema(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    connection = _ValidationConnection(
        tables=["kb_users", "knowledge_bases", "kb_members", "asset_documents"],
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    with pytest.raises(SchemaValidationError, match="pre-51"):
        validate_supported_rebase_source(connection)


def test_rebase_source_accepts_complete_pre51_schema_without_51_tables(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    tables = sorted(PRE51_REQUIRED_TABLES)
    connection = _ValidationConnection(
        tables=tables,
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    assert validate_supported_rebase_source(connection) == tuple(tables)


def test_rebase_source_accepts_complete_immediately_previous_schema(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    tables = sorted(PREVIOUS_RELEASE_TABLES)
    connection = _ValidationConnection(
        tables=tables,
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    assert validate_supported_rebase_source(connection) == tuple(tables)


def test_rebase_source_accepts_current_schema_for_future_offline_rebase(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    tables = sorted(FORMAL_TABLES)
    connection = _ValidationConnection(
        tables=tables,
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    assert validate_supported_rebase_source(connection) == tuple(tables)


def test_rebase_source_rejects_unknown_historical_table_before_clone(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    tables = sorted(PRE51_REQUIRED_TABLES | {"unclaimed_historical_table"})
    connection = _ValidationConnection(
        tables=tables,
        extensions=["pg_trgm", "vector"],
        migration=manifest.migrations[0],
    )

    with pytest.raises(SchemaValidationError, match="未知表"):
        validate_supported_rebase_source(connection)


def test_pre51_contract_is_exactly_formal_tables_without_bridge_tables() -> None:
    assert PRE51_REQUIRED_TABLES == PREVIOUS_RELEASE_TABLES - BRIDGE_51_TABLES
    assert "serving_query_logs" in PRE51_REQUIRED_TABLES
    assert "knowledge_access_records" not in PRE51_REQUIRED_TABLES


def test_pre51_rebase_target_adds_51_tables_without_requiring_them_in_source() -> None:
    source = {
        "kb_users",
        "knowledge_bases",
        "kb_members",
        "asset_documents",
        "asset_builds",
        "mcp_access",
        "mcp_open_kbs",
    }

    target = expected_rebase_target_tables(source)

    assert {"user_domains", "mcp_keys", "mcp_key_open_kbs", "kb_purge_tasks"} <= target
    assert "mcp_access" not in target
    assert "mcp_open_kbs" not in target


def test_content_digest_exempt_tables_are_exactly_migration_mutated_ones() -> None:
    """001 合法改写 mining_runs（删列+awaiting_review 改写），指纹豁免清单不得漂移。"""
    from knowledge_mining.mining.maintenance.database_upgrade.validation import (
        CONTENT_DIGEST_EXEMPT_TABLES,
    )

    assert CONTENT_DIGEST_EXEMPT_TABLES == frozenset({"mining_runs"})


def test_future_rebase_preserves_existing_knowledge_access_records(monkeypatch) -> None:
    """After this cutover, later offline rebases must retain ledger sentinels."""

    class _CountRows:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class _Connection:
        def __init__(self, name):
            self.name = name
            self.tables = tuple(FORMAL_TABLES)

        def execute(self, _statement, _params=None):
            return _CountRows(1)

    source = _Connection("source")
    target = _Connection("target")
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation.fetch_public_tables",
        lambda connection: connection.tables,
    )
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation._shared_columns",
        lambda *_args: ("id",),
    )
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation._table_digest",
        lambda connection, table, _columns: (
            "retrieval-sentinel" if table == "knowledge_access_records" else "same"
        ),
    )

    counts = compare_retained_table_counts(source, target)

    assert counts["knowledge_access_records"] == 1


def test_shared_columns_intersect_in_target_order() -> None:
    """源库被删列（subloop_stage 等）不参与指纹：取交集并按目标列序。"""
    from knowledge_mining.mining.maintenance.database_upgrade.validation import (
        _shared_columns,
    )

    class _ColsConnection:
        def __init__(self, names):
            self.names = names

        def execute(self, query, params=None):
            class _Result:
                def __init__(self, rows):
                    self.rows = rows

                def fetchall(self):
                    return self.rows

            return _Result([(name,) for name in self.names])

    source = _ColsConnection(["id", "status", "subloop_stage", "ontology_version_id"])
    target = _ColsConnection(["id", "status", "build_id"])
    assert _shared_columns(source, target, "mining_runs") == ("id", "status")


def test_two_consecutive_future_rebases_preserve_record_and_payload_ledgers(
    monkeypatch,
) -> None:
    """Both ledgers survive source -> vNext -> vNext+1 clone verification."""

    class _CountRows:
        def fetchone(self):
            return (1,)

    class _Connection:
        def __init__(self, name):
            self.name = name
            self.tables = tuple(FORMAL_TABLES)

        def execute(self, _statement, _params=None):
            return _CountRows()

    source = _Connection("source")
    middle = _Connection("middle")
    target = _Connection("target")
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation.fetch_public_tables",
        lambda connection: connection.tables,
    )
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation._shared_columns",
        lambda *_args: ("id",),
    )
    monkeypatch.setattr(
        "knowledge_mining.mining.maintenance.database_upgrade.validation._table_digest",
        lambda _connection, table, _columns: (
            "ledger-sentinel"
            if table in {
                "knowledge_access_records",
                "knowledge_access_record_payloads",
            }
            else "same"
        ),
    )

    first = compare_retained_table_counts(source, middle)
    second = compare_retained_table_counts(middle, target)

    for counts in (first, second):
        assert counts["knowledge_access_records"] == 1
        assert counts["knowledge_access_record_payloads"] == 1
