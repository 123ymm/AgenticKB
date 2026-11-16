# -*- coding: utf-8 -*-
"""工作台来源适配器（67号 MVP Task 5）—— kb_dir 文档底表 / Excel 冻结解析 /
materialize 统一取数。真库跑法与 Task 2 同款（容器命令见
docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 5）。

覆盖：
- read_kb_dir：无权（同域非成员对私有库）→ WorkspacePermissionError；kb
  不存在 → WorkspaceNotFoundError；正常读取 → 4 列底表 + 文档内容_JSON
  L1/L2 章节结构 + _id/_source 形态 + manifest（frozen_at/doc_count/
  unparsed_docs/documents 含 content_revision+snapshot_id + dataset 内嵌
  ——冻结统一语义）；scope 目录过滤（精确命中 + 子目录前缀）；文档上限
  （monkeypatch MAX_DOCS=2 造 3 文档超限）；无解析结果/修订错锚的文档
  → unparsed_docs 记录且不产出行。
- parse_excel：类型推断（功率 int / 型号 enum 值原样）+ 表头去重 +
  _id=row_<hex> / _source=文件/sheet；指定第二张 sheet；空表（0 数据行）
  拒；坏 zip（PK 头+垃圾）与压缩比炸弹拒；未知 sheet 拒。
- materialize_source：excel 与 kb_dir 两类 manifest 往返同构；缺 dataset
  键 → WorkspaceValidationError。
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from openpyxl import Workbook

from knowledge_mining.mining.kb import workspace_sources as ws
from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.workspace_dataset import validate_dataset

# 真库标记全局；asyncio 标记逐用例给（纯函数用例不带——strict 模式下
# 全局 asyncio mark 会给同步用例报 PytestWarning）。
pytestmark = [pytest.mark.postgres]


def _hex(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


async def _user(async_pool, *, site_role: str = "member") -> dict:
    return await KbDB(async_pool).create_user(
        username=f"ws-src-{_hex(10)}", site_role=site_role)


def _ctx(u: dict) -> dict:
    """auth.current_user 返回的 kb_users 行形态（id/site_role）。"""
    return {"id": u["id"], "username": u["username"],
            "site_role": u.get("site_role", "member"),
            "status": "active"}


async def _seed_kb(async_pool, *, visibility: str = "private") -> tuple:
    """随机域 + owner 绑域 + 私有 KB，返回 (kbdb, owner_user, kb_row)。"""
    kbdb = KbDB(async_pool)
    domain = f"dom-{_hex()}"
    owner = await _user(async_pool)
    await kbdb.bind_domain(user_id=owner["id"], domain=domain)
    kb = await kbdb.create_kb(
        domain=domain, name=f"云核产品库-{_hex(6)}", owner_id=owner["id"],
        visibility=visibility,
    )
    return kbdb, owner, kb


async def _add_doc(async_pool, kbdb, kb: dict, owner: dict, *, name: str,
                   directory: str = "") -> dict:
    """造一个 KB 文档（必要时先建 kb_folders 行——insert 校验目录存在）。"""
    if directory:
        async with async_pool.connection() as conn:
            await conn.execute(
                """INSERT INTO kb_folders (id, kb_id, parent_id, name, path,
                                           created_at)
                   VALUES (%s, %s, NULL, %s, %s, %s)""",
                (f"fld_{_hex(12)}", kb["id"], directory.rsplit("/", 1)[-1],
                 directory, f"2026-11-16T00:00:00+00:00"),
            )
    return await kbdb.insert_document_from_storage(
        domain=kb["domain"], kb_id=kb["id"], document_key=f"doc:/{name}",
        document_name=name, storage_object_id=f"so-{_hex(12)}",
        source_raw_hash=f"sha-{_hex(12)}", directory_path=directory,
        document_type="reference", owner_id=owner["id"], file_size=128,
    )


def _heading(index: int, chain: list[tuple[int, str]]) -> dict:
    """heading 段：section_path 含自身（编译器 stack 语义），title=最内层。"""
    path = [{"level": lv, "title": ti} for lv, ti in chain]
    return {"segment_index": index, "block_type": "heading",
            "section_path": path, "section_title": path[-1]["title"],
            "raw_text": path[-1]["title"]}


def _para(index: int, chain: list[tuple[int, str]], text: str) -> dict:
    path = [{"level": lv, "title": ti} for lv, ti in chain]
    return {"segment_index": index, "block_type": "paragraph",
            "section_path": path,
            "section_title": path[-1]["title"] if path else None,
            "raw_text": text}


async def _seed_parse(async_pool, kb: dict, doc: dict,
                      segments: list[dict], *, revision: int | None = None,
                      ) -> str:
    """造解析结果三件套：READY 快照 + 锚定当前对象/修订的 link + 切片行。

    revision 显式传错值 = 模拟「快照锚在旧内容版本」（最新修订无解析结果）。"""
    snap_id = f"snap-{_hex(12)}"
    async with async_pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO asset_document_snapshots
                   (id, domain, normalized_content_hash, raw_content_hash,
                    mime_type, title, created_at, snapshot_fingerprint,
                    lifecycle_status)
                   VALUES (%s, %s, %s, %s, 'text/markdown', %s, %s, %s,
                           'READY')""",
                (snap_id, kb["domain"], f"n-{_hex(12)}", f"r-{_hex(12)}",
                 doc["document_name"], "2026-11-16T00:00:00+00:00",
                 f"fp-{_hex(16)}"),
            )
            await conn.execute(
                """INSERT INTO asset_document_snapshot_links
                   (id, document_id, document_snapshot_id, relative_path,
                    source_uri, title, linked_at, metadata_json,
                    source_storage_object_id, source_content_revision)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)""",
                (f"lnk-{_hex(12)}", doc["id"], snap_id, "k", "k",
                 doc["document_name"], "2026-11-16T00:00:00+00:00",
                 doc["storage_object_id"],
                 doc["content_revision"] if revision is None else revision),
            )
            for seg in segments:
                await conn.execute(
                    """INSERT INTO asset_raw_segments
                       (id, document_snapshot_id, segment_key, segment_index,
                        block_type, section_path, section_title, raw_text,
                        normalized_text, content_hash, normalized_hash)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                               %s, %s)""",
                    (f"seg-{_hex(12)}", snap_id,
                     f"{doc['document_key']}#{seg['segment_index']}",
                     seg["segment_index"], seg["block_type"],
                     json.dumps(seg["section_path"], ensure_ascii=False),
                     seg["section_title"], seg["raw_text"], seg["raw_text"],
                     f"ch-{_hex(10)}", f"nh-{_hex(10)}"),
                )
    return snap_id


async def _seed_corpus(async_pool) -> dict:
    """B.3 语义固定材料：P1（L1+L2+L3 折叠）、P2（首段前导+L1/L2）、
    P3 无解析结果、P4 快照锚旧修订——四文档两目录。"""
    kbdb, owner, kb = await _seed_kb(async_pool)
    p1 = await _add_doc(async_pool, kbdb, kb, owner, name="核心网产品概述.md")
    p2 = await _add_doc(async_pool, kbdb, kb, owner, name="割接方案.md",
                        directory="方案")
    p3 = await _add_doc(async_pool, kbdb, kb, owner, name="未解析资料.md")
    p4 = await _add_doc(async_pool, kbdb, kb, owner, name="旧修订文档.md")
    snap1 = await _seed_parse(async_pool, kb, p1, [
        _heading(0, [(1, "产品定位")]),
        _para(1, [(1, "产品定位")], "面向核心网云化场景的产品线定位陈述。"),
        _heading(2, [(1, "组网要求"), (2, "硬件约束")]),
        _para(3, [(1, "组网要求"), (2, "硬件约束")],
              "单机柜功率上限 12kW，电源双路冗余。"),
        _heading(4, [(1, "组网要求"), (2, "硬件约束"), (3, "板卡插槽")]),
        _para(5, [(1, "组网要求"), (2, "硬件约束"), (3, "板卡插槽")],
              "主控槽位与交换板槽位禁止混插。"),
    ])
    snap2 = await _seed_parse(async_pool, kb, p2, [
        _para(0, [], "本方案适用于 2026 年度核心网割接窗口。"),
        # 真实编译形态：带正文的标题并入正文段（无独立 heading 行，链含结构，
        # 文本带标题前缀——_build_sections 按链前缀重建）
        _para(1, [(1, "割接流程")], "割接流程\n先备份配置，再逐台切换。"),
        _heading(2, [(1, "割接流程"), (2, "回退预案")]),
        _para(3, [(1, "割接流程"), (2, "回退预案")],
              "切换失败 30 分钟内回退至旧版本。"),
    ])
    # P3：完全不造解析结果；P4：link 锚在 revision=99（当前是 1）
    await _seed_parse(async_pool, kb, p4, [
        _heading(0, [(1, "旧内容")]),
    ], revision=99)
    return {"kbdb": kbdb, "owner": owner, "kb": kb,
            "docs": {"p1": p1, "p2": p2, "p3": p3, "p4": p4},
            "snaps": {"p1": snap1, "p2": snap2}}


# ------------------------------------------------------------ read_kb_dir

@pytest.mark.asyncio
async def test_read_kb_dir_permission_and_not_found(async_pool) -> None:
    env = await _seed_corpus(async_pool)
    stranger = await _user(async_pool)
    await env["kbdb"].bind_domain(
        user_id=stranger["id"], domain=env["kb"]["domain"])
    async with async_pool.connection() as conn:
        # 同域非成员对私有库：不可读 → Permission（谓词镜像 is_visible）
        with pytest.raises(ws.WorkspacePermissionError):
            await ws.read_kb_dir(
                conn, kb_id=env["kb"]["id"], directories=[],
                user=_ctx(stranger))
        # 不存在的 kb → NotFound
        with pytest.raises(WorkspaceNotFoundError):
            await ws.read_kb_dir(
                conn, kb_id="kb_missing", directories=[],
                user=_ctx(env["owner"]))


@pytest.mark.asyncio
async def test_read_kb_dir_builds_dataset_and_manifest(async_pool) -> None:
    env = await _seed_corpus(async_pool)
    async with async_pool.connection() as conn:
        dataset, manifest = await ws.read_kb_dir(
            conn, kb_id=env["kb"]["id"], directories=[],
            user=_ctx(env["owner"]))

    kb_name = env["kb"]["name"]
    assert dataset["cols"] == ["知识库", "目录", "文件", "文档内容_JSON"]
    rows = dataset["rows"]
    assert len(rows) == 2  # P3 无解析、P4 锚旧修订 → 不产出行
    by_file = {r["文件"]: r for r in rows}
    assert set(by_file) == {"核心网产品概述.md", "割接方案.md"}

    r1 = by_file["核心网产品概述.md"]
    assert r1["_id"] == env["docs"]["p1"]["id"]
    # 根目录：目录段以 '/' 占位（规格 f"{kb名}/{目录或'/'}/{文件名}" 字面语义）
    assert r1["_source"] == f"{kb_name}///核心网产品概述.md"
    assert r1["知识库"] == kb_name and r1["目录"] == ""
    body = json.loads(r1["文档内容_JSON"])
    assert body["document_id"] == env["docs"]["p1"]["id"]
    assert body["title"] == "核心网产品概述.md"
    assert [(s["level"], s["title"]) for s in body["sections"]] == [
        (1, "产品定位"), (1, "组网要求"), (2, "硬件约束")]
    # L3「板卡插槽」不单列——其文本折叠进 L2「硬件约束」
    sec_hw = body["sections"][2]
    assert "单机柜功率上限 12kW" in sec_hw["text"]
    assert "主控槽位与交换板槽位禁止混插" in sec_hw["text"]
    assert "板卡插槽" not in [s["title"] for s in body["sections"]]

    r2 = by_file["割接方案.md"]
    assert r2["_source"] == f"{kb_name}/方案/割接方案.md"
    body2 = json.loads(r2["文档内容_JSON"])
    # 首个 heading 前的前导段 → title="" 的 L1 伪节（不静默丢内容）
    assert body2["sections"][0] == {
        "level": 1, "title": "", "text": "本方案适用于 2026 年度核心网割接窗口。"}
    assert [(s["level"], s["title"]) for s in body2["sections"][1:]] == [
        (1, "割接流程"), (2, "回退预案")]
    # 并入正文段的标题：节由链开出来，text 保留标题前缀（不去重）
    assert body2["sections"][1]["text"] == "割接流程\n先备份配置，再逐台切换。"

    validate_dataset(dataset)  # 契约合法（断言不抛）
    assert manifest["doc_count"] == 2
    assert manifest["frozen_at"]
    assert {u["document_id"] for u in manifest["unparsed_docs"]} == {
        env["docs"]["p3"]["id"], env["docs"]["p4"]["id"]}
    docs = {d["document_id"]: d for d in manifest["documents"]}
    assert set(docs) == {env["docs"]["p1"]["id"], env["docs"]["p2"]["id"]}
    assert docs[env["docs"]["p1"]["id"]]["content_revision"] == 1
    assert docs[env["docs"]["p1"]["id"]]["snapshot_id"] == env["snaps"]["p1"]
    # 冻结统一语义：manifest 内嵌整份数据集（两类连接器 materialize 同构）
    assert manifest["dataset"] == dataset


@pytest.mark.asyncio
async def test_read_kb_dir_scope_directory_filter(async_pool) -> None:
    env = await _seed_corpus(async_pool)
    async with async_pool.connection() as conn:
        sub = await _add_doc(async_pool, env["kbdb"], env["kb"], env["owner"],
                             name="子目录规格.md", directory="方案/子目录")
        await _seed_parse(async_pool, env["kb"], sub, [
            _heading(0, [(1, "子目录章节")]),
        ])
        dataset, manifest = await ws.read_kb_dir(
            conn, kb_id=env["kb"]["id"], directories=["方案"],
            user=_ctx(env["owner"]))
    # 精确命中（割接方案.md）+ 子目录前缀（子目录规格.md）；根目录 P1 不进
    assert {r["文件"] for r in dataset["rows"]} == {
        "割接方案.md", "子目录规格.md"}
    assert manifest["doc_count"] == 2
    assert manifest["unparsed_docs"] == []  # scope 外文档不进 unparsed 账


@pytest.mark.asyncio
async def test_read_kb_dir_doc_cap(async_pool, monkeypatch) -> None:
    env = await _seed_corpus(async_pool)  # 库内 4 文档
    monkeypatch.setattr(ws, "MAX_DOCS", 3)
    async with async_pool.connection() as conn:
        with pytest.raises(WorkspaceValidationError) as ei:
            await ws.read_kb_dir(
                conn, kb_id=env["kb"]["id"], directories=[],
                user=_ctx(env["owner"]))
    assert "4" in str(ei.value)  # 错误带实际数量


# ------------------------------------------------------------ parse_excel

def _xlsx_bytes(*sheets: tuple[str, list, list[list]]) -> bytes:
    """内存构造 xlsx：sheets=(标题, 表头行, 数据行)。"""
    wb = Workbook()
    wb.remove(wb.active)
    for title, header, data in sheets:
        sheet = wb.create_sheet(title=title)
        sheet.append(header)
        for row in data:
            sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_excel_types_dedupe_and_identity() -> None:
    content = _xlsx_bytes(
        ("功率表", ["型号", "功率", "功率", "备注"],
         [["P1", 100, "12kW", None], ["P2", 200, "20kW", "加急"],
          ["P3", 300, "30kW", None]]),
        ("第二张", ["仅列"], [["值"]]),
    )
    dataset, manifest = ws.parse_excel(content, file_name="产品功率表.xlsx")
    # 表头去重：第二个「功率」→ 功率_2
    assert dataset["cols"] == ["型号", "功率", "功率_2", "备注"]
    r1, r2 = dataset["rows"][0], dataset["rows"][1]
    assert r1["_id"] == "row_1" and r2["_id"] == "row_2"  # 序号 hex 身份
    assert r1["_source"] == "产品功率表.xlsx/功率表"
    assert r1["功率"] == 100 and isinstance(r1["功率"], int)  # int 推断
    assert r1["功率_2"] == "12kW"  # 文本列
    assert r1["备注"] is None  # 空单元格 → None（合法缺列值）
    validate_dataset(dataset)
    assert manifest == {
        "file_name": "产品功率表.xlsx", "sheet_name": "功率表",
        "row_count": 3, "frozen_at": manifest["frozen_at"],
        "dataset": dataset,
    }


def test_parse_excel_sheet_selection_and_unknown() -> None:
    content = _xlsx_bytes(
        ("S1", ["列A"], [["a1"]]), ("S2", ["列B"], [["b1"], ["b2"]]))
    dataset, manifest = ws.parse_excel(
        content, file_name="双表.xlsx", sheet_name="S2")
    assert manifest["sheet_name"] == "S2" and manifest["row_count"] == 2
    assert dataset["cols"] == ["列B"]
    assert [r["列B"] for r in dataset["rows"]] == ["b1", "b2"]
    with pytest.raises(WorkspaceValidationError):
        ws.parse_excel(content, file_name="双表.xlsx", sheet_name="不存在")


def test_parse_excel_empty_sheet_rejected() -> None:
    content = _xlsx_bytes(("空表", ["只有表头"], []))
    with pytest.raises(WorkspaceValidationError):
        ws.parse_excel(content, file_name="空.xlsx")


def test_parse_excel_rejects_bad_zip_and_bomb() -> None:
    # PK 头 + 垃圾：BadZipFile → invalid_xlsx 语义
    with pytest.raises(WorkspaceValidationError):
        ws.parse_excel(b"PK\x03\x04" + b"\x00" * 4096, file_name="坏.xlsx")
    # 真炸弹：10MB 零字节压缩后 ~10KB，压缩比 >100 → 拒
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\x00" * (10 * 1024 * 1024))
    with pytest.raises(WorkspaceValidationError):
        ws.parse_excel(bomb.getvalue(), file_name="炸.xlsx")


# ------------------------------------------------------------ materialize

@pytest.mark.asyncio
async def test_materialize_source_roundtrip_both_connectors(
        async_pool) -> None:
    env = await _seed_corpus(async_pool)
    async with async_pool.connection() as conn:
        kb_dataset, kb_manifest = await ws.read_kb_dir(
            conn, kb_id=env["kb"]["id"], directories=["方案"],
            user=_ctx(env["owner"]))
    excel_dataset, excel_manifest = ws.parse_excel(
        _xlsx_bytes(("功率表", ["型号", "功率"],
                     [["P1", 100], ["P2", 200]])),
        file_name="功率.xlsx")

    # 两类连接器对 materialize_source 同构：都从 input_manifest.dataset 取
    assert ws.materialize_source(
        {"connector": "kb_dir", "input_manifest": kb_manifest}) == kb_dataset
    assert ws.materialize_source(
        {"connector": "excel", "input_manifest": excel_manifest}) == \
        excel_dataset
    validate_dataset(ws.materialize_source(
        {"connector": "kb_dir", "input_manifest": kb_manifest}))

    # 缺 dataset 键（旧形态/脏数据）→ 显式错误，提示重新接入来源
    with pytest.raises(WorkspaceValidationError) as ei:
        ws.materialize_source({"connector": "kb_dir", "input_manifest": {}})
    assert "重新接入" in str(ei.value)
    with pytest.raises(WorkspaceValidationError):
        ws.materialize_source({"connector": "excel"})


# ------------------------------------------------- Task 6 携带小修（LOW）

def test_fallback_value_normalizes_time_to_isoformat() -> None:
    """Task 6 质量审查 LOW：datetime.time 可过预算闸（default=str）但在
    canonical 序列化（无 default）爆 TypeError——兜底必须归一 ISO。"""
    from datetime import datetime as dt
    from datetime import time as dtime

    assert ws._fallback_value(dtime(9, 30, 5)) == "09:30:05"
    assert ws._fallback_value(dt(2026, 1, 2, 3, 4)) == "2026-01-02T03:04:00"
    assert ws._fallback_value("x") == "x"
    assert ws._fallback_value(None) is None


def test_ensure_budget_error_carries_actual_bytes(monkeypatch) -> None:
    """Task 6 质量审查 LOW：预算超限错误消息带实际字节数（无字节数无法
    判断超了多少——排查冻结预算问题的唯一线索）。"""
    monkeypatch.setattr(ws, "MAX_SERIALIZED_BYTES", 8)
    payload = {"dataset": {"cols": ["列a"], "rows": []}}
    with pytest.raises(WorkspaceValidationError) as ei:
        ws._ensure_budget(payload)
    expected = len(json.dumps(
        payload, ensure_ascii=False, default=str).encode("utf-8"))
    assert str(expected) in str(ei.value)
    assert "8" in str(ei.value)
