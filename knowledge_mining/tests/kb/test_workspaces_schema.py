# -*- coding: utf-8 -*-
"""工作台 7 表迁移守卫（67号工作台 MVP Task 1，需求=67号规划 §4.1）。

三层断言：
1. 真库表存在性：tests/kb 的 `_ensure_schema` 夹具按启动链
   （pg_schema.domain_schema_paths）建库——021 镜像未注册前 7 张
   kb_workspace_* 表必须缺席，本模块先红后绿即 TDD 红态。
2. 关键约束面：actor_kind/role/connector/status CHECK（actor 字段必须从
   第一张表就带上，67号 §0 禁止事项）+ uq_ws_output_current 部分唯一索引
   （is_current 单活指针，输出失效传播的根）+ 安全语义封闭域——scope 不含
   trial（试跑不建 run）、mode 仅 create_draft（绝不直接发布）、exports.
   request_id UNIQUE（幂等键，规格审查补）。
3. 文件级：迁移 012 与启动链镜像 021 建表集合一致（全量 DDL 主体比对另钉
   在 tests/maintenance/test_schema_mirror_sync.py 的 MIRROR_PAIRS；此处守
   表集合不漏建、两文件同表集）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.postgres]

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_SQL = (
    REPO_ROOT / "databases" / "migrations" / "core" / "20261116_012_kb_workspaces.sql"
)
MIRROR_SQL = REPO_ROOT / "databases" / "kb" / "schemas" / "021_kb_workspaces.sql"

WORKSPACE_TABLES = (
    "kb_workspaces",
    "kb_workspace_members",
    "kb_workspace_sources",
    "kb_workspace_runs",
    "kb_workspace_run_outputs",
    "kb_workspace_output_bindings",
    "kb_workspace_exports",
)


def _created_tables(path: Path) -> set[str]:
    """从 SQL 文件抽取 CREATE TABLE 表名（口径同 test_bootstrap_table_census）。"""
    assert path.is_file(), f"SQL 文件缺失：{path}"
    sql = path.read_text(encoding="utf-8")
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return set(
        re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
            sql,
            flags=re.IGNORECASE,
        )
    )


async def _table_exists(pool, table: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s""",
            (table,),
        )
        return await cur.fetchone() is not None


async def _check_defs(pool, table: str) -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT pg_get_constraintdef(oid) AS def
                 FROM pg_constraint
                WHERE conrelid = %s::regclass AND contype = 'c'""",
            (table,),
        )
        return sorted(str(row["def"]) for row in await cur.fetchall())


async def _unique_defs(pool, table: str) -> list[tuple[str, str]]:
    """返回 (约束名, 定义) 列表——UNIQUE 约束（contype='u'）。"""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT conname, pg_get_constraintdef(oid) AS def
                 FROM pg_constraint
                WHERE conrelid = %s::regclass AND contype = 'u'""",
            (table,),
        )
        return sorted(
            (str(row["conname"]), str(row["def"])) for row in await cur.fetchall()
        )


def _quoted_values(check_def: str) -> set[str]:
    """从 CHECK 定义提取引号字面量（pg_get_constraintdef 的 IN 渲染形态）。

    用于"值域恰好封闭"断言（如 scope 不含 trial、mode 仅 create_draft）——
    渲染形如 `= ANY ((ARRAY['a'::character varying,...])::text[])`，引号
    字面量与 DDL 声明值一一对应。
    """
    return set(re.findall(r"'([^']*)'", check_def))


# ------------------------------------------------------------ ① 表存在性


@pytest.mark.asyncio
@pytest.mark.parametrize("table", WORKSPACE_TABLES)
async def test_workspace_table_exists(async_pool, table: str) -> None:
    assert await _table_exists(async_pool, table), (
        f"{table} 不存在——启动链 021 镜像未注册进 pg_schema.domain_schema_paths？"
    )


# ------------------------------------------------------------ ② 约束面


@pytest.mark.asyncio
async def test_actor_kind_check_on_registry_run_export(async_pool) -> None:
    """actor 三处落位（67号 §4.1）：登记/运行/导出均带 user/mcp_key 二值约束。"""
    for table in ("kb_workspaces", "kb_workspace_runs", "kb_workspace_exports"):
        defs = await _check_defs(async_pool, table)
        matched = [d for d in defs if "actor_kind" in d]
        assert matched, f"{table} 缺 actor_kind CHECK 约束"
        assert any("'user'" in d and "'mcp_key'" in d for d in matched), table


@pytest.mark.asyncio
async def test_member_role_check(async_pool) -> None:
    defs = await _check_defs(async_pool, "kb_workspace_members")
    assert any(
        "role" in d
        and all(value in d for value in ("'owner'", "'editor'", "'viewer'"))
        for d in defs
    )


@pytest.mark.asyncio
async def test_source_connector_check(async_pool) -> None:
    defs = await _check_defs(async_pool, "kb_workspace_sources")
    assert any(
        "connector" in d and "'kb_dir'" in d and "'excel'" in d for d in defs
    )


@pytest.mark.asyncio
async def test_run_status_check(async_pool) -> None:
    defs = await _check_defs(async_pool, "kb_workspace_runs")
    expected = (
        "'queued'",
        "'running'",
        "'awaiting_review'",
        "'completed'",
        "'partial'",
        "'failed'",
        "'cancelled'",
    )
    assert any(
        "status" in d and all(value in d for value in expected) for d in defs
    )


@pytest.mark.asyncio
async def test_run_scope_check_excludes_trial(async_pool) -> None:
    """scope 值域封闭为 node|workspace|subgraph：trial 试跑不建 run 行
    （67号 §4.1），值域里出现 trial 即语义漂移。"""
    defs = await _check_defs(async_pool, "kb_workspace_runs")
    matched = [d for d in defs if "scope" in d and "CHECK" in d.upper()]
    assert matched, "kb_workspace_runs 缺 scope CHECK 约束"
    values: set[str] = set()
    for d in matched:
        values |= _quoted_values(d)
    assert values == {"node", "workspace", "subgraph"}, (
        f"scope 值域漂移（trial 不允许出现）：{sorted(values)}"
    )


@pytest.mark.asyncio
async def test_export_request_id_unique_constraint(async_pool) -> None:
    """request_id 幂等键（67号 §4.1）：导出重放必须命中同一行而非重复建稿。"""
    uniques = await _unique_defs(async_pool, "kb_workspace_exports")
    matched = [(name, d) for name, d in uniques if "request_id" in d]
    assert matched, "kb_workspace_exports 缺 request_id UNIQUE 约束（幂等键）"
    assert any(name == "uq_ws_exports_request" for name, _ in matched), matched


@pytest.mark.asyncio
async def test_export_mode_check_only_create_draft(async_pool) -> None:
    """mode 值域仅 create_draft：导出只建草稿、绝不直接发布（67号 §0 安全语义，
    publish/overwrite 类取值出现即违规）。"""
    defs = await _check_defs(async_pool, "kb_workspace_exports")
    matched = [d for d in defs if "mode" in d and "CHECK" in d.upper()]
    assert matched, "kb_workspace_exports 缺 mode CHECK 约束"
    values: set[str] = set()
    for d in matched:
        values |= _quoted_values(d)
    assert values == {"create_draft"}, (
        f"mode 值域漂移（只允许 create_draft）：{sorted(values)}"
    )


@pytest.mark.asyncio
async def test_output_current_partial_unique_index(async_pool) -> None:
    """is_current 单活指针：同 (workspace, node, port) 至多一行 current。"""
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_ws_output_current'"
        )
        row = await cur.fetchone()
    assert row is not None, "缺 uq_ws_output_current 部分唯一索引"
    definition = str(row["indexdef"])
    assert definition.startswith("CREATE UNIQUE INDEX"), definition
    assert "kb_workspace_run_outputs" in definition, definition
    assert "(workspace_id, node_id, port_id)" in definition, definition
    assert "WHERE is_current" in definition, definition


# ------------------------------------------------------------ ③ 镜像同表集


def test_migration_and_mirror_create_same_table_set() -> None:
    migration_tables = _created_tables(MIGRATION_SQL)
    mirror_tables = _created_tables(MIRROR_SQL)
    assert migration_tables == set(WORKSPACE_TABLES), migration_tables
    assert mirror_tables == set(WORKSPACE_TABLES), mirror_tables
