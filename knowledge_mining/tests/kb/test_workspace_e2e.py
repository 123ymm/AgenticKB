# -*- coding: utf-8 -*-
"""工作台 MVP 端到端真库验收（67号 Task 13）——64号 B.3 固定材料全链。

一条用例穿透 Task 1-12 全链：登记（create/put_graph/来源接入即冻结）→
执行引擎（全区跑/node 跑）→ 确定性算子 + LLM 算子（fake llm_factory 注入，
按文本正则抽 型号/功率——确定性，不调真 llm_service）→ 分支 partial /
级联标败 / 旧 current 输出守恒 → 导出交付（幂等 request_id / 单事务私有
草稿 / 绑定 / 不写批次）→ 制品行 provenance 血缘回溯到 KB 名与文件名。

B.3 口径与行级 llm 的调和（本文件的关键设计决策）：

- split_sections(level=1) 逐文档展开 3 个 L1 章节 → 9 行（断言）；
  llm_extract 行级保留全部 9 行（断言）——「产品表行」与「纯章节行」混
  在同一张章节长表里。而 64号 B.3 的验收数字要求 join 左表=每产品一行的
  产品表（左关联 3 行、筛选后 2 行），故在 llm 与 join 之间加一道
  ``fspec = filter(型号, not_empty)`` 把 9 行收敛为 3 行产品表（无型号
  的章节行不进关联——真实选型场景也须先剔除非产品行再关联库存）。
- 型号/功率 集中在「技术规格」节文本（抽取源）；「基本属性」节只含部件
  编码——型号若也出现在基本属性节，fspec 将保留 6 行而非 3 行，B.3 数字
  不再成立。
- null 键/无抽取结果的行：fake 返回 ``{"型号": null, "功率_W": null}``
  （_cast_value 对 blank 一律 (True, None)）——行成功、新列 None，llm
  quality 无 partial（B.4 完整性：缺失显式为 None 不冒充）。

右键重复铁律（B.3）：重传含重复 P1 行的库存 Excel（同 node_id 重 PUT
source，batch_no 推进）→ 重跑全区 → join 节点失败（duplicate right key
语义）且**旧 current 输出守恒**（行数/指纹不变）；下游 filter 级联
「上游失败」；split/llm 等其余分支照常完成（分支 partial 语义）。

真库跑法与前例同款（容器命令见计划文档 Task 0；随机域前缀隔离）。
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import uuid

import pytest
from openpyxl import Workbook

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_artifacts import (
    ArtifactDB,
    _encode_key_parts,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.workspace_executor import WorkspaceExecutor
from knowledge_mining.mining.kb.workspace_service import WorkspaceService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_TERMINAL = ("completed", "partial", "failed", "cancelled")

#: B.3 固定材料：(标签, 型号, 功率W, 电源类型, 部件编码)。
_PRODUCTS = [
    ("P1", "PAC3KS54-NE", 3000, "交流", "54-0031"),
    ("P2", "PAC2KS54-NE", 2000, "交流", "54-0032"),
    ("P3", "PDC3KS54-NE", 1200, "直流", "54-0033"),
]

#: 库存表（型号/可用库存）：正常=P1/5、P3/0；重复=多一行 P1/3。
_INV_CLEAN = (("PAC3KS54-NE", 5), ("PDC3KS54-NE", 0))
_INV_DUP = (("PAC3KS54-NE", 5), ("PAC3KS54-NE", 3), ("PDC3KS54-NE", 0))

#: fake llm 的确定性抽取正则（锚定「型号/功率」前缀，prompt 前缀文本无
#: 大写字母 token/数字，不会被误命中）。
_MODEL_RE = re.compile(r"型号[：:]?\s*([A-Z][A-Z0-9\-]+)")
_POWER_RE = re.compile(r"功率[：:]?\s*(\d+)\s*W")


# ------------------------------------------------------------ 确定性 LLM 桩


class RegexExtractLlmFake:
    """llm_factory 注入桩：submit_chat 登记渲染后 prompt，wait_for_tasks
    按正则即时产出 ``{"型号": ..., "功率_W": ...}``（无命中 → null——行成功
    但新列 None）；不调真 llm_service（B.3：LLM 质量不混入确定性验收）。"""

    def __init__(self) -> None:
        self.submits: list[dict] = []

    def submit_chat(
        self, messages, *, expected_output_type=None, idempotency_key=None,
        model=None, knowledge_domain=None, **_kwargs,
    ) -> str:
        self.submits.append({"messages": messages, "key": idempotency_key,
                             "domain": knowledge_domain})
        return f"t{len(self.submits)}"

    def wait_for_tasks(self, task_ids, *, timeout=None,
                       poll_interval=None) -> dict:
        out: dict[str, dict] = {}
        for tid in task_ids:
            text = self.submits[int(tid[1:]) - 1]["messages"][0]["content"]
            model = _MODEL_RE.search(text)
            power = _POWER_RE.search(text)
            payload = {"型号": model.group(1) if model else None,
                       "功率_W": power.group(1) if power else None}
            out[tid] = {"task_id": tid, "status": "succeeded",
                        "result": {"text_output": json.dumps(
                            payload, ensure_ascii=False),
                            "parse_status": "ok"}}
        return out


# ------------------------------------------------------------ 造数材料


def _hex(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def _heading(index: int, chain: list[tuple[int, str]]) -> dict:
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


def _doc_segments(part: str, model: str, power: int, acdc: str) -> list[dict]:
    """单产品文档的 L1 三章节（基本属性/技术规格/配套关系）。

    型号/功率只在「技术规格」节出现（见模块 docstring 调和决策）。"""
    return [
        _heading(0, [(1, "基本属性")]),
        _para(1, [(1, "基本属性")],
              f"部件编码 {part}，所属产品线为电源模块，通过 CE 认证。"),
        _heading(2, [(1, "技术规格")]),
        _para(3, [(1, "技术规格")],
              f"型号 {model}，功率 {power}W，电源类型 {acdc}，输入电压 220V。"),
        _heading(4, [(1, "配套关系")]),
        _para(5, [(1, "配套关系")],
              "配套电池组与标准机柜联合部署，详见配套清单。"),
    ]


async def _seed_parse(async_pool, kb: dict, doc: dict,
                      segments: list[dict]) -> None:
    """造解析结果三件套（READY 快照+锚定当前对象/修订的 link+切片行），
    手法照 test_workspace_sources._seed_parse。"""
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
                   VALUES (%s, %s, %s, 'k', 'k', %s, %s, '{}'::jsonb, %s, %s)""",
                (f"lnk-{_hex(12)}", doc["id"], snap_id, doc["document_name"],
                 "2026-11-16T00:00:00+00:00", doc["storage_object_id"],
                 doc["content_revision"]),
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


def _inventory_bytes(rows: tuple[tuple[str, int], ...]) -> bytes:
    """库存表 xlsx 字节（单 sheet：型号/可用库存）。"""
    wb = Workbook()
    wb.remove(wb.active)
    sheet = wb.create_sheet(title="Sheet1")
    sheet.append(["型号", "可用库存"])
    for row in rows:
        sheet.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _node(nid: str, op: str, *, name: str, inputs: tuple[str, ...] = (),
          config: dict | None = None) -> dict:
    return {"id": nid, "op": op, "name": name, "config": config or {},
            "inputs": list(inputs),
            "ports": [{"id": "main", "name": "主输出", "kind": "table"}],
            "x": 0, "y": 0}


def _graph_nodes() -> list[dict]:
    """B.3 全链图（7 节点）：kb_dir 源→拆章节→LLM 抽取→产品表行过滤→
    左关联库存→功率筛选；excel 库存源见 inv（见模块 docstring 的 fspec
    调和决策）。"""
    return [
        _node("src", "source", name="产品文档目录"),
        _node("inv", "source", name="库存表"),
        _node("split", "split_sections", name="拆一级章节",
              inputs=("src:main",), config={"level": 1}),
        _node("llm", "llm_extract", name="抽取型号功率",
              inputs=("split:main",),
              config={"input_column": "内容",
                      "prompt_template": "从下文抽取型号与功率，返回JSON"
                                         "字段 型号/功率_W：{内容}",
                      "output_fields": [{"name": "型号", "type": "text"},
                                        {"name": "功率_W", "type": "int"}]}),
        _node("fspec", "filter", name="产品表行（型号非空）",
              inputs=("llm:main",),
              config={"field": "型号", "cond": "not_empty"}),
        _node("join", "join", name="左关联库存",
              inputs=("fspec:main", "inv:main"),
              config={"method": "left", "left_key": "型号",
                      "right_key": "型号"}),
        _node("fpower", "filter", name="功率≥2000",
              inputs=("join:main",),
              config={"field": "功率_W", "cond": "gte", "value": 2000}),
    ]


# ------------------------------------------------------------ 环境装配


async def _seed_env(async_pool) -> dict:
    """域+owner+私有 KB（3 篇带 L1 章节文档）+ 工作区 + 图 + 两来源。"""
    kbdb = KbDB(async_pool)
    domain = f"dom-{_hex()}"
    owner = await kbdb.create_user(username=f"we2e-{_hex(10)}")
    owner_id = str(owner["id"])
    # 2026-11-17 收紧：owner 提升为域管理员（bind_domain domain_role='admin'）
    # ——服务层只认站点/域管理员，普通绑定成员建台/读写全被拒。
    await kbdb.bind_domain(user_id=owner_id, domain=domain,
                           domain_role="admin")
    kb = await kbdb.create_kb(
        domain=domain, name=f"电源模块产品库-{_hex(6)}",
        owner_id=owner_id, visibility="private")
    docs: dict[str, dict] = {}
    for tag, model, power, acdc, part in _PRODUCTS:
        name = f"电源模块{tag}.md"
        doc = await kbdb.insert_document_from_storage(
            domain=domain, kb_id=str(kb["id"]), document_key=f"doc:/{name}",
            document_name=name, storage_object_id=f"so-{_hex(12)}",
            source_raw_hash=f"sha-{_hex(12)}", directory_path="",
            document_type="reference", owner_id=owner_id, file_size=256)
        await _seed_parse(async_pool, kb, doc,
                          _doc_segments(part, model, power, acdc))
        docs[tag] = doc

    ctx = {"id": owner_id, "username": owner["username"],
           "site_role": "member", "status": "active"}
    fake = RegexExtractLlmFake()
    svc = WorkspaceService(
        async_pool, WorkspaceExecutor(async_pool, llm_factory=lambda: fake))
    ws = await svc.create_workspace(
        user=ctx, domain_id=domain, name="产品选型工作台",
        description="B.3 固定材料全链验收")
    ws_id = str(ws["id"])
    put = await svc.put_graph(user=ctx, workspace_id=ws_id,
                              graph={"nodes": _graph_nodes()},
                              expected_revision=0)
    assert put["graph_revision"] == 1
    # kb_dir 接入：batch_no 1→2、底表 3 行文档（接入即冻结，全部可解析）
    r1 = await svc.upsert_source_kb_dir(
        user=ctx, workspace_id=ws_id, node_id="src", kb_id=str(kb["id"]))
    assert r1["batch_no"] == 2
    assert r1["frozen"] == {"doc_count": 3, "unparsed_count": 0}
    # excel 库存源（第二 source 节点）：batch_no 2→3
    r2 = await svc.upsert_source_excel(
        user=ctx, workspace_id=ws_id, node_id="inv",
        content=_inventory_bytes(_INV_CLEAN), file_name="库存表.xlsx")
    assert r2["batch_no"] == 3 and r2["frozen"]["row_count"] == 2
    return {"svc": svc, "ctx": ctx, "ws": ws, "kb_name": kb["name"],
            "docs": docs, "fake": fake,
            "rdb": WorkspaceRunDB(async_pool),
            "adb": ArtifactDB(async_pool)}


async def _start_and_wait(env: dict, *, scope: str = "workspace",
                          target: str | None = None) -> tuple[str, dict]:
    """service 层启动 run 并轮询至终态（超时 30s，口径照验收剧本）。"""
    svc, ctx, ws_id = env["svc"], env["ctx"], str(env["ws"]["id"])
    run_id = (await svc.start_run(user=ctx, workspace_id=ws_id, scope=scope,
                                  target_node_id=target))["run_id"]

    async def _poll() -> dict:
        while True:
            status = await svc.get_run_status(user=ctx, workspace_id=ws_id,
                                              run_id=run_id)
            if status["run"]["status"] in _TERMINAL:
                return status["run"]
            await asyncio.sleep(0.02)

    return run_id, await asyncio.wait_for(_poll(), 30.0)


async def _current(env: dict) -> dict:
    out = await env["svc"].get_current_outputs(
        user=env["ctx"], workspace_id=str(env["ws"]["id"]))
    return out["outputs"]


# ------------------------------------------------------------ 阶段断言


def _assert_run1_accounting(env: dict, outputs: dict) -> None:
    """全区跑完成后的逐级账（B.3：9/9/3/2）+ 各级 quality_json。"""
    assert set(outputs) == {"src:main", "inv:main", "split:main", "llm:main",
                            "fspec:main", "join:main", "fpower:main"}
    assert outputs["src:main"]["row_count"] == 3          # 底表 3 行文档
    assert outputs["src:main"]["quality_json"] == {"rows": 3}
    assert outputs["inv:main"]["row_count"] == 2

    split = outputs["split:main"]                          # 3 文档×3 章节
    assert split["row_count"] == 9
    assert split["quality_json"] == {"no_section_docs": []}
    assert split["content_inline"]["cols"] == ["文件", "章节", "内容"]
    split_ids = {r["_id"] for r in split["content_inline"]["rows"]}
    for doc in env["docs"].values():
        assert {f"{doc['id']}:{t}"
                for t in ("基本属性", "技术规格", "配套关系")} <= split_ids

    llm = outputs["llm:main"]                              # 9 行全保留
    assert llm["row_count"] == 9
    assert llm["quality_json"] == {"total": 9, "succeeded": 9,
                                   "failed_row_ids": [], "retried": 0,
                                   "partial": False}
    assert llm["content_inline"]["cols"] == ["文件", "章节", "内容",
                                             "型号", "功率_W"]
    llm_rows = {r["_id"]: r for r in llm["content_inline"]["rows"]}
    for tag, model, power, _acdc, _part in _PRODUCTS:
        spec = llm_rows[f"{env['docs'][tag]['id']}:技术规格"]
        assert spec["型号"] == model and spec["功率_W"] == power
        base = llm_rows[f"{env['docs'][tag]['id']}:基本属性"]
        assert base["型号"] is None and base["功率_W"] is None

    fspec = outputs["fspec:main"]                          # 产品表 3 行
    assert fspec["row_count"] == 3
    assert fspec["quality_json"] == {"input_rows": 9, "filtered": 6}

    join = outputs["join:main"]                            # 左关联 3 行
    assert join["row_count"] == 3
    assert join["quality_json"] == {"matched": 2, "unmatched_left": 1}
    join_cols = join["content_inline"]["cols"]
    assert "右表_型号" in join_cols and "可用库存" in join_cols
    by_model = {r["型号"]: r for r in join["content_inline"]["rows"]}
    kb_name, docs = env["kb_name"], env["docs"]
    p1 = by_model["PAC3KS54-NE"]
    assert p1["可用库存"] == 5 and p1["右表_型号"] == "PAC3KS54-NE"
    assert p1["_source"] == (f"{kb_name}///{docs['P1']['document_name']}"
                             "；库存表.xlsx/Sheet1")       # _source 合并
    p2 = by_model["PAC2KS54-NE"]
    assert p2["可用库存"] is None                          # P2 库存空（B.3）
    assert p2["_source"] == \
        f"{kb_name}///{docs['P2']['document_name']}"       # 未匹配仅左

    fpower = outputs["fpower:main"]                        # ≥2000 → 2 行
    assert fpower["row_count"] == 2
    assert fpower["quality_json"] == {"input_rows": 3, "filtered": 1}
    power_rows = {r["型号"]: r for r in fpower["content_inline"]["rows"]}
    assert set(power_rows) == {"PAC3KS54-NE", "PAC2KS54-NE"}
    assert power_rows["PAC3KS54-NE"]["功率_W"] == 3000
    assert power_rows["PAC3KS54-NE"]["可用库存"] == 5
    assert power_rows["PAC2KS54-NE"]["功率_W"] == 2000
    assert power_rows["PAC2KS54-NE"]["可用库存"] is None


async def _assert_dup_partial(env: dict, run1_id: str,
                              join_hash_before: str) -> None:
    """右键重复铁律：重传含重复 P1 的库存 → 全区重跑 → partial + 旧
    current 输出守恒 + 级联标败 + 其余分支照常。"""
    svc, ctx, ws_id = env["svc"], env["ctx"], str(env["ws"]["id"])
    dup = await svc.upsert_source_excel(
        user=ctx, workspace_id=ws_id, node_id="inv",
        content=_inventory_bytes(_INV_DUP), file_name="库存表.xlsx")
    assert dup["batch_no"] == 4                            # 重 PUT 推进
    assert dup["frozen"]["row_count"] == 3

    run2_id, run2 = await _start_and_wait(env)
    assert run2["status"] == "partial"
    errors = run2["progress_json"]["node_errors"]
    assert set(errors) == {"join", "fpower"}
    assert "duplicate right key" in errors["join"]         # 右键重复语义
    assert "PAC3KS54-NE" in errors["join"]
    assert errors["fpower"].startswith("上游失败")          # 级联标败

    outputs = await _current(env)
    join = outputs["join:main"]                            # 旧输出守恒
    assert join["row_count"] == 3
    assert join["content_hash"] == join_hash_before
    assert join["run_id"] == run1_id                       # 仍是 run1 的产物
    assert join["stale_since_run"] == run2_id              # 上游重跑的账
    for key, count in (("src:main", 3), ("inv:main", 3), ("split:main", 9),
                       ("llm:main", 9), ("fspec:main", 3)):
        assert outputs[key]["row_count"] == count          # 其余分支照常
        assert outputs[key]["run_id"] == run2_id


async def _assert_export_and_lineage(env: dict, run3_id: str) -> str:
    """导出交付：私有草稿/rev0=2 行/provenance 血缘/绑定/零批次/幂等。"""
    svc, ctx, ws_id = env["svc"], env["ctx"], str(env["ws"]["id"])
    request_id = f"req-{_hex(12)}"
    exported = await svc.export_drafts(
        ws_id, ctx, request_id=request_id, items=[
            {"node_id": "fpower", "port_id": "main",
             "artifact_name": "选型结果表"}])
    [item] = exported["exports"]
    assert item["status"] == "succeeded" and "error" not in item
    art_id = item["artifact_id"]
    assert art_id.startswith("art_")

    adb = env["adb"]
    art = await adb.get_artifact(art_id)
    assert art["status"] == "draft" and art["current_revision"] == 0
    assert art["visibility"] == "private" and art["kind"] == "table"
    assert art["title"] == "选型结果表"
    assert art["domain_id"] == env["ws"]["domain_id"]
    assert art["owner_user_id"] == str(ctx["id"])          # owner 默认

    assert await adb.count_rows(art_id, 0) == 2            # rev0=2 行
    out_rows = (await _current(env))["fpower:main"]["content_inline"]["rows"]
    listing = await adb.list_rows(art_id, 0, size=10)
    by_key = {r["row_key"]: r for r in listing["rows"]}
    for src in out_rows:
        row = by_key[_encode_key_parts([str(src["_id"])])]
        assert row["data"]["型号"] == src["型号"]
        assert row["data"]["功率_W"] == src["功率_W"]
        assert row["provenance"] == [{
            "_source": src["_source"],
            "workspace": {"id": ws_id, "node": "fpower", "port": "main",
                          "run_id": run3_id}}]

    # 血缘追溯：抽一行（P1）沿 _source 断言含 KB 名与文件名
    p1 = next(r for r in out_rows if r["型号"] == "PAC3KS54-NE")
    assert env["kb_name"] in p1["_source"]
    assert env["docs"]["P1"]["document_name"] in p1["_source"]
    assert "库存表.xlsx/Sheet1" in p1["_source"]

    binding = await env["rdb"].get_binding(
        workspace_id=ws_id, node_id="fpower", port_id="main")
    assert binding["artifact_id"] == art_id
    assert binding["last_run_id"] == run3_id
    export_row = await env["rdb"].get_export_by_request(
        request_id=request_id)
    assert export_row["status"] == "succeeded"
    assert export_row["target_artifact_id"] == art_id

    # 幂等：同 request_id 二次 → 同 artifact + duplicate + 行数不变
    again = await svc.export_drafts(
        ws_id, ctx, request_id=request_id, items=[
            {"node_id": "fpower", "port_id": "main",
             "artifact_name": "选型结果表"}])
    assert again["duplicate"] is True
    assert again["exports"][0]["artifact_id"] == art_id
    assert await adb.count_rows(art_id, 0) == 2
    return art_id


# ------------------------------------------------------------ 端到端用例


async def test_b3_fixed_material_full_chain(async_pool) -> None:
    """B.3 固定材料全链：造数→建区→接源→全区跑→断言链→node 跑复用账→
    右键重复守恒→恢复重跑→导出→幂等→血缘。"""
    env = await _seed_env(async_pool)
    fake = env["fake"]

    # ---- 3. 运行全区 → completed（全节点成功）
    run1_id, run1 = await _start_and_wait(env)
    assert run1["status"] == "completed"
    assert len(run1["progress_json"]["done"]) == 7
    assert "node_errors" not in (run1["progress_json"] or {})
    outputs = await _current(env)
    _assert_run1_accounting(env, outputs)
    join_hash_1 = outputs["join:main"]["content_hash"]
    # fake llm 传参钉：幂等键 wsop:{run}:{node}:{行}、域=工作区域
    assert len(fake.submits) == 9
    assert fake.submits[0]["key"] == \
        f"wsop:{run1_id}:llm:{env['docs']['P1']['id']}:基本属性"
    assert fake.submits[0]["domain"] == env["ws"]["domain_id"]

    # ---- 4b. node 跑 fpower：复用 current 输入的 reused_snapshot 账
    _node_run_id, node_run = await _start_and_wait(env, scope="node",
                                                   target="fpower")
    assert node_run["status"] == "completed"
    quality = (await _current(env))["fpower:main"]["quality_json"]
    assert quality["reused_snapshot"] is True
    assert quality["input_rows"] == 3 and quality["filtered"] == 1

    # ---- 5. 右键重复铁律（batch_no 3→4→5，见阶段断言内）
    await _assert_dup_partial(env, run1_id, join_hash_1)

    # ---- 6. 恢复干净库存 → 重跑至 completed
    svc, ctx, ws_id = env["svc"], env["ctx"], str(env["ws"]["id"])
    clean = await svc.upsert_source_excel(
        user=ctx, workspace_id=ws_id, node_id="inv",
        content=_inventory_bytes(_INV_CLEAN), file_name="库存表.xlsx")
    assert clean["batch_no"] == 5
    run3_id, run3 = await _start_and_wait(env)
    assert run3["status"] == "completed"
    outputs = await _current(env)
    assert outputs["join:main"]["row_count"] == 3
    assert outputs["join:main"]["stale_since_run"] is None
    assert outputs["fpower:main"]["row_count"] == 2
    assert outputs["fpower:main"]["run_id"] == run3_id

    # ---- 7/8. 导出 + 幂等 + 血缘
    art_id = await _assert_export_and_lineage(env, run3_id)

    # 工作台不写批次（67号 §4.3）
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM kb_artifact_batches"
            " WHERE artifact_id = %s", (art_id,))
        assert int((await cur.fetchone())["n"]) == 0
