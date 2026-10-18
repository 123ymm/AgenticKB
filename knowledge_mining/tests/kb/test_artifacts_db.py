"""ArtifactDB 仓储测试 —— 真库门控（@pytest.mark.postgres）。

发布事务 / ensure_draft / diff / UPSERT provenance 拼接 / 回滚后重发布编号 /
set_primary_key 重算与冲突 / 表达式索引生成等机制级断言。本机无 PG 时整个
模块在收集期跳过（与 KB_RUN_POSTGRES_ACCEPTANCE 验收门同款门槛，跑法见
tests/conftest.py 的 control_pool/domain_pool 注释）。

对抗审查修复（61号 21项）后断言已对齐实现语义：rollback 拨回 > current 的
历史版放行（数据不动）、set_primary_key 按 column_name 重算键、diff 干净态
（draft_open=FALSE 且 current>0）零差异投影、publish 无待发布变更拒绝
（no_pending_changes）。本模块真库跑绿 = 部署前预演项（KB_RUN_POSTGRES_
ACCEPTANCE=1）；本地静态守卫（占位符计数等非门控回归）见
test_artifacts_service.py 尾部。
"""
from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip(
        "set KB_RUN_POSTGRES_ACCEPTANCE=1 to run real PostgreSQL tests",
        allow_module_level=True,
    )

from psycopg.rows import dict_row  # noqa: E402

from knowledge_mining.mining.kb.db import KbDB  # noqa: E402
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB, ArtifactError  # noqa: E402


def _row(key: str, data: dict, ordinal: int = 0, provenance: list | None = None,
         conflicts: dict | None = None) -> dict:
    return {
        "row_key": key, "data": data,
        "provenance": provenance or [], "conflicts": conflicts or {},
        "ordinal": ordinal,
    }


async def _index_names(pool, artifact_id: str) -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT indexname FROM pg_indexes
               WHERE tablename = 'kb_table_rows' AND indexname LIKE %s""",
            (f"idx\\_kbtr\\_{artifact_id.replace('_', '\\_')}\\_%",),
        )
        return sorted(str(r["indexname"]) for r in await cur.fetchall())


async def _table_count(pool, table: str, artifact_id: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE artifact_id = %s",  # noqa: S608
            (artifact_id,),
        )
        return int((await cur.fetchone())["n"])


# ------------------------------------------------------------ 登记 / 成员 / 权限

async def test_create_get_update_and_membership(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-1")
    created = await db.create_artifact(
        domain_id="plant-a", title="MOP 表", description="d",
        owner_user_id=owner["id"],
    )
    aid = created["artifact_id"]
    assert aid.startswith("art_")
    assert created["status"] == "draft"
    assert created["current_revision"] == 0 and created["draft_open"] is False

    got = await db.get_artifact(aid)
    assert got is not None and got["title"] == "MOP 表"
    assert await db.get_artifact("art_missing") is None

    updated = await db.update_artifact_meta(
        aid, title="MOP 表 v2", usage_note="何时用：割接前查",
    )
    assert updated["title"] == "MOP 表 v2"
    assert updated["usage_note"] == "何时用：割接前查"

    member = await kbdb.create_user(username="art-member-1")
    await db.upsert_member(aid, member["id"], "viewer")
    await db.upsert_member(aid, member["id"], "editor")   # 幂等升级
    assert await db.artifact_role(aid, member["id"]) == "editor"
    members = {m["user_id"]: m["role"] for m in await db.list_members(aid)}
    assert members == {owner["id"]: "owner", member["id"]: "editor"}

    mine = await db.list_artifacts_for_user(member["id"])
    assert [a["artifact_id"] for a in mine] == [aid]
    assert mine[0]["my_role"] == "editor"

    all_rows = await db.list_all_artifacts("plant-a")
    assert aid in {a["artifact_id"] for a in all_rows}
    assert await db.list_all_artifacts("no-such-domain") == []

    await db.remove_member(aid, member["id"])
    assert await db.artifact_role(aid, member["id"]) is None

    with pytest.raises(ArtifactError, match="invalid_member_role"):
        await db.upsert_member(aid, member["id"], "boss")


async def test_is_domain_admin_follows_user_domains(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    domadmin = await kbdb.create_user(username="art-domadmin-1")
    plain = await kbdb.create_user(username="art-plain-1")
    siteadmin = await kbdb.create_user(
        username="art-siteadmin-1", site_role="admin",
    )
    await kbdb.bind_domain(
        user_id=domadmin["id"], domain="plant-a", domain_role="admin",
    )
    await kbdb.bind_domain(user_id=plain["id"], domain="plant-a")
    assert await db.is_domain_admin(domadmin["id"], "plant-a") is True
    assert await db.is_domain_admin(domadmin["id"], "other-dom") is False
    assert await db.is_domain_admin(plain["id"], "plant-a") is False
    assert await db.is_domain_admin(siteadmin["id"], "any-dom") is True


# ------------------------------------------------------------ 发布事务主线

async def test_publish_freezes_rev_clears_draft_and_builds_indexes(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-2")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="发布主线", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.update_artifact_meta(aid, usage_note="何时用：检索MOP口径")

    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_primary_key": True, "ordinal": 1},
        {"column_id": "c002", "column_name": "端口号", "column_type": "int",
         "is_indexed": True, "ordinal": 2},
    ])
    result = await db.insert_rows_upsert(aid, 0, [
        _row("MOP-1", {"MOP": "MOP-1", "端口号": 8080}, 1),
        _row("MOP-2", {"MOP": "MOP-2", "端口号": 9090}, 2),
        _row("MOP-3", {"MOP": "MOP-3", "端口号": None}, 3),
    ], updated_by=owner["id"])
    assert result == {"inserted": 3, "updated": 0}

    published = await db.publish(aid, "首次发布", owner["id"])
    assert published == {"revision": 1, "row_count": 3}

    art = await db.get_artifact(aid)
    assert art["status"] == "published" and art["current_revision"] == 1
    assert art["draft_open"] is False
    assert await db.count_rows(aid, 0) == 0            # draft 清空
    assert await db.count_rows(aid, 1) == 3

    # 干净态（draft 关闭且 current>0）：再发布拒绝 + diff 零差异投影（修复③）
    with pytest.raises(ArtifactError, match="no_pending_changes"):
        await db.publish(aid, "again", owner["id"])
    assert await db.diff_draft_vs_current(aid) == {
        "added": [], "removed": [], "changed": [],
        "added_count": 0, "removed_count": 0, "changed_count": 0,
    }

    revision = await db.get_revision(aid, 1)
    assert revision is not None and revision["row_count"] == 3
    schema_cols = revision["schema_json"]
    assert [c["column_id"] for c in schema_cols] == ["c001", "c002"]
    assert schema_cols[1]["column_type"] == "int"

    # 表达式索引：c002 是 int → numeric cast 版
    indexes = await _index_names(async_pool, aid)
    assert indexes == [f"idx_kbtr_{aid}_c002"]

    rows = await db.list_rows(aid, 1, page=1, size=2)
    assert rows["total"] == 3 and len(rows["rows"]) == 2
    assert rows["rows"][0]["row_key"] == "MOP-1"


async def test_publish_requires_usage_note(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-3")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="无说明卡", owner_user_id=owner["id"],
    ))["artifact_id"]
    with pytest.raises(ArtifactError, match="usage_note_required"):
        await db.publish(aid, "x", owner["id"])
    assert (await db.get_artifact(aid))["status"] == "draft"


# ------------------------------------------------------------ UPSERT 语义

async def test_insert_rows_upsert_merges_provenance(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-4")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="UPSERT", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.ensure_draft(aid)
    await db.insert_rows_upsert(aid, 0, [
        _row("k1", {"a": 1}, 1, provenance=[{"batch": "b1"}]),
    ])
    result = await db.insert_rows_upsert(aid, 0, [
        # 命中：data 覆盖 + provenance 拼接 + conflicts 覆盖
        _row("k1", {"a": 2}, 9, provenance=[{"batch": "b2"}],
             conflicts={"a": "cast_failed"}),
        # 未命中：插入
        _row("k2", {"a": 3}, 2),
    ])
    assert result == {"inserted": 1, "updated": 1}
    rows = {r["row_key"]: r for r in (await db.list_rows(aid, 0))["rows"]}
    assert rows["k1"]["data"] == {"a": 2}
    assert rows["k1"]["provenance"] == [{"batch": "b1"}, {"batch": "b2"}]
    assert rows["k1"]["conflicts"] == {"a": "cast_failed"}
    assert rows["k2"]["data"] == {"a": 3}


# ------------------------------------------------------------ 行编辑

async def test_update_cell_batch_assign_delete_row(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-5")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="行编辑", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "ordinal": 1},
        {"column_id": "c002", "column_name": "L1", "column_type": "enum",
         "enum_values": ["部署", "扩容"], "ordinal": 2},
    ])
    await db.insert_rows_upsert(aid, 0, [
        _row("k1", {"MOP": "k1", "L1": "部署"}, 1),
        _row("k2", {"MOP": "k2", "L1": "扩容"}, 2),
        _row("k3", {"MOP": "k3", "L1": "部署"}, 3),
    ])

    row = await db.update_cell(aid, "k1", "L1", "演练", owner["id"])
    assert row is not None and row["data"]["L1"] == "演练"
    assert await db.update_cell(aid, "missing", "L1", "x", owner["id"]) is None

    n = await db.batch_assign(
        aid, "L1", "割接",
        [{"column": "L1", "op": "eq", "value": "部署"}],
        owner["id"],
    )
    assert n == 2
    rows = {r["row_key"]: r for r in (await db.list_rows(aid, 0))["rows"]}
    assert rows["k1"]["data"]["L1"] == "割接"
    assert rows["k3"]["data"]["L1"] == "割接"
    assert rows["k2"]["data"]["L1"] == "扩容"

    n = await db.batch_assign(
        aid, "L1", "保留",
        [{"column": "MOP", "op": "in", "value": ["k1", "k3"]}],
        owner["id"],
    )
    assert n == 2
    with pytest.raises(ArtifactError, match="invalid_filter_op"):
        await db.batch_assign(
            aid, "L1", "x", [{"column": "L1", "op": "regex", "value": ".*"}],
            owner["id"],
        )

    assert await db.delete_row(aid, "k2") is True
    assert await db.delete_row(aid, "k2") is False
    assert (await db.list_rows(aid, 0))["total"] == 2


# ------------------------------------------------------------ diff / discard

async def _published_artifact(db, kbdb, username: str) -> tuple[str, str]:
    owner = await kbdb.create_user(username=username)
    aid = (await db.create_artifact(
        domain_id="plant-a", title="diff 基线", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.update_artifact_meta(aid, usage_note="u")
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "ordinal": 1},
        {"column_id": "c002", "column_name": "L1", "column_type": "text",
         "ordinal": 2},
    ])
    await db.insert_rows_upsert(aid, 0, [
        _row("k1", {"MOP": "k1", "L1": "部署"}, 1),
        _row("k2", {"MOP": "k2", "L1": "扩容"}, 2),
    ])
    await db.publish(aid, "rev1", owner["id"])
    return aid, owner["id"]


async def test_diff_reports_added_removed_changed_column_level(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    aid, owner = await _published_artifact(db, kbdb, "art-owner-6")

    # 未开草稿：diff 为空
    assert await db.diff_draft_vs_current(aid) == {
        "added": [], "removed": [], "changed": [],
        "added_count": 0, "removed_count": 0, "changed_count": 0,
    }

    await db.ensure_draft(aid)   # checkout current → rev0
    assert await db.count_rows(aid, 0) == 2
    await db.update_cell(aid, "k1", "L1", "演练", owner)
    await db.delete_row(aid, "k2")
    await db.insert_rows_upsert(aid, 0, [_row("k3", {"MOP": "k3", "L1": "割接"}, 3)])

    diff = await db.diff_draft_vs_current(aid)
    assert diff["added"] == ["k3"]
    assert diff["removed"] == ["k2"]
    assert diff["changed"] == [
        {"row_key": "k1", "column": "L1", "old": "部署", "new": "演练"},
    ]
    assert (diff["added_count"], diff["removed_count"], diff["changed_count"]) \
        == (1, 1, 1)

    # discard：回干净态
    await db.discard_draft(aid)
    art = await db.get_artifact(aid)
    assert art["draft_open"] is False
    assert await db.count_rows(aid, 0) == 0
    clean = await db.diff_draft_vs_current(aid)
    assert clean["added_count"] == 0 and clean["changed_count"] == 0


# ------------------------------------------------------------ 回滚 / 停用

async def test_rollback_keeps_history_and_republish_numbers_beyond_max(async_pool):
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    aid, owner = await _published_artifact(db, kbdb, "art-owner-7")

    # 第二版：删掉 k2
    await db.ensure_draft(aid)
    await db.delete_row(aid, "k2")
    await db.publish(aid, "rev2 删 k2", owner)
    assert (await db.get_artifact(aid))["current_revision"] == 2

    # 回滚到 rev1（数据不动：rev1/rev2 行都在）
    rolled = await db.rollback_to(aid, 1)
    assert rolled["current_revision"] == 1 and rolled["status"] == "published"
    assert await db.count_rows(aid, 1) == 2
    assert await db.count_rows(aid, 2) == 1          # 历史仍留档

    # 拨回 rev2（> current 的历史版）：数据不动语义下放行（对抗审查修复④）
    rolled = await db.rollback_to(aid, 2)
    assert rolled["current_revision"] == 2
    await db.rollback_to(aid, 1)                      # 回到低位继续用例
    # 非法回滚：仅剩「版本行不存在」判据
    with pytest.raises(ArtifactError, match="invalid_revision"):
        await db.rollback_to(aid, 99)                 # 无版本行

    # 回滚后再发布 → rev3（不与 rev2 撞主键）
    await db.ensure_draft(aid)
    await db.insert_rows_upsert(aid, 0, [_row("k9", {"MOP": "k9", "L1": "新"}, 9)])
    again = await db.publish(aid, "rev3", owner)
    assert again == {"revision": 3, "row_count": 3}

    # 停用 / 再发布
    deprecated = await db.deprecate(aid)
    assert deprecated["status"] == "deprecated"
    assert deprecated["current_revision"] == 3
    with pytest.raises(ArtifactError, match="invalid_status"):
        await db.deprecate(aid)                       # published 才能 deprecate
    revived = await db.republish(aid)
    assert revived["status"] == "published" and revived["current_revision"] == 3


# ------------------------------------------------------------ 主键重算

async def test_set_primary_key_rekeys_rows_and_detects_conflicts(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-8")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="改主键", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_primary_key": True, "ordinal": 1},
        {"column_id": "c002", "column_name": "机房", "column_type": "text",
         "ordinal": 2},
    ])
    await db.insert_rows_upsert(aid, 0, [
        _row("MOP-1", {"MOP": "MOP-1", "机房": "A"}, 1),
        _row("MOP-2", {"MOP": "MOP-2", "机房": "A"}, 2),
    ])

    # 换主键为 机房：两行折到 "A" → 冲突整体回滚
    with pytest.raises(ArtifactError, match="primary_key_conflict"):
        await db.set_primary_key(aid, ["c002"])
    assert {r["row_key"] for r in (await db.list_rows(aid, 0))["rows"]} \
        == {"MOP-1", "MOP-2"}

    # 组合主键 MOP+机房：无冲突，键重算（长度前缀编码——codex二审批号④：
    # '/' 直拼 (A/B,C) 与 (A,B/C) 同键，长度前缀消除碰撞）
    result = await db.set_primary_key(aid, ["c001", "c002"])
    assert result["rekeyed_rows"] == 2
    keys = {r["row_key"] for r in (await db.list_rows(aid, 0))["rows"]}
    assert keys == {"5:MOP-11:A", "5:MOP-21:A"}
    cols = {c["column_id"]: c for c in await db.list_columns(aid)}
    assert cols["c001"]["is_primary_key"] is True
    assert cols["c002"]["is_primary_key"] is True

    # 未知列
    with pytest.raises(ArtifactError, match="unknown_column"):
        await db.set_primary_key(aid, ["c999"])


async def test_set_primary_key_preserves_row_metadata(async_pool) -> None:
    """codex二审批号③回归：改主键重插不丢来源账——provenance/conflicts/
    ordinal/updated_by 完整保留（只有 row_key 换新、updated_at=now()）。"""
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-13")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="重键保字段", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "ordinal": 1},
        {"column_id": "c002", "column_name": "机房", "column_type": "text",
         "ordinal": 2},
    ])
    await db.insert_rows_upsert(aid, 0, [
        _row("k1", {"MOP": "NC-1", "机房": "A"}, 7,
             provenance=[{"batch_id": "bat_x", "file_name": "a.xlsx"}],
             conflicts={"机房": "type_error:脏值"}),
    ], updated_by=owner["id"])
    await db.set_primary_key(aid, ["c001", "c002"])
    row = (await db.list_rows(aid, 0))["rows"][0]
    assert row["row_key"] == "4:NC-11:A"
    assert row["provenance"] == [{"batch_id": "bat_x", "file_name": "a.xlsx"}]
    assert row["conflicts"] == {"机房": "type_error:脏值"}
    assert row["ordinal"] == 7
    raw = await db.get_row(aid, 0, "4:NC-11:A")
    assert raw is not None and raw["data"] == {"MOP": "NC-1", "机房": "A"}


async def test_rename_column_migrates_rev0_data_keys(async_pool) -> None:
    """codex二审批号②回归（真库）：列改名（rename_from）同事务迁移 rev0 行
    data 键——新键有值、旧键无残留；已发布快照（rev>0）不回改（旧键+旧
    schema 自洽，读取侧经 alias 映射闭环）。"""
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-14")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="改名迁键", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.update_artifact_meta(aid, usage_note="u")
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_primary_key": True, "ordinal": 1},
    ])
    await db.insert_rows_upsert(aid, 0, [
        _row("16:NC20260601000295", {"MOP": "NC20260601000295"}, 1),
    ], updated_by=owner["id"])
    await db.publish(aid, "rev1", owner["id"])   # rev1 快照定格旧键

    await db.ensure_draft(aid)
    col = await db.upsert_column(
        aid, "c001", {"column_name": "MOP单号", "alias": ["MOP"]},
        rename_from="MOP",
    )
    assert col["column_name"] == "MOP单号"
    assert col["alias"] == ["MOP"]
    row = (await db.list_rows(aid, 0))["rows"][0]
    assert row["data"] == {"MOP单号": "NC20260601000295"}   # 旧键无残留
    frozen = (await db.list_rows(aid, 1))["rows"][0]
    assert frozen["data"] == {"MOP": "NC20260601000295"}    # 已发布快照不动
    # schema 变更推进乐观锁令牌（批号⑥c）
    assert (await db.get_artifact(aid))["updated_at"] is not None


async def test_row_writes_check_expected_updated_at_atomically(async_pool) -> None:
    """codex二审批号⑥回归（真库）：update_cell 带 expected_updated_at——
    失配 stale_artifact（全精度）且行未被改动；命中则写+touch 同事务。"""
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-15")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="原子乐观锁", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "L1", "column_type": "text",
         "ordinal": 1},
    ])
    await db.insert_rows_upsert(aid, 0, [_row("k1", {"L1": "部署"}, 1)])

    updated_at = (await db.get_artifact(aid))["updated_at"]
    row = await db.update_cell(
        aid, "k1", "L1", "演练", owner["id"], expected_updated_at=updated_at,
    )
    assert row is not None and row["data"]["L1"] == "演练"
    new_updated_at = (await db.get_artifact(aid))["updated_at"]
    assert new_updated_at != updated_at   # touch 同事务推进令牌
    # 旧令牌（微秒级失配也拒）→ stale_artifact，行不动
    with pytest.raises(ArtifactError, match="stale_artifact"):
        await db.update_cell(
            aid, "k1", "L1", "不该落", owner["id"],
            expected_updated_at=updated_at,
        )
    fresh = (await db.list_rows(aid, 0))["rows"][0]
    assert fresh["data"]["L1"] == "演练"


# ------------------------------------------------------------ schema 演进

async def test_column_lifecycle_and_alias(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-9")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="列演进", owner_user_id=owner["id"],
    ))["artifact_id"]

    await db.upsert_column(aid, "c001", {"column_name": "MOP", "ordinal": 1})
    await db.upsert_column(aid, "c002", {
        "column_name": "分类", "column_type": "enum",
        "enum_values": ["部署", "扩容"], "ordinal": 2,
    })
    # 改名 → alias 记旧名（61号 §7.1：读取层旧名→新名兼容）
    await db.upsert_column(aid, "c002", {
        "column_name": "L1分类", "alias": ["分类"],
    })
    cols = await db.list_columns(aid)
    assert [c["column_id"] for c in cols] == ["c001", "c002"]
    assert cols[1]["column_name"] == "L1分类"
    assert cols[1]["alias"] == ["分类"]
    assert cols[1]["enum_values"] == ["部署", "扩容"]

    await db.drop_column(aid, "c001")
    active = await db.list_columns(aid)
    assert [c["column_id"] for c in active] == ["c002"]
    with_dropped = await db.list_columns(aid, include_dropped=True)
    assert [c["column_id"] for c in with_dropped] == ["c001", "c002"]
    assert with_dropped[0]["status"] == "dropped"

    with pytest.raises(ArtifactError, match="invalid_column_type"):
        await db.upsert_column(aid, "c003", {"column_type": "json"})
    with pytest.raises(ArtifactError, match="invalid_column_field"):
        await db.upsert_column(aid, "c003", {"is_primary_key": True})


# ------------------------------------------------------------ 删除级联 / 批次

async def test_delete_artifact_cascades_all_six_tables(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    aid, owner = await _published_artifact(db, kbdb, "art-owner-10")
    await db.ensure_draft(aid)
    await db.insert_rows_upsert(aid, 0, [_row("k9", {"MOP": "k9"}, 9)])
    await db.create_batch(
        aid, channel="upload", file_name="a.xlsx", sheet_name="Sheet1",
        row_count=2, created_by=owner,
    )
    await db.delete_artifact(aid)
    assert await db.get_artifact(aid) is None
    for table in ("kb_table_rows", "kb_table_columns", "kb_artifact_batches",
                  "kb_artifact_members", "kb_artifact_revisions"):
        assert await _table_count(async_pool, table, aid) == 0


async def test_batch_records(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-11")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="批次", owner_user_id=owner["id"],
    ))["artifact_id"]
    batch = await db.create_batch(
        aid, channel="upload", file_name="mop.xlsx", sheet_name="Sheet1",
        row_count=100, created_by=owner["id"],
    )
    assert batch["batch_id"].startswith("bat_")
    listed = await db.list_batches(aid)
    assert [b["batch_id"] for b in listed] == [batch["batch_id"]]
    assert listed[0]["row_count"] == 100
    with pytest.raises(ArtifactError, match="invalid_channel"):
        await db.create_batch(
            aid, channel="ftp", file_name="x", sheet_name="", row_count=0,
            created_by=owner["id"],
        )


# ------------------------------------------------------------ 索引重建

async def test_republish_rebuilds_indexes_and_drop_clears(async_pool) -> None:
    db = ArtifactDB(async_pool)
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username="art-owner-12")
    aid = (await db.create_artifact(
        domain_id="plant-a", title="索引", owner_user_id=owner["id"],
    ))["artifact_id"]
    await db.update_artifact_meta(aid, usage_note="u")

    await db.ensure_draft(aid)
    await db.replace_columns_for_artifact(aid, [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_indexed": True, "ordinal": 1},
        {"column_id": "c002", "column_name": "端口", "column_type": "int",
         "is_indexed": True, "ordinal": 2},
    ])
    await db.insert_rows_upsert(aid, 0, [_row("k1", {"MOP": "k1", "端口": 80})])
    await db.publish(aid, "r1", owner["id"])
    assert await _index_names(async_pool, aid) == [
        f"idx_kbtr_{aid}_c001", f"idx_kbtr_{aid}_c002",
    ]

    # c001 取消索引后重发布 → 旧索引清理
    await db.ensure_draft(aid)
    await db.upsert_column(aid, "c001", {"is_indexed": False})
    await db.publish(aid, "r2", owner["id"])
    assert await _index_names(async_pool, aid) == [f"idx_kbtr_{aid}_c002"]

    await db.drop_draft_expression_indexes(aid)
    assert await _index_names(async_pool, aid) == []
