"""WikiDB 仓储测试 —— 真库门控（@pytest.mark.postgres）。

页面 CRUD / 乐观锁失配 / publish 全事务（revisions 页面清单+清 rev0+
no_pending_changes+wiki_no_pages+stale 令牌）/ rollback 纯指针（rev0 草稿
保留）/ discard_draft / ensure_draft checkout（不推进 updated_at）/ units
投影（H2 分节、幂等重投、1024 维向量写入）/ links 维护（recompute 删目标页
落 broken + alias 边归一 canonical）/ diff 六键 / 来源绑定与指纹（批次3
T1）/ collect_units 编译取材（批次3 T2：列/scope 过滤/向量解码/排序）。本机
无 PG 时整个模块在收集期跳过（与 KB_RUN_POSTGRES_ACCEPTANCE 验收门同款
门槛，跑法见 tests/conftest.py 的 control_pool/domain_pool 注释）。
"""
from __future__ import annotations

import json
import os
import uuid
from array import array

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip(
        "set KB_RUN_POSTGRES_ACCEPTANCE=1 to run real PostgreSQL tests",
        allow_module_level=True,
    )

from knowledge_mining.mining.kb.db import KbDB  # noqa: E402
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB  # noqa: E402
from knowledge_mining.mining.kb.db_compile_inputs import CompileInputsDB  # noqa: E402
from knowledge_mining.mining.kb.db_wiki import (  # noqa: E402
    WikiDB,
    WikiError,
    compute_page_edges,
)
from knowledge_mining.mining.kb.db_wiki_toc import (  # noqa: E402
    get_draft_toc,
    upsert_draft_toc,
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
    # 返回契约（66号批次1扩展）：warnings=toc 解析警示（本例无草稿行、
    # 派生无警示 → 空）
    assert result == {"revision": 1, "page_count": 2, "warnings": []}

    rev = await adb.get_revision(aid, 1)
    assert rev is not None and rev["row_count"] == 2
    schema = rev["schema_json"]
    assert [p["page_key"] for p in schema["pages"]] == ["概述", "细节"]
    assert schema["pages"][0]["origin"] == "manual"
    # 无 toc 草稿行 → 按 ordinal 派生：两页均 manual → 「手工页」节
    assert schema["toc"] == {"sections": [
        {"section_title": "手工页", "page_keys": ["概述", "细节"]},
    ]}
    assert schema["glossary"] == []  # 批次3填充
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

    # 纯指针：rev0 草稿保留、快照不动（数据不动语义）。
    # rev0=checkout 复制的 v1页 + 新增 v2页 共 2 页（ensure_draft 与表格行表同构：
    # current→rev0 INSERT..SELECT 复制后开草稿窗）——回滚不得触碰。
    # （真库首跑勘误：原断言误写 1——写而不跑期间从未执行过。）
    assert await wdb.count_pages(aid, 0) == 2
    assert await wdb.count_pages(aid, 1) == 1
    assert await wdb.get_page(aid, 0, "v2页") is not None
    assert await wdb.get_page(aid, 0, "v1页") is not None
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


async def test_diff_changed_carries_content(async_pool) -> None:
    """Task 1.5：changed 元素带 changes——非 body 字段旧新直给（old=current、
    new=draft，与表格列级 old/new 先例同向）；body_md preview 各截前 200 字 +
    unified_diff 行级 hunks（未超 100 行不带 truncated）。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    old_body = "旧段落一\n旧段落二\n"
    await _upsert(wdb, aid, _page("改动页", "改动页", old_body, aliases=["旧别名"]))
    await _upsert(wdb, aid, _page("类型页", "类型页", "同文"))
    art = await adb.get_artifact(aid)
    await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="v1", published_by="editor-1",
    )

    # 草稿编辑：一页改 title/body_md/frontmatter，另一页翻 page_type/origin
    await wdb.ensure_draft(aid)
    new_body = "新段落一\n旧段落二\n"
    await _upsert(wdb, aid, _page(
        "改动页", "改动页v2", new_body, aliases=["新别名"]))
    await _upsert(wdb, aid, _page(
        "类型页", "类型页", "同文", page_type="glossary", origin="machine"))

    d = await wdb.diff_draft_vs_current(aid)
    assert d["changed_count"] == 2
    assert set(d) == set(_EMPTY_DIFF)  # 顶层仍六平键（计数语义不变）
    elems = {c["page_key"]: c for c in d["changed"]}
    assert set(elems["改动页"]) == {"page_key", "fields", "changes"}
    assert set(elems["改动页"]["changes"]) == {"title", "body_md", "frontmatter"}
    # 非 body 字段：旧新直给，无 hunks 结构
    assert elems["改动页"]["changes"]["title"] == {
        "old": "改动页", "new": "改动页v2"}
    assert elems["改动页"]["changes"]["frontmatter"] == {
        "old": {"aliases": ["旧别名"]}, "new": {"aliases": ["新别名"]}}
    assert elems["类型页"]["changes"] == {
        "page_type": {"old": "topic", "new": "glossary"},
        "origin": {"old": "manual", "new": "machine"},
    }
    # body_md：preview 上下文 + 行级 hunks（difflib.unified_diff 原生输出——
    # 未传 fromfile/tofile，---/+++ 头带尾随空格，stdlib 字面行为）
    body = elems["改动页"]["changes"]["body_md"]
    assert set(body) == {"old_preview", "new_preview", "diff_hunks"}
    assert body["old_preview"] == old_body   # 短于 200 字不截
    assert body["new_preview"] == new_body
    assert body["diff_hunks"] == [
        "--- ", "+++ ", "@@ -1,2 +1,2 @@",
        "-旧段落一", "+新段落一", " 旧段落二",
    ]
    assert "truncated" not in body  # 未超样例上限不携带


async def test_diff_changed_body_hunks_truncated(async_pool) -> None:
    """Task 1.5 截断口径：body_md hunks 截前 100 行（对齐 _DIFF_SAMPLE_LIMIT
    先例）+ truncated=true；preview 各截前 200 字符。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    old_body = "\n".join(f"旧{i}" for i in range(150))
    new_body = "\n".join(f"新{i}" for i in range(150))
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("长文页", "长文页", old_body))
    art = await adb.get_artifact(aid)
    await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="v1", published_by="editor-1",
    )
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("长文页", "长文页", new_body))

    d = await wdb.diff_draft_vs_current(aid)
    assert d["changed_count"] == 1
    [elem] = d["changed"]
    assert elem["fields"] == ["body_md"]
    body = elem["changes"]["body_md"]
    # 3 头（---/+++/@@）+ 150 删 + 150 增 = 303 行 → 截前 100
    assert len(body["diff_hunks"]) == 100
    assert body["diff_hunks"][0] == "--- " and body["diff_hunks"][1] == "+++ "
    assert body["diff_hunks"][2] == "@@ -1,150 +1,150 @@"
    assert body["diff_hunks"][3] == "-旧0"
    assert body["truncated"] is True
    assert body["old_preview"] == old_body[:200]
    assert body["new_preview"] == new_body[:200]
    assert len(body["old_preview"]) == len(body["new_preview"]) == 200


# --------------------------------------------- 来源绑定（63号批次3 Task 1）

async def _seed_kb(async_pool) -> tuple[KbDB, str]:
    """编译取材夹具：建 kb（用户/库名随机，测试间不互踩）。
    返回 (kbdb, kb_id)。"""
    kbdb = KbDB(async_pool)
    owner = await kbdb.create_user(username=f"wiki-src-{uuid.uuid4().hex[:8]}")
    # create_kb 校验 owner 域绑定（_lock_user_domain_access）——create_user
    # 不自动绑域，须显式 bind_domain
    await kbdb.bind_domain(user_id=owner["id"], domain="plant-a")
    kb = await kbdb.create_kb(
        domain="plant-a", name=f"指纹库{uuid.uuid4().hex[:6]}",
        owner_id=owner["id"])
    return kbdb, kb["id"]


async def _add_doc(kbdb: KbDB, kb_id: str, *, directory_path: str = "") -> str:
    """kb 内加一份文档身份（directory_path 支撑 scope 过滤断言）。返回 doc_id。"""
    doc = await kbdb.insert_document_identity(
        domain="plant-a", kb_id=kb_id,
        document_key=f"doc:/{kb_id}/{uuid.uuid4().hex[:8]}",
        document_name="a.md", storage_path=f"/tmp/{kb_id}/a.md",
        directory_path=directory_path)
    return str(doc["id"])


async def _seed_build(
    async_pool, *, kb_id: str, document_id: str, units: int,
    created_at: str = "2026-10-22T00:00:00+00:00",
    selection_status: str = "active",
    with_embeddings: int = 0,
) -> str:
    """直插一份 validated build + 文档快照 + snapshot link + N 个 v2 检索单元
    （夹具形态照 test_stats_queries._build_snapshot；units 走
    asset_retrieval_units_v2——编译取材只读 L1 v2 产出；link 行是 scope 过滤
    的目录归属来源——_DIR_PATH_LATERAL 经 asset_document_snapshot_links 取
    directory_path）。ordinal=i / section_ref=s-{i}（取材排序列断言锚点，
    section_ref 由 015 迁移 ALTER 补列）；with_embeddings>0 时给前 N 个单元
    直插 1024 维向量行（embedding_id 沿生产 `{snapshot}:{rep}` 确定格式；
    向量首分量 0.5 其余 0.001——array('f') 解码断言锚点）。返回 snapshot_id。"""
    snap_id, build_id = uuid.uuid4().hex, uuid.uuid4().hex
    vec_text = "[0.5," + ",".join(["0.001"] * 1023) + "]"
    async with async_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO asset_document_snapshots
               (id, domain, normalized_content_hash, raw_content_hash,
                mime_type, created_at)
               VALUES (%s, 'plant-a', %s, %s, 'text/markdown', %s)""",
            (snap_id, uuid.uuid4().hex, uuid.uuid4().hex, created_at))
        await conn.execute(
            """INSERT INTO asset_builds
               (id, build_code, status, build_mode, domain, kb_id, created_at)
               VALUES (%s, %s, 'validated', 'full', 'plant-a', %s, %s)""",
            (build_id, f"build-{build_id[:8]}", kb_id, created_at))
        await conn.execute(
            """INSERT INTO asset_build_document_snapshots
               (build_id, document_id, document_snapshot_id, selection_status,
                reason)
               VALUES (%s, %s, %s, %s, 'add')""",
            (build_id, document_id, snap_id, selection_status))
        await conn.execute(
            """INSERT INTO asset_document_snapshot_links
               (id, document_id, document_snapshot_id, relative_path,
                source_uri, linked_at)
               VALUES (%s, %s, %s, 'a.md', 'object://source', %s)""",
            (uuid.uuid4().hex, document_id, snap_id, created_at))
        for i in range(units):
            await conn.execute(
                """INSERT INTO asset_retrieval_units_v2
                   (representation_id, snapshot_id, representation_type,
                    content_type, content_text, target_type, target_ref,
                    canonical_evidence_id, ordinal, section_ref)
                   VALUES (%s, %s, 'raw_text', 'text/plain', %s,
                           'raw_segment', %s, %s, %s, %s)""",
                (f"ru-{snap_id[:16]}-{i}", snap_id, f"单元{i}",
                 f"seg-{i}", f"ev-{i}", i, f"s-{i}"))
        for i in range(with_embeddings):
            await conn.execute(
                """INSERT INTO asset_retrieval_embeddings_v2
                   (embedding_id, snapshot_id, representation_id, strategy,
                    policy_version, provider, model, model_version, dimension,
                    input_hash, embedding_vector_vec)
                   VALUES (%s, %s, %s, 'dense', 'test', 'test', 'test', '1',
                           1024, %s, %s::vector)""",
                (f"{snap_id}:ru-{snap_id[:16]}-{i}", snap_id,
                 f"ru-{snap_id[:16]}-{i}", uuid.uuid4().hex, vec_text))
    return snap_id


async def test_sources_crud_soft_delete_revival_fingerprint(
    async_pool,
) -> None:
    """sources 往返：upsert 落库/复活（软删后重绑同库 status 回 active、指纹
    保留）；软删后 list 不可见、二次软删 False；update_fingerprint 覆写且
    不存在的绑定 False；绑定写推进乐观锁令牌（touch 同事务）；stale 令牌
    权威比对拒绝且整体回滚。"""
    kbdb = KbDB(async_pool)
    adb = ArtifactDB(async_pool)
    inputs = CompileInputsDB(async_pool)
    owner = await kbdb.create_user(username=f"wiki-src-{uuid.uuid4().hex[:8]}")
    art = await adb.create_artifact(
        domain_id="plant-a", kind="wiki", title="绑定 Wiki",
        owner_user_id=owner["id"])
    aid = art["artifact_id"]
    assert await inputs.list_sources(aid) == []

    row = await inputs.upsert_source(
        aid, "kb-alpha", scope={"directories": ["方案/云核"]},
        created_by=owner["id"])
    assert row["status"] == "active"
    assert row["scope"] == {"directories": ["方案/云核"]}
    assert row["fingerprint"] == {}
    # 绑定写推进令牌（touch 同事务，upsert 返回后立即可见）
    assert (await adb.get_artifact(aid))["updated_at"] != art["updated_at"]

    # stale 令牌 → 事务内权威比对拒绝（先于写入，不落行）——"不落行"指本次的
    # kb-beta；已成功的 kb-alpha 不受影响（真库首跑勘误：原断言误写成空列表）。
    with pytest.raises(WikiError) as ei:
        await inputs.upsert_source(
            aid, "kb-beta", scope={"directories": []}, created_by=owner["id"],
            expected_updated_at="2000-01-01T00:00:00+00:00")
    assert ei.value.code == "stale_artifact"
    listed_after_stale = await inputs.list_sources(aid)
    assert [s["kb_id"] for s in listed_after_stale] == ["kb-alpha"]

    await inputs.upsert_source(
        aid, "kb-beta", scope={"directories": []}, created_by=owner["id"])
    listed = await inputs.list_sources(aid)
    assert [r["kb_id"] for r in listed] == ["kb-alpha", "kb-beta"]

    assert await inputs.update_fingerprint(
        aid, "kb-alpha",
        {"snapshots": [{"snapshot_id": "s1", "unit_count": 3}],
         "computed_at": "2026-10-22T00:00:00+00:00"}) is True
    by_kb = {r["kb_id"]: r for r in await inputs.list_sources(aid)}
    assert by_kb["kb-alpha"]["fingerprint"]["snapshots"][0]["unit_count"] == 3
    assert await inputs.update_fingerprint(aid, "kb-missing", {}) is False

    # 软删：list 不可见、二次软删 False；复活：重绑同库回 active、指纹保留
    assert await inputs.delete_source(aid, "kb-alpha") is True
    assert await inputs.delete_source(aid, "kb-alpha") is False
    assert [r["kb_id"] for r in await inputs.list_sources(aid)] == ["kb-beta"]
    revived = await inputs.upsert_source(
        aid, "kb-alpha", scope={"directories": []}, created_by=owner["id"])
    assert revived["status"] == "active"
    assert revived["fingerprint"]["snapshots"][0]["unit_count"] == 3


async def test_current_fingerprint_scope_and_snapshot_semantics(
    async_pool,
) -> None:
    """current_fingerprint：单 kb 当前快照口径（_CURRENT_SNAPSHOT_CTE 同构
    ——重挖取最新 build 快照不累加 / 撤回 removed 不计 / 空库 snapshots=[]）
    + **scope 感知**（审查裁定）：unit_count 按 directories 过滤（_DIR_PATH_
    LATERAL 目录归属 + 精确/前缀命中；空数组=全库；scope 外快照保留行、
    计 0——dirty 对比两侧同形）。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    doc_plan = await _add_doc(kbdb, kb_id, directory_path="方案/云核")
    doc_root = await _add_doc(kbdb, kb_id, directory_path="")
    snap_plan = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_plan, units=3)
    snap_root = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_root, units=2)

    def by_id(snaps: list) -> dict:
        return {s["snapshot_id"]: s["unit_count"] for s in snaps}

    # 全库口径（空数组）
    fp = await inputs.current_fingerprint(kb_id, [])
    assert by_id(fp["snapshots"]) == {snap_plan: 3, snap_root: 2}
    assert "computed_at" in fp
    assert await inputs.kb_snapshot_unit_count(kb_id) == 5

    # scope 精确命中：只计 plan 快照（root 快照保留行、计 0——两侧同形）
    fp_plan = await inputs.current_fingerprint(kb_id, ["方案/云核"])
    assert by_id(fp_plan["snapshots"]) == {snap_plan: 3, snap_root: 0}
    # 前缀语义：绑定 "方案" 命中 "方案/云核" 子目录
    fp_prefix = await inputs.current_fingerprint(kb_id, ["方案"])
    assert by_id(fp_prefix["snapshots"]) == by_id(fp_plan["snapshots"])
    # 不相关目录 → 全 0
    fp_none = await inputs.current_fingerprint(kb_id, ["不存在"])
    assert by_id(fp_none["snapshots"]) == {snap_plan: 0, snap_root: 0}

    # 重挖：更晚的 validated build 换新快照（5 单元）→ 只见新快照（不累加）
    snap2 = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_plan, units=5,
        created_at="2026-10-22T01:00:00+00:00")
    fp2 = await inputs.current_fingerprint(kb_id, [])
    assert by_id(fp2["snapshots"]) == {snap2: 5, snap_root: 2}
    assert await inputs.kb_snapshot_unit_count(kb_id) == 7

    # 撤回（最新 build 的 selection_status='removed'）→ plan 文档无当前快照
    await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_plan, units=7,
        created_at="2026-10-22T02:00:00+00:00", selection_status="removed")
    fp3 = await inputs.current_fingerprint(kb_id, [])
    assert by_id(fp3["snapshots"]) == {snap_root: 2}
    assert await inputs.kb_snapshot_unit_count(kb_id) == 2

    # 无任何 build 的空库 → snapshots=[]（scope 与全库同）
    _, empty_kb = await _seed_kb(async_pool)
    assert (await inputs.current_fingerprint(
        empty_kb, ["任意"]))["snapshots"] == []
    assert await inputs.kb_snapshot_unit_count(empty_kb) == 0


async def test_kb_readable_delegates_to_is_visible(async_pool) -> None:
    """kb_readable 委托链接线断言（审查补项；谓词本体语义由 test_kb_db 的
    is_visible 用例覆盖）：owner 可读 / 域内无关者不可读，且与 KbDB.is_visible
    同参同果——防未来漂移成两套判定。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT owner_id FROM knowledge_bases WHERE id = %s", (kb_id,))
        owner_id = str((await cur.fetchone())["owner_id"])
    stranger = await kbdb.create_user(
        username=f"wiki-oth-{uuid.uuid4().hex[:8]}")
    await kbdb.bind_domain(user_id=stranger["id"], domain="plant-a")

    assert await inputs.kb_readable(kb_id, owner_id) is True
    assert await inputs.kb_readable(kb_id, stranger["id"]) is False
    assert await inputs.kb_readable(kb_id, owner_id) == \
        await kbdb.is_visible(kb_id=kb_id, user_id=owner_id)
    assert await inputs.kb_readable(kb_id, stranger["id"]) == \
        await kbdb.is_visible(kb_id=kb_id, user_id=stranger["id"])


async def test_unit_source_provenance_gate_and_unit_join(async_pool) -> None:
    """Q01 回原文（66号批次5 Task 5.2）unit_source：页 provenance 归属门
    （unit_ids 数组元素命中才放行——不开放任意 unit 反查）+ units v2 真身
    JOIN（content_text/section_ref/snapshot_id）；revision 精确匹配；原文
    退役（真身删除）→ None；多页同 unit 确定性（ORDER BY page_key 首遇）。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    doc = await _add_doc(kbdb, kb_id)
    snap = await _seed_build(async_pool, kb_id=kb_id, document_id=doc, units=2)
    rid0, rid1 = f"ru-{snap[:16]}-0", f"ru-{snap[:16]}-1"

    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页一", "标题", provenance=[
        {"kb_id": kb_id, "snapshot_id": snap, "unit_ids": [rid0]}]))
    art = await adb.get_artifact(aid)
    await wdb.publish(aid, expected_updated_at=art["updated_at"],
                      change_summary="v1", published_by="editor-1")

    # 命中：真身行 + provenance 的 (kb_id, snapshot_id)
    row = await inputs.unit_source(aid, rid0, [1])
    assert row == {"kb_id": kb_id, "snapshot_id": snap,
                   "content_text": "单元0", "section_ref": "s-0"}

    # 归属门：库里存在但不在本制品 provenance（rev1）→ None
    assert await inputs.unit_source(aid, rid1, [1]) is None
    # 完全未知 unit → None
    assert await inputs.unit_source(aid, "ru-none", [1]) is None
    # revision 不匹配（发布清 rev0；rev2 不存在）→ None
    assert await inputs.unit_source(aid, rid0, [0]) is None
    assert await inputs.unit_source(aid, rid0, [2]) is None
    # 空 revision 集（调用方 runtime 空集语义）→ None
    assert await inputs.unit_source(aid, rid0, []) is None

    # 多页含同一 unit：第二页 page_key 序在前 → 确定性取该页的 provenance 账
    await wdb.ensure_draft(aid)   # checkout current→rev0
    await _upsert(wdb, aid, _page("A页", "标题", provenance=[
        {"kb_id": kb_id, "snapshot_id": snap, "unit_ids": [rid0]}]))
    art = await adb.get_artifact(aid)
    await wdb.publish(aid, expected_updated_at=art["updated_at"],
                      change_summary="v2", published_by="editor-1")
    row2 = await inputs.unit_source(aid, rid0, [2])
    assert row2 == {"kb_id": kb_id, "snapshot_id": snap,
                    "content_text": "单元0", "section_ref": "s-0"}

    # 原文退役：真身删除 → JOIN 不上 → None（页 provenance 仍在）
    async with async_pool.connection() as conn:
        await conn.execute(
            "DELETE FROM asset_retrieval_units_v2 WHERE representation_id = %s",
            (rid0,))
    assert await inputs.unit_source(aid, rid0, [1]) is None


async def test_update_batch_details_merge_and_latest_compile_batch(
    async_pool,
) -> None:
    """details 增量合并铁律（批次3 决策1）：两次 patch 叠加不丢键（非全量
    覆盖）；latest_compile_batch 含 details 列、只认 wiki_compile 通道、
    created_at DESC 取最新；不存在的批次/制品 → False/None。"""
    kbdb = KbDB(async_pool)
    adb = ArtifactDB(async_pool)
    inputs = CompileInputsDB(async_pool)
    owner = await kbdb.create_user(username=f"wiki-bat-{uuid.uuid4().hex[:8]}")
    art = await adb.create_artifact(
        domain_id="plant-a", kind="wiki", title="编译 Wiki",
        owner_user_id=owner["id"])
    aid = art["artifact_id"]

    # create_batch 白名单已放行 wiki_compile（file_name/sheet_name 列 NOT
    # NULL DEFAULT ''——空串占位；Task 3 编译调用方再做通道适配）
    b1 = await adb.create_batch(
        aid, channel="wiki_compile", file_name="", sheet_name="",
        row_count=0, created_by=owner["id"])
    assert await inputs.update_batch_details(
        b1["batch_id"],
        {"state": "preparing", "progress": {"batch": 1, "total": 8}}) is True
    assert await inputs.update_batch_details(
        b1["batch_id"],
        {"state": "awaiting_toc", "toc": [{"section_title": "概述"}]}) is True

    latest = await inputs.latest_compile_batch(aid)
    assert latest is not None and latest["batch_id"] == b1["batch_id"]
    assert latest["channel"] == "wiki_compile"
    # 两次 patch 叠加：progress 键未被第二次 patch 丢掉（增量合并语义）
    assert latest["details"]["state"] == "awaiting_toc"
    assert latest["details"]["progress"] == {"batch": 1, "total": 8}
    assert latest["details"]["toc"] == [{"section_title": "概述"}]

    # upload 批次不入 latest_compile_batch；新 wiki_compile 批成为最新
    await adb.create_batch(
        aid, channel="upload", file_name="a.xlsx", sheet_name="S1",
        row_count=1, created_by=owner["id"])
    b2 = await adb.create_batch(
        aid, channel="wiki_compile", file_name="", sheet_name="",
        row_count=12, created_by=owner["id"])
    latest2 = await inputs.latest_compile_batch(aid)
    assert latest2 is not None and latest2["batch_id"] == b2["batch_id"]
    assert latest2["row_count"] == 12 and latest2["details"] == {}

    assert await inputs.latest_compile_batch("art_missing") is None
    assert await inputs.update_batch_details(
        "bat_missing", {"state": "failed"}) is False


# --------------------------------------------- 编译取材（63号批次3 Task 2）

async def test_collect_units_columns_scope_and_ordering(async_pool) -> None:
    """collect_units（编译取材）：返回列齐全（section_ref=015 补列 /
    kb_id Python 侧回填）+ scope 过滤三态（精确命中/前缀子目录/空数组=全库；
    不相关目录=[]）+ LEFT JOIN 无向量单元保行（embedding=None）+ 有向量单元
    array('f') 1024 维解码 + ORDER BY (snapshot_id, ordinal,
    representation_id) 排序确定性（两次调用逐行相等——聚类输入序根基）。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    doc_plan = await _add_doc(kbdb, kb_id, directory_path="方案/云核")
    doc_root = await _add_doc(kbdb, kb_id, directory_path="")
    snap_plan = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_plan,
        units=3, with_embeddings=1)
    snap_root = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_root, units=2)

    # 空目录数组 = 全库；两次调用逐行相等（排序确定性）
    rows = await inputs.collect_units(kb_id, [])
    assert rows == await inputs.collect_units(kb_id, [])
    assert len(rows) == 5
    keys = [(r["snapshot_id"], r["ordinal"], r["representation_id"])
            for r in rows]
    assert keys == sorted(keys)

    plan_rows = [r for r in rows if r["snapshot_id"] == snap_plan]
    assert len(plan_rows) == 3
    first = plan_rows[0]
    assert first["representation_id"] == f"ru-{snap_plan[:16]}-0"
    assert first["content_text"] == "单元0"
    assert first["structural_context"] == ""
    assert first["section_ref"] == "s-0"
    assert first["ordinal"] == 0
    assert first["dense_eligible"] is True
    assert first["directory_path"] == "方案/云核"
    assert first["kb_id"] == kb_id

    # 首单元带向量：pgvector 文本 → array('f') 1024 维（决策2 ②；f32 舍入
    # 用 approx）
    emb = first["embedding"]
    assert isinstance(emb, array) and emb.typecode == "f"
    assert len(emb) == 1024
    assert emb[0] == pytest.approx(0.5, rel=1e-6)
    assert emb[1] == pytest.approx(0.001, rel=1e-6)
    # 无向量单元（plan 其余 + root 全部）：LEFT JOIN 保行、embedding=None
    assert all(r["embedding"] is None for r in plan_rows[1:])
    root_rows = [r for r in rows if r["snapshot_id"] == snap_root]
    assert len(root_rows) == 2
    assert all(r["embedding"] is None and r["directory_path"] == ""
               for r in root_rows)

    # scope 精确命中：只取 plan 快照 3 单元（root 快照整体出局）
    exact = await inputs.collect_units(kb_id, ["方案/云核"])
    assert {r["snapshot_id"] for r in exact} == {snap_plan}
    assert all(r["directory_path"] == "方案/云核" for r in exact)
    assert all(r["kb_id"] == kb_id for r in exact)
    # 前缀语义：绑定 "方案" 命中 "方案/云核" 子目录——与精确命中同集同序
    prefix = await inputs.collect_units(kb_id, ["方案"])
    assert prefix == exact
    # 不相关目录 → 空
    assert await inputs.collect_units(kb_id, ["不存在"]) == []


async def test_collect_units_follows_latest_snapshot_only(async_pool) -> None:
    """collect_units 快照口径与 current_fingerprint 同构：重挖取最新 build
    快照（旧快照单元不残留）；撤回（selection_status='removed'）→ 该文档
    单元整体出局——取材与指纹/dirty 判定看同一份语料。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    doc = await _add_doc(kbdb, kb_id, directory_path="方案/云核")
    snap1 = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc, units=3)
    assert len(await inputs.collect_units(kb_id, [])) == 3

    # 重挖：更晚的 validated build 换新快照（2 单元）→ 只见新快照
    snap2 = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc, units=2,
        created_at="2026-10-22T01:00:00+00:00")
    rows = await inputs.collect_units(kb_id, [])
    assert {r["snapshot_id"] for r in rows} == {snap2}
    assert all(r["representation_id"].startswith(f"ru-{snap2[:16]}")
               for r in rows)
    assert snap1 != snap2

    # 撤回（最新 build 的快照 selection_status='removed'）→ 无当前快照
    await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc, units=7,
        created_at="2026-10-22T02:00:00+00:00", selection_status="removed")
    assert await inputs.collect_units(kb_id, []) == []


async def test_collect_units_shared_snapshot_dedup(async_pool) -> None:
    """共享快照去重（审查 I-3）：同 kb 两文档共享同一快照（同内容 hash
    一等支持路径）——latest 按 document_id DISTINCT ON 会对同一
    document_snapshot_id 产出两行；cur 段 DISTINCT 修正后 units JOIN 不
    翻倍（修复前 6 行）、指纹单行（修复前重复两条）、Σ 计数不虚增。"""
    inputs = CompileInputsDB(async_pool)
    kbdb, kb_id = await _seed_kb(async_pool)
    doc_a = await _add_doc(kbdb, kb_id, directory_path="共享目录")
    doc_b = await _add_doc(kbdb, kb_id, directory_path="共享目录")
    snap = await _seed_build(
        async_pool, kb_id=kb_id, document_id=doc_a, units=3)

    # 文档 B 复用同快照：补 validated build 选择行 + snapshot link
    # （两文档同目录——LATERAL 的 ORDER BY d.id 归属无二义）
    build_id = uuid.uuid4().hex
    async with async_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO asset_builds
               (id, build_code, status, build_mode, domain, kb_id, created_at)
               VALUES (%s, %s, 'validated', 'full', 'plant-a', %s,
                       '2026-10-22T03:00:00+00:00')""",
            (build_id, f"build-{build_id[:8]}", kb_id))
        await conn.execute(
            """INSERT INTO asset_build_document_snapshots
               (build_id, document_id, document_snapshot_id, selection_status,
                reason)
               VALUES (%s, %s, %s, 'active', 'add')""",
            (build_id, doc_b, snap))
        await conn.execute(
            """INSERT INTO asset_document_snapshot_links
               (id, document_id, document_snapshot_id, relative_path,
                source_uri, linked_at)
               VALUES (%s, %s, %s, 'b.md', 'object://source',
                       '2026-10-22T03:00:00+00:00')""",
            (uuid.uuid4().hex, doc_b, snap))

    rows = await inputs.collect_units(kb_id, [])
    assert len(rows) == 3  # 共享快照只取一份（修复前 latest 双行 → 6）
    assert {r["snapshot_id"] for r in rows} == {snap}
    assert all(r["directory_path"] == "共享目录" for r in rows)

    # 指纹与 Σ 护栏同源受益：单行快照、计数不虚增
    fp = await inputs.current_fingerprint(kb_id, [])
    assert fp["snapshots"] == [{"snapshot_id": snap, "unit_count": 3}]
    assert await inputs.kb_snapshot_unit_count(kb_id) == 3


# ------------------------------------------------- 目录定版（66号批次1 Task 1.2）

async def test_draft_toc_roundtrip(async_pool) -> None:
    """toc 草稿行 CRUD 往返：无行（未知 artifact）→ None；首次写入跳锁
    （行不存在——编译钩子/用户首次编辑场景）→ get 幂等；行存在时
    expected_updated_at 比对**本行** updated_at——stale → WikiError
    ("stale_artifact")（服务层 _WIKI_CODE_TO_ERROR 映射 StaleArtifact/409，
    复用既有口径不新造第二套）且整体回滚不被覆盖；畸形 toc（sections 非
    列表/节字段类型错/嵌套畸形/跨节重复键）→ ValueError（路由 422）。"""
    _, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)

    toc = {"sections": [
        {"section_title": "概述", "page_keys": ["页A", "页B"]},
        {"section_title": "细节", "page_keys": ["页C"]},
    ]}
    async with async_pool.connection() as conn:
        assert await get_draft_toc(conn, aid) is None  # 未知 artifact 无行

        row = await upsert_draft_toc(conn, aid, toc, updated_by="editor-1")
        assert row["artifact_id"] == aid
        assert row["toc"] == toc and row["updated_by"] == "editor-1"

        got = await get_draft_toc(conn, aid)
        assert got is not None and got["toc"] == toc

        # 行已存在：带令牌合法再写（updated_at 推进后返回新令牌）
        row2 = await upsert_draft_toc(
            conn, aid, toc, updated_by="editor-2",
            expected_updated_at=got["updated_at"],
        )
        assert row2["updated_by"] == "editor-2"

        # stale 令牌 → 并发冲突（409 口径），且整体回滚不被覆盖
        with pytest.raises(WikiError) as ei:
            await upsert_draft_toc(
                conn, aid, toc, updated_by="late",
                expected_updated_at="2000-01-01T00:00:00+00:00",
            )
        assert ei.value.code == "stale_artifact"
        assert (await get_draft_toc(conn, aid))["updated_by"] == "editor-2"

        # 畸形 toc → ValueError（sections 列表 / 节字段类型 / 嵌套 / 跨节重复）
        for bad in (
            {"sections": "不是列表"},
            {},  # 缺 sections 键
            {"sections": [{"section_title": 1, "page_keys": []}]},
            {"sections": [{"section_title": "s", "page_keys": "不是列表"}]},
            {"sections": [{"section_title": "s", "page_keys": [{"k": 1}]}]},
            {"sections": [
                {"section_title": "s1", "page_keys": ["页A"]},
                {"section_title": "s2", "page_keys": ["页A"]},  # 跨节重复
            ]},
        ):
            with pytest.raises(ValueError):
                await upsert_draft_toc(conn, aid, bad, updated_by="e")


async def test_publish_freezes_toc(async_pool) -> None:
    """发布冻结：预置 toc 行（含 1 个已删页键 + 漏 1 个现存页）→ publish →
    快照 toc 无死键、漏页落尾部「其他」节、返回 warnings 带 dropped/
    appended 两组键；toc 草稿行发布后保留（设计态持续存在，下次编辑起点）。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页A", "页A", "a", ordinal=0))
    await _upsert(wdb, aid, _page("页B", "页B", "b", ordinal=1))
    await _upsert(wdb, aid, _page("页C", "页C", "c", ordinal=2))
    async with async_pool.connection() as conn:
        await upsert_draft_toc(conn, aid, {"sections": [
            {"section_title": "节1", "page_keys": ["页A", "死页X"]},
            {"section_title": "节2", "page_keys": ["页B"]},
        ]}, updated_by="editor-1")

    art = await adb.get_artifact(aid)
    result = await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="含目录", published_by="editor-1",
    )
    assert result["revision"] == 1
    warnings = result["warnings"]
    assert any(w.startswith("toc_dropped_page_keys:死页X") for w in warnings)
    assert any(w.startswith("toc_appended_page_keys:页C") for w in warnings)

    schema = (await adb.get_revision(aid, 1))["schema_json"]
    assert schema["toc"] == {"sections": [
        {"section_title": "节1", "page_keys": ["页A"]},  # 死键已剔
        {"section_title": "节2", "page_keys": ["页B"]},
        {"section_title": "其他", "page_keys": ["页C"]},  # 漏页尾部追加
    ]}
    # 草稿行发布后保留（冻结进快照 ≠ 清草稿）
    async with async_pool.connection() as conn:
        assert await get_draft_toc(conn, aid) is not None


async def test_publish_derives_toc_when_absent(async_pool) -> None:
    """无 toc 行 → 按 ordinal 派生：机器页 ordinal//1000-1 归节（节1 两页/
    节2 一页）；专页 910000 基段归固定节「矛盾对照」；manual 手工页归
    「手工页」节；派生无警示（warnings 空）。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("机1", "机1", "x", ordinal=1001,
                                  origin="machine"))
    await _upsert(wdb, aid, _page("机2", "机2", "x", ordinal=1002,
                                  origin="machine"))
    await _upsert(wdb, aid, _page("机3", "机3", "x", ordinal=2001,
                                  origin="machine"))
    await _upsert(wdb, aid, _page(
        "矛盾1", "矛盾1", "x", ordinal=910001, origin="machine",
        page_type="contradiction"))
    await _upsert(wdb, aid, _page("手1", "手1", "x", ordinal=0,
                                  origin="manual"))

    art = await adb.get_artifact(aid)
    result = await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="派生目录", published_by="editor-1",
    )
    assert result["warnings"] == []

    schema = (await adb.get_revision(aid, 1))["schema_json"]
    assert schema["toc"] == {"sections": [
        {"section_title": "第1节", "page_keys": ["机1", "机2"]},
        {"section_title": "第2节", "page_keys": ["机3"]},
        {"section_title": "矛盾对照", "page_keys": ["矛盾1"]},
        {"section_title": "手工页", "page_keys": ["手1"]},
    ]}


async def test_rollback_reads_old_toc(async_pool) -> None:
    """回滚读旧目录（防回归）：r1 快照冻结节 A → 改 toc 发布 r2 冻结节 B →
    rollback r1 后读 r1 快照仍是节 A（纯指针回滚，快照数据不动）。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页", "页", "v1"))
    async with async_pool.connection() as conn:
        await upsert_draft_toc(conn, aid, {"sections": [
            {"section_title": "节A", "page_keys": ["页"]},
        ]}, updated_by="e1")
    art = await adb.get_artifact(aid)
    await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="r1", published_by="e1",
    )

    # 改目录（行已存在 → 锁令牌路径）再发布 r2
    await wdb.ensure_draft(aid)
    async with async_pool.connection() as conn:
        token = (await get_draft_toc(conn, aid))["updated_at"]
        await upsert_draft_toc(conn, aid, {"sections": [
            {"section_title": "节B", "page_keys": ["页"]},
        ]}, updated_by="e2", expected_updated_at=token)
    art = await adb.get_artifact(aid)
    await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="r2", published_by="e2",
    )
    r2_toc = (await adb.get_revision(aid, 2))["schema_json"]["toc"]
    assert r2_toc["sections"][0]["section_title"] == "节B"

    art = await adb.get_artifact(aid)
    await wdb.rollback_to(aid, 1, expected_updated_at=art["updated_at"])
    r1_toc = (await adb.get_revision(aid, 1))["schema_json"]["toc"]
    assert r1_toc == {"sections": [
        {"section_title": "节A", "page_keys": ["页"]},
    ]}


async def test_hard_delete_cascades_toc_row(async_pool) -> None:
    """硬删级联守卫（审查 I-1）：kb_wiki_tocs 身份列刻意无 FK（跨表身份
    TEXT 引用约定），清理责任在统一硬删管线——delete_artifact 必须连带清
    toc 草稿行，否则制品删净后残留孤儿行，违反 1.1.7 删除体系不变量。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页", "页", "x"))
    async with async_pool.connection() as conn:
        await upsert_draft_toc(conn, aid, {"sections": [
            {"section_title": "节", "page_keys": ["页"]},
        ]}, updated_by="e")
        assert await get_draft_toc(conn, aid) is not None  # 前置

    await adb.delete_artifact(aid)

    async with async_pool.connection() as conn:
        assert await get_draft_toc(conn, aid) is None  # 级联已清
    assert await adb.get_artifact(aid) is None  # 制品已删净


async def test_publish_survives_malformed_toc_row(async_pool) -> None:
    """resolve_toc 窄防御（审查 M-3）：toc 列是 JSONB 无库级形态约束，绕过
    写路径校验直插畸形行（sections 非 list——运维/脏数据路径）发布不炸
    事务：畸形节集/节/键列表统一视作空，全页落「其他」节 + appended 警示
    ——快照仍产出完整目录。"""
    adb, wdb, aid = await _make_wiki(async_pool)
    await wdb.ensure_draft(aid)
    await _upsert(wdb, aid, _page("页A", "页A", "a"))
    async with async_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kb_wiki_tocs (artifact_id, toc, updated_by)"
            " VALUES (%s, %s::jsonb, 'ops')",
            (aid, json.dumps({"sections": "运维直插的脏数据"})),
        )

    art = await adb.get_artifact(aid)
    result = await wdb.publish(
        aid, expected_updated_at=art["updated_at"],
        change_summary="脏toc", published_by="editor-1",
    )
    assert any(w.startswith("toc_appended_page_keys:页A")
               for w in result["warnings"])
    schema = (await adb.get_revision(aid, 1))["schema_json"]
    assert schema["toc"] == {"sections": [
        {"section_title": "其他", "page_keys": ["页A"]},
    ]}
