"""WikiDB 仓储测试 —— 真库门控（@pytest.mark.postgres）。

页面 CRUD / 乐观锁失配 / publish 全事务（revisions 页面清单+清 rev0+
no_pending_changes+wiki_no_pages+stale 令牌）/ rollback 纯指针（rev0 草稿
保留）/ discard_draft / ensure_draft checkout（不推进 updated_at）/ units
投影（H2 分节、幂等重投、1024 维向量写入）/ links 维护（recompute 删目标页
落 broken + alias 边归一 canonical）/ diff 六键。本机无 PG 时整个模块在
收集期跳过（与 KB_RUN_POSTGRES_ACCEPTANCE 验收门同款门槛，跑法见
tests/conftest.py 的 control_pool/domain_pool 注释）。
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip(
        "set KB_RUN_POSTGRES_ACCEPTANCE=1 to run real PostgreSQL tests",
        allow_module_level=True,
    )

from knowledge_mining.mining.kb.db import KbDB  # noqa: E402
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB  # noqa: E402
from knowledge_mining.mining.kb.db_wiki import (  # noqa: E402
    WikiDB,
    WikiError,
    compute_page_edges,
)

_EMPTY_DIFF = {
    "added": [], "removed": [], "changed": [],
    "added_count": 0, "removed_count": 0, "changed_count": 0,
}


def _page(page_key: str, title: str, body: str = "", *,
          page_type: str = "topic", aliases: list | None = None,
          ordinal: int = 0, origin: str = "manual",
          provenance: list | None = None, flags: dict | None = None) -> dict:
    return {
        "page_key": page_key, "title": title, "page_type": page_type,
        "frontmatter": {"aliases": aliases or []},
        "body_md": body, "provenance": provenance or [], "flags": flags or {},
        "ordinal": ordinal, "origin": origin,
    }


async def _upsert(wdb: WikiDB, artifact_id: str, page: dict,
                  updated_by: str = "editor-1") -> dict:
    return await wdb.upsert_page(
        artifact_id, page_key=page["page_key"], title=page["title"],
        page_type=page["page_type"], frontmatter=page["frontmatter"],
        body_md=page["body_md"], provenance=page["provenance"],
        flags=page["flags"], ordinal=page["ordinal"], origin=page["origin"],
        updated_by=updated_by,
    )


async def _make_wiki(
    async_pool, *, usage_note: str = "何时用：团队词条库",
) -> tuple[ArtifactDB, WikiDB, str]:
    """建 wiki 制品（owner + 可选 usage_note），返回 (adb, wdb, artifact_id)。"""
    kbdb = KbDB(async_pool)
    adb = ArtifactDB(async_pool)
    wdb = WikiDB(async_pool)
    owner = await kbdb.create_user(username=f"wiki-owner-{uuid.uuid4().hex[:8]}")
    art = await adb.create_artifact(
        domain_id="plant-a", kind="wiki", title="运维 Wiki",
        owner_user_id=owner["id"],
    )
    if usage_note:
        art = await adb.update_artifact_meta(
            art["artifact_id"], usage_note=usage_note,
        )
    return adb, wdb, art["artifact_id"]


async def _publish_v1(adb: ArtifactDB, wdb: WikiDB, artifact_id: str,
                      page: dict) -> dict:
    """便捷链：开草稿→写一页→发布，返回 publish 结果。"""
    await wdb.ensure_draft(artifact_id)
    await _upsert(wdb, artifact_id, page)
    art = await adb.get_artifact(artifact_id)
    return await wdb.publish(
        artifact_id, expected_updated_at=art["updated_at"],
        change_summary="v1", published_by="editor-1",
    )


# ------------------------------------------------------------ 页面 CRUD

async def test_page_crud_roundtrip(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    assert await wdb.count_pages(aid) == 0

    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("首页", "首页", "导语 [[网络方案]]"))
    await _upsert(wdb, aid, _page(
        "网络方案", "网络集成方案", "正文", aliases=["组网方案"], ordinal=1,
        provenance=[{"kb_id": "kb1"}], flags={"quality": "ok"},
    ))
    assert await wdb.count_pages(aid) == 2

    pages = await wdb.list_pages(aid, revision=0)
    assert [p["page_key"] for p in pages] == ["首页", "网络方案"]  # ordinal 序
    assert pages[1]["aliases"] == ["组网方案"]  # 自 frontmatter 派生
    assert "body_md" not in pages[1] and "frontmatter" not in pages[1]

    got = await wdb.get_page(aid, 0, "网络方案")
    assert got is not None
    assert got["title"] == "网络集成方案"
    assert got["body_md"] == "正文"
    assert got["frontmatter"]["aliases"] == ["组网方案"]
    assert got["origin"] == "manual"
    assert got["provenance"] == [{"kb_id": "kb1"}]
    assert got["flags"] == {"quality": "ok"}
    assert await wdb.get_page(aid, 0, "不存在") is None

    # 同键再 upsert = 更新（内容字段全量覆盖）
    await _upsert(wdb, aid, _page("网络方案", "网络集成方案v2", "正文v2"))
    got = await wdb.get_page(aid, 0, "网络方案")
    assert got is not None
    assert got["title"] == "网络集成方案v2" and got["body_md"] == "正文v2"

    # 删除页（锁令牌校验路径）
    art = await adb.get_artifact(aid)
    assert await wdb.delete_page(
        aid, "首页", expected_updated_at=art["updated_at"],
    ) is True
    assert await wdb.delete_page(aid, "首页") is False  # 已删
    assert await wdb.count_pages(aid) == 1


async def test_upsert_stale_token_raises_and_rolls_back(async_pool) -> None:
    _, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页", "页", "v1"))
    with pytest.raises(WikiError) as ei:
        await wdb.upsert_page(
            aid, page_key="页", title="页", page_type="topic",
            frontmatter={}, body_md="v2", provenance=[], flags={},
            ordinal=0, origin="manual", updated_by="e",
            expected_updated_at="2000-01-01T00:00:00+00:00",
        )
    assert ei.value.code == "stale_artifact"
    # 失配整体回滚：页未被改动
    got = await wdb.get_page(aid, 0, "页")
    assert got is not None and got["body_md"] == "v1"


# ------------------------------------------------------------ publish

async def test_publish_full_transaction(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("概述", "概述", "导语 [[细节]]", ordinal=0))
    await _upsert(wdb, aid, _page("细节", "细节", "正文", ordinal=1))
    edges = compute_page_edges([
        {"page_key": "概述", "frontmatter": {}, "body_md": "导语 [[细节]]"},
        {"page_key": "细节", "frontmatter": {}, "body_md": ""},
    ])
    for e in edges:
        await wdb.replace_page_links(aid, 0, e["from_page"], edges)

    art = await adb.get_artifact(aid)
    result = await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="首发", published_by="editor-1",
    )
    assert result == {"revision": 1, "page_count": 2}

    rev = await adb.get_revision(aid, 1)
    assert rev is not None and rev["row_count"] == 2
    schema = rev["schema_json"]
    assert [p["page_key"] for p in schema["pages"]] == ["概述", "细节"]
    assert schema["pages"][0]["origin"] == "manual"
    assert schema["toc"] is None and schema["glossary"] == []  # 批次3填充
    assert rev["change_summary"] == "首发" and rev["published_by"] == "editor-1"

    # rev0 已清（页+链接），快照页/边齐全
    assert await wdb.count_pages(aid, 0) == 0
    assert await wdb.list_links(aid, revision=0) == []
    assert await wdb.count_pages(aid, 1) == 2
    assert await wdb.list_links(aid, revision=1) == [
        {"from_page": "概述", "to_page": "细节", "anchor_text": "",
         "status": "valid"},
    ]

    art = await adb.get_artifact(aid)
    assert art["current_revision"] == 1 and art["status"] == "published"
    assert art["draft_open"] is False

    # 干净态二次 publish → no_pending_changes（先于 wiki_no_pages 判定）
    with pytest.raises(WikiError) as ei:
        await wdb.publish(aid, change_summary="再发", published_by="editor-1")
    assert ei.value.code == "no_pending_changes"


async def test_publish_gates(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)  # draft_open=TRUE 且 rev0 无页
    with pytest.raises(WikiError) as ei:
        await wdb.publish(aid, change_summary="空发", published_by="e")
    assert ei.value.code == "wiki_no_pages"

    # usage_note 缺失门禁
    _, wdb2, aid2 = await _make_wiki(async_pool, usage_note="")
    await wdb2.ensure_draft(aid2)
    await _upsert(wdb2, aid2, _page("页", "页", "x"))
    with pytest.raises(WikiError) as ei:
        await wdb2.publish(aid2, change_summary="无用途", published_by="e")
    assert ei.value.code == "usage_note_required"

    # stale 令牌（FOR UPDATE 取行后全精度比对，先于其余门禁）
    await _upsert(wdb, aid, _page("页", "页", "y"))
    stale = (await adb.get_artifact(aid))["updated_at"]
    await _upsert(wdb, aid, _page("页", "页", "z"))  # 令牌再前进
    with pytest.raises(WikiError) as ei:
        await wdb.publish(
            aid, expected_updated_at=stale,
            change_summary="stale", published_by="e",
        )
    assert ei.value.code == "stale_artifact"


# --------------------------------------------- rollback / discard / checkout

async def test_rollback_is_pointer_only(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await _publish_v1(adb, wdb, aid, _page("v1页", "v1页", "v1"))
    # 发布后开新草稿（rev0 有 v2 页）
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("v2页", "v2页", "v2"))

    art = await adb.get_artifact(aid)
    row = await wdb.rollback_to(aid, 1, expected_updated_at=art["updated_at"])
    assert row["current_revision"] == 1 and row["status"] == "published"

    # 纯指针：rev0 草稿保留、快照不动（数据不动语义）
    assert await wdb.count_pages(aid, 0) == 1
    assert await wdb.count_pages(aid, 1) == 1
    assert await wdb.get_page(aid, 0, "v2页") is not None
    assert await wdb.get_page(aid, 1, "v1页") is not None

    art = await adb.get_artifact(aid)
    with pytest.raises(WikiError) as ei:
        await wdb.rollback_to(aid, 99, expected_updated_at=art["updated_at"])
    assert ei.value.code == "invalid_revision"


async def test_discard_draft_clears_rev0_only(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await _publish_v1(adb, wdb, aid, _page("页", "页", "v1"))
    await wdb.ensure_draft(aid)  # checkout
    await _upsert(wdb, aid, _page("草稿页", "草稿页", "乱改"))

    art = await adb.get_artifact(aid)
    row = await wdb.discard_draft(aid, expected_updated_at=art["updated_at"])
    assert row["draft_open"] is False
    assert await wdb.count_pages(aid, 0) == 0
    assert await wdb.count_pages(aid, 1) == 1  # 快照不动


async def test_ensure_draft_checkout_idempotent_no_touch(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await _publish_v1(adb, wdb, aid, _page("页", "页", "v1"))

    token_before = (await adb.get_artifact(aid))["updated_at"]
    row = await wdb.ensure_draft(aid)  # checkout
    assert row["draft_open"] is True
    # checkout 不推进 updated_at（rev0 是 current 的逐字节副本）
    assert (await adb.get_artifact(aid))["updated_at"] == token_before

    got = await wdb.get_page(aid, 0, "页")
    assert got is not None and got["body_md"] == "v1"

    # 幂等：再 ensure 不重复复制
    row2 = await wdb.ensure_draft(aid)
    assert row2["draft_open"] is True
    assert await wdb.count_pages(aid, 0) == 1


# ------------------------------------------------------------ units 投影

async def test_units_projection_and_embeddings(async_pool) -> None:
    _, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    long_body = "## 概述\n\n" + "甲" * 1500 + "\n\n## 细节\n\n" + "乙" * 1500
    await _upsert(wdb, aid, _page("长页", "长页", long_body))
    await _upsert(wdb, aid, _page("短页", "短页", "短正文"))

    n = await wdb.project_units(aid, 0)
    assert n == 3  # 长页 H2 分节 2 块 + 短页 1 块
    assert await wdb.units_status(aid, 0) == {
        "total": 3, "done": 0, "failed": 0, "pending": 3,
    }

    pending = await wdb.list_pending_units(aid, 0)
    keys = sorted(u["unit_key"] for u in pending)
    assert keys == ["短页", "长页#s1", "长页#s2"]
    by_key = {u["unit_key"]: u for u in pending}
    assert by_key["长页#s1"]["content_text"].startswith("# 长页\n\n## 概述")

    # 向量写入 → done（VECTOR(1024) 列必须 1024 维；文本字面量绑定路径）
    target = keys[2]  # 长页#s2
    await wdb.update_unit_embeddings(aid, 0, [target], [[0.001] * 1024])
    assert await wdb.units_status(aid, 0) == {
        "total": 3, "done": 1, "failed": 0, "pending": 2,
    }
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT embedding IS NOT NULL AS has_vec
               FROM kb_wiki_page_units
               WHERE artifact_id = %s AND revision = 0 AND unit_key = %s""",
            (aid, target),
        )
        assert bool((await cur.fetchone())["has_vec"])

    # 失败标记 + 幂等重投（清旧重插，状态归 pending）
    await wdb.mark_units_failed(aid, 0, [keys[1]])
    assert await wdb.units_status(aid, 0) == {
        "total": 3, "done": 1, "failed": 1, "pending": 1,
    }
    assert await wdb.project_units(aid, 0) == 3
    assert await wdb.units_status(aid, 0) == {
        "total": 3, "done": 0, "failed": 0, "pending": 3,
    }


# ------------------------------------------------------------ links 维护

async def test_links_replace_recompute_and_alias_normalization(async_pool) -> None:
    _, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page(
        "A", "A页", "见 [[B]] 与 [[组网方案|看这里]] 以及 [[不存在页]]",
    ))
    await _upsert(wdb, aid, _page("B", "B页", "回 [[A]]"))
    await _upsert(wdb, aid, _page("C", "C页", "", aliases=["组网方案"]))

    metas = await wdb.list_pages(aid, revision=0)
    full = []
    for m in metas:
        got = await wdb.get_page(aid, 0, m["page_key"])
        full.append({
            "page_key": got["page_key"],
            "frontmatter": got["frontmatter"],
            "body_md": got["body_md"],
        })
    edges = compute_page_edges(full)
    for edge in edges:
        await wdb.replace_page_links(aid, 0, edge["from_page"], edges)

    out = await wdb.list_links(aid, revision=0, page_key="A", direction="out")
    assert {e["to_page"]: e["status"] for e in out} == {
        "B": "valid", "C": "valid", "不存在页": "broken",
    }
    # alias 目标归一到 canonical page_key；锚文本保留书写原文
    alias_edge = [e for e in out if e["to_page"] == "C"][0]
    assert alias_edge["anchor_text"] == "看这里"
    back = await wdb.list_links(aid, revision=0, page_key="C", direction="in")
    assert [(e["from_page"], e["to_page"]) for e in back] == [("A", "C")]

    # 删 B → recompute：A→B 落 broken，其余不变
    assert await wdb.delete_page(aid, "B") is True
    await wdb.recompute_link_statuses(aid, 0)
    out = await wdb.list_links(aid, revision=0, page_key="A", direction="out")
    assert {e["to_page"]: e["status"] for e in out} == {
        "B": "broken", "C": "valid", "不存在页": "broken",
    }


# ------------------------------------------------------------ diff

async def test_diff_six_keys(async_pool) -> None:
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("保留页", "保留页", "同文"))
    await _upsert(wdb, aid, _page("改动页", "改动页", "旧文"))
    await _upsert(wdb, aid, _page("删除页", "删除页", "x"))

    # 从未发布：added=全部 rev0 页
    d = await wdb.diff_draft_vs_current(aid)
    assert d["added_count"] == 3
    assert set(d["added"]) == {"保留页", "改动页", "删除页"}
    assert d["removed_count"] == 0 and d["changed_count"] == 0

    art = await adb.get_artifact(aid)
    await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="v1", published_by="editor-1",
    )
    # 干净态：零差异投影
    assert await wdb.diff_draft_vs_current(aid) == _EMPTY_DIFF

    # 编辑草稿：改一页 title + 新增一页 + 删一页 + origin 翻转
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("改动页", "改动页v2", "旧文"))
    await _upsert(wdb, aid, _page("新增页", "新增页", "n"))
    await _upsert(wdb, aid, _page("保留页", "保留页", "同文", origin="machine"))
    await wdb.delete_page(aid, "删除页")

    d = await wdb.diff_draft_vs_current(aid)
    assert d["added"] == ["新增页"] and d["added_count"] == 1
    assert d["removed"] == ["删除页"] and d["removed_count"] == 1
    assert d["changed_count"] == 2
    changed = {c["page_key"]: c["fields"] for c in d["changed"]}
    assert changed["改动页"] == ["title"]
    assert changed["保留页"] == ["origin"]  # 翻 manual/machine 也算 changed
    assert set(d) == set(_EMPTY_DIFF)  # 六平键同名同构
