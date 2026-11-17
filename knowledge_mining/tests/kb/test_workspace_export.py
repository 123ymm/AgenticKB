# -*- coding: utf-8 -*-
"""工作台导出交付（67号 MVP Task 9）——真库 TDD（导出要写真制品表，fake 不够）。

覆盖：
- 正常导出（executor 全链：excel source→select 跑一遍）：制品 status=draft /
  current_revision=0 / visibility=private / domain=工作区域；owner 成员行 +
  操作者 editor 行（owner 指定他人时）；列类型样本推断（功率_W→int、型号→
  enum）；rev0 行 row_key=hex(_id)、provenance 数组含 workspace 血缘、data
  只含 cols；binding 落库；exports 行 succeeded；不写 kb_artifact_batches。
- request_id 幂等：二次同 request_id → 同 artifact_id + duplicate 标记 +
  制品行数不变（不重复写）。
- 门禁族：stale 输出 / row_count=0 / 输出缺失 / owner 非同域有效用户 →
  逐项失败不阻断其余；全部失败 → export_all_failed（exports 行 failed）。
- 人工改动门禁：导出后 PATCH 制品 title（推 updated_at）→ 再导出 →
  artifact_modified_since_export；未改动时重复导出=B.5 重写草稿（同
  artifact_id、行数不变、provenance 不拼接）。
- kb_dir 血缘撤权：私有 KB 非成员 → source_permission_revoked；补
  kb_members 行后放行（read_kb_dir 同款谓词 KbDB.is_visible）。
- 多 item 部分成功：一个合法 + 一个 stale → 200 语义逐项状态，exports 行
  succeeded。
- 权限（2026-11-17 收紧后口径）：非管理员（陌生人/绑定 viewer）→ 404 反
  探测；pending 行 → 409 export_in_progress；failed 行复用重跑（同 id 状态
  回 pending 再 succeeded）。

真库跑法与 Task 2 同款（随机域前缀隔离，无模块级 skip；容器命令见计划
文档 Task 0）。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB, _encode_key_parts
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.workspace_executor import WorkspaceExecutor
from knowledge_mining.mining.kb.workspace_service import WorkspaceService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ------------------------------------------------------------ 夹具与构造


def _hex(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


async def _user(async_pool) -> dict:
    return await KbDB(async_pool).create_user(
        username=f"wexp-{_hex(10)}")


def _ctx(u: dict) -> dict:
    """auth.current_user 返回的 kb_users 行形态（id/site_role）。"""
    return {"id": str(u["id"]), "username": u["username"],
            "site_role": u.get("site_role", "member"), "status": "active"}


async def _seed(async_pool) -> tuple:
    """随机域工作台 + 服务/仓储四件，返回 (svc, wdb, rdb, adb, ws, owner_ctx)。

    2026-11-17 收紧：owner 从绑定 member 提升为该域 domain admin
    （bind_domain domain_role='admin'）——服务层 _assert_ws_role 只认
    管理员，普通绑定成员全 404。"""
    owner = await _user(async_pool)
    domain = f"dom-{_hex()}"
    await KbDB(async_pool).bind_domain(
        user_id=str(owner["id"]), domain=domain, domain_role="admin")
    wdb = WorkspaceDB(async_pool)
    ws = await wdb.create_workspace(
        domain_id=domain, name="导出工作台", description="导出交付",
        owner_user_id=str(owner["id"]), created_by=str(owner["id"]),
    )
    svc = WorkspaceService(async_pool)
    return (svc, wdb, WorkspaceRunDB(async_pool), ArtifactDB(async_pool),
            ws, _ctx(owner))


def _port() -> list[dict]:
    return [{"id": "main", "name": "主输出", "kind": "table"}]


def _node(nid: str, op: str, *, inputs: tuple[str, ...] = (),
          config: dict | None = None) -> dict:
    return {"id": nid, "op": op, "name": nid, "config": config or {},
            "inputs": list(inputs), "ports": _port(), "x": 0, "y": 0}


async def _put_graph(wdb: WorkspaceDB, ws_id: str, nodes: list[dict],
                     expected_revision: int = 0) -> dict:
    return await wdb.put_graph(
        workspace_id=ws_id, graph={"nodes": nodes},
        expected_revision=expected_revision)


def _products() -> dict:
    return {"kind": "table", "cols": ["型号", "功率_W"],
            "rows": [{"_id": "p1", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P1", "功率_W": 3000},
                     {"_id": "p2", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P2", "功率_W": 2000},
                     {"_id": "p3", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P3", "功率_W": 1200}]}


async def _write_output(rdb: WorkspaceRunDB, wdb: WorkspaceDB, ws: dict,
                        owner_id: str, nodes: list[dict], node_id: str,
                        dataset: dict, *, batch_no: int = 1) -> dict:
    """直写 current 输出（绕过执行器；与 executor write_outputs 同一路径）。

    返回 run 行。空数据集（row_count=0）传 rows=[]。"""
    out = await rdb.try_start_guard(
        None, str(ws["id"]), owner_id, graph_snapshot={"nodes": nodes},
        graph_hash=f"gh-{batch_no}", scope="node", target_node_id=node_id,
        batch_no=batch_no)
    run = out["run"]
    await rdb.mark_running(run["id"])
    await rdb.write_outputs(
        None, run_id=run["id"], workspace_id=str(ws["id"]), node_id=node_id,
        outputs={"main": {"dataset": dataset, "quality": {}}})
    await rdb.mark_completed(run["id"])
    # 图不落库时 put_graph 一次保证 graph_json 与血缘上溯一致
    current = await wdb.get_workspace(workspace_id=str(ws["id"]))
    if not (current["graph_json"] or {}).get("nodes"):
        await _put_graph(wdb, str(ws["id"]), nodes)
    return run


async def _wait_terminal(rdb: WorkspaceRunDB, run_id: str,
                         timeout: float = 15.0) -> dict:
    async def _poll() -> dict:
        while True:
            run = await rdb.get_run(run_id)
            assert run is not None, f"run disappeared: {run_id}"
            if run["status"] in ("completed", "partial", "failed",
                                 "cancelled"):
                return run
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout)


def _item(node_id: str, *, port_id: str = "main", name: str = "导出制品",
          owner_user_id: str | None = None) -> dict:
    out = {"node_id": node_id, "port_id": port_id, "artifact_name": name}
    if owner_user_id is not None:
        out["owner_user_id"] = owner_user_id
    return out


async def _export(svc: WorkspaceService, ws: dict, ctx: dict,
                  request_id: str, items: list[dict]) -> dict:
    return await svc.export_drafts(
        str(ws["id"]), ctx, request_id=request_id, items=items)


# ------------------------------------------------------------ 正常导出


async def test_export_creates_private_draft_with_lineage(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    ws_id, owner_id = str(ws["id"]), str(ctx["id"])
    nodes = [
        _node("sp", "source"),
        _node("s", "select", inputs=("sp:main",),
              config={"columns": [{"id": "型号", "name": "型号"},
                                  {"id": "功率_W", "name": "功率_W"}]}),
    ]
    await _put_graph(wdb, ws_id, nodes)
    await wdb.upsert_source(
        workspace_id=ws_id, node_id="sp", connector="excel", config={},
        input_manifest={"dataset": _products()}, updated_by=owner_id)

    run_id = await WorkspaceExecutor(async_pool).start_run(
        ws_id, scope="workspace", created_by=owner_id)
    run = await _wait_terminal(rdb, run_id)
    assert run["status"] == "completed", run
    out = (await rdb.get_current_outputs(ws_id))["s:main"]
    assert out["row_count"] == 3

    # owner 指定他人：同域有效用户
    other = await _user(async_pool)
    await KbDB(async_pool).bind_domain(
        user_id=str(other["id"]), domain=str(ws["domain_id"]))

    result = await _export(svc, ws, ctx, f"req-{_hex(12)}", [
        _item("s", name="产品功率表", owner_user_id=str(other["id"]))])
    [item] = result["exports"]
    assert item["status"] == "succeeded" and "error" not in item
    assert item["node_id"] == "s" and item["port_id"] == "main"
    assert item["name"] == "产品功率表"
    art_id = item["artifact_id"]
    assert art_id.startswith("art_")

    art = await adb.get_artifact(art_id)
    assert art["status"] == "draft" and art["current_revision"] == 0
    assert art["visibility"] == "private" and art["kind"] == "table"
    assert art["domain_id"] == ws["domain_id"]
    assert art["owner_user_id"] == str(other["id"])
    assert art["title"] == "产品功率表"

    # 成员行：owner（指定他人）+ 操作者 editor
    assert await adb.artifact_role(art_id, str(other["id"])) == "owner"
    assert await adb.artifact_role(art_id, owner_id) == "editor"

    # 列类型样本推断：型号 3 值→enum；功率_W 全 int→int
    cols = {c["column_name"]: c for c in await adb.list_columns(art_id)}
    assert cols["型号"]["column_type"] == "enum"
    assert cols["型号"]["enum_values"] == ["P1", "P2", "P3"]
    assert cols["功率_W"]["column_type"] == "int"

    # rev0 行：row_key=hex(_id) 同构；provenance 数组含 workspace 血缘；
    # data 只含 cols（_id/_source 不混入）
    listing = await adb.list_rows(art_id, 0, size=50)
    assert listing["total"] == 3
    ds_rows = out["content_inline"]["rows"]
    by_key = {r["row_key"]: r for r in listing["rows"]}
    for src in ds_rows:
        key = _encode_key_parts([str(src["_id"])])
        row = by_key[key]
        assert row["data"] == {"型号": src["型号"], "功率_W": src["功率_W"]}
        assert row["provenance"] == [{
            "_source": src["_source"],
            "workspace": {"id": ws_id, "node": "s", "port": "main",
                          "run_id": out["run_id"]}}]
    assert [r["ordinal"] for r in
            sorted(listing["rows"], key=lambda r: r["ordinal"])] == [1, 2, 3]

    # binding + exports 行 + 不写 batches（67号 §4.3）
    binding = await rdb.get_binding(
        workspace_id=ws_id, node_id="s", port_id="main")
    assert binding["artifact_id"] == art_id
    assert binding["last_run_id"] == out["run_id"]
    assert binding["last_exported_at"]
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT status, result_json FROM kb_workspace_exports
               WHERE workspace_id = %s ORDER BY created_at DESC""",
            (ws_id,))
        [export_row] = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM kb_artifact_batches"
            " WHERE artifact_id = %s", (art_id,))
        assert int((await cur.fetchone())["n"]) == 0
    assert export_row["status"] == "succeeded"
    assert export_row["result_json"]["exports"][0]["artifact_id"] == art_id


# ------------------------------------------------------------ 幂等


async def test_export_idempotent_same_request_id(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    nodes = [_node("sp", "source"),
             _node("s", "select", inputs=("sp:main",))]
    await _write_output(rdb, wdb, ws, str(ctx["id"]), nodes, "s",
                        _products())
    request_id = f"req-{_hex(12)}"

    first = await _export(svc, ws, ctx, request_id, [_item("s")])
    art_id = first["exports"][0]["artifact_id"]
    assert await adb.count_rows(art_id, 0) == 3

    second = await _export(svc, ws, ctx, request_id, [_item("s")])
    assert second.get("duplicate") is True
    assert second["exports"] == first["exports"]
    assert await adb.count_rows(art_id, 0) == 3  # 不重复写

    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM kb_workspace_exports"
            " WHERE request_id = %s", (request_id,))
        assert int((await cur.fetchone())["n"]) == 1


# ------------------------------------------------------------ 门禁族


async def test_export_gate_failures_all_failed(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    owner_id = str(ctx["id"])
    nodes = [_node("a", "source"), _node("b", "source"),
             _node("d", "source"), _node("f", "select", inputs=("a:main",))]
    run1 = await _write_output(rdb, wdb, ws, owner_id, nodes, "a",
                               _products(), batch_no=1)
    await _write_output(rdb, wdb, ws, owner_id, nodes, "b",
                        {"kind": "table", "cols": ["型号"], "rows": []},
                        batch_no=2)
    await _write_output(rdb, wdb, ws, owner_id, nodes, "d",
                        _products(), batch_no=3)
    # a 的 current 输出打 stale（上游重跑账）
    await rdb.set_stale(None, str(ws["id"]), [("a", "main")],
                        since_run_id=run1["id"])

    request_id = f"req-{_hex(12)}"
    with pytest.raises(WorkspaceValidationError) as ei:
        await _export(svc, ws, ctx, request_id, [
            _item("a"),                                   # stale_output
            _item("b"),                                   # empty_output
            _item("missing", name="x"),                   # output_not_found
            _item("d", owner_user_id=f"u-ghost-{_hex()}"),  # invalid_owner
        ])
    assert str(ei.value.code) == "export_all_failed"
    for code in ("stale_output", "empty_output", "output_not_found",
                 "invalid_owner_user"):
        assert code in str(ei.value)

    # 全部失败：exports 行 failed 留档（重试复用同行的前置态）
    row = await rdb.get_export_by_request(request_id=request_id)
    assert row["status"] == "failed"
    assert "stale_output" in str(row["error"])
    # 无制品创建
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM kb_workspace_output_bindings"
            " WHERE workspace_id = %s", (str(ws["id"]),))
        assert int((await cur.fetchone())["n"]) == 0


async def test_export_partial_success(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    owner_id = str(ctx["id"])
    nodes = [_node("g", "source"), _node("a", "source"),
             _node("f", "select", inputs=("a:main",))]
    run1 = await _write_output(rdb, wdb, ws, owner_id, nodes, "g",
                               _products(), batch_no=1)
    await _write_output(rdb, wdb, ws, owner_id, nodes, "a",
                        _products(), batch_no=2)
    await rdb.set_stale(None, str(ws["id"]), [("a", "main")],
                        since_run_id=run1["id"])

    result = await _export(svc, ws, ctx, f"req-{_hex(12)}", [
        _item("g", name="好制品"), _item("a", name="坏制品")])
    statuses = {i["node_id"]: i for i in result["exports"]}
    assert statuses["g"]["status"] == "succeeded"
    assert statuses["g"]["artifact_id"].startswith("art_")
    assert statuses["a"]["status"] == "failed"
    assert statuses["a"]["error"] == "stale_output"
    assert statuses["a"]["artifact_id"] is None
    assert "duplicate" not in result


# ------------------------------------------------------------ 人工改动与重写


async def test_export_rewrite_and_human_modification_gate(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    nodes = [_node("sp", "source"), _node("s", "select", inputs=("sp:main",))]
    await _write_output(rdb, wdb, ws, str(ctx["id"]), nodes, "s",
                        _products())

    first = await _export(svc, ws, ctx, f"req-{_hex(12)}", [
        _item("s", name="草稿一")])
    art_id = first["exports"][0]["artifact_id"]

    # B.5：重复导出同 port = 重写既有草稿（不解除绑定、不新建制品）
    second = await _export(svc, ws, ctx, f"req-{_hex(12)}", [
        _item("s", name="草稿一")])
    assert second["exports"][0]["artifact_id"] == art_id
    assert await adb.count_rows(art_id, 0) == 3
    # 重写=删除重插：provenance 不随 UPSERT 拼接（仍单元素）
    rows = (await adb.list_rows(art_id, 0, size=10))["rows"]
    assert all(len(r["provenance"]) == 1 for r in rows)

    # 人工改动（PATCH title 推 updated_at）→ 阻断覆盖；单 item 全败 → 整体
    # export_all_failed（明细 code 进异常消息），绑定与行数不动（人工改动未丢）
    await adb.update_artifact_meta(art_id, title="人工改名")
    with pytest.raises(WorkspaceValidationError) as ei:
        await _export(svc, ws, ctx, f"req-{_hex(12)}", [
            _item("s", name="草稿一")])
    assert str(ei.value.code) == "export_all_failed"
    assert "artifact_modified_since_export" in str(ei.value)
    assert await adb.count_rows(art_id, 0) == 3
    assert (await rdb.get_binding(
        workspace_id=str(ws["id"]), node_id="s",
        port_id="main"))["artifact_id"] == art_id
    art = await adb.get_artifact(art_id)
    assert art["title"] == "人工改名"


# ------------------------------------------------------------ kb_dir 撤权


async def test_export_kb_dir_lineage_permission_revoked(async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    operator_id = str(ctx["id"])

    # 私有 KB（他人在别的域持有）；工作台 kb_dir 来源直指它
    kbdb = KbDB(async_pool)
    kb_owner = await _user(async_pool)
    kb_domain = f"kbdom-{_hex()}"
    await kbdb.bind_domain(user_id=str(kb_owner["id"]), domain=kb_domain)
    kb = await kbdb.create_kb(
        domain=kb_domain, name=f"撤权库-{_hex(6)}",
        owner_id=str(kb_owner["id"]), visibility="private")

    nodes = [_node("ksrc", "source"),
             _node("s", "select", inputs=("ksrc:main",))]
    await wdb.upsert_source(
        workspace_id=str(ws["id"]), node_id="ksrc", connector="kb_dir",
        config={"kb_id": str(kb["id"]), "directories": []},
        input_manifest={"dataset": _products()}, updated_by=operator_id)
    await _write_output(rdb, wdb, ws, operator_id, nodes, "s", _products())

    # 操作者对 KB 无读权（同款谓词 KbDB.is_visible）→ item 失败；单 item
    # 全败 = 整体 export_all_failed，失败明细进异常与 exports 行 error
    request_id = f"req-{_hex(12)}"
    with pytest.raises(WorkspaceValidationError) as ei:
        await _export(svc, ws, ctx, request_id, [_item("s")])
    assert str(ei.value.code) == "export_all_failed"
    assert "source_permission_revoked" in str(ei.value)
    row = await rdb.get_export_by_request(request_id=request_id)
    assert row["status"] == "failed"

    # 补权：先绑 KB 域（is_visible 谓词域门槛）+ kb_members 行 → 放行；
    # 新 request_id（旧行已 failed——同 request_id 走复用重跑路径，此处
    # 不混测）
    await kbdb.bind_domain(user_id=operator_id, domain=kb_domain)
    async with async_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO kb_members (kb_id, user_id, role, added_at)
               VALUES (%s, %s, 'viewer', %s)""",
            (str(kb["id"]), operator_id, "2026-11-16T00:00:00+00:00"),
        )
    ok = await _export(svc, ws, ctx, f"req-{_hex(12)}", [_item("s")])
    assert ok["exports"][0]["status"] == "succeeded"


# ------------------------------------------------------------ 幂等冲突与重试


async def test_export_pending_conflict_and_failed_retry_reuses_row(
        async_pool) -> None:
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    nodes = [_node("sp", "source"), _node("s", "select", inputs=("sp:main",))]
    await _write_output(rdb, wdb, ws, str(ctx["id"]), nodes, "s",
                        _products())
    request_id = f"req-{_hex(12)}"

    # 直接造 pending 行（在飞）→ 409 export_in_progress
    row, created = await rdb.insert_export(
        None, request_id=request_id, workspace_id=str(ws["id"]), node_id="s",
        port_id="main", mapping_json={}, created_by=str(ctx["id"]))
    assert created is True
    with pytest.raises(WorkspaceConflictError) as ei:
        await _export(svc, ws, ctx, request_id, [_item("s")])
    assert str(ei.value.code) == "export_in_progress"

    # failed 行：复用该行重跑（状态回 pending 再 succeeded，不建新行）
    assert await rdb.mark_export_result(
        row["id"], "failed", error="人工造失败") is True
    result = await _export(svc, ws, ctx, request_id, [_item("s")])
    assert result["exports"][0]["status"] == "succeeded"
    after = await rdb.get_export_by_request(request_id=request_id)
    assert after["id"] == row["id"] and after["status"] == "succeeded"
    assert after["target_artifact_id"] == result["exports"][0]["artifact_id"]
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM kb_workspace_exports"
            " WHERE request_id = %s", (request_id,))
        assert int((await cur.fetchone())["n"]) == 1


# ------------------------------------------------------------ 权限反探测


async def test_export_permission_gates(async_pool) -> None:
    """收紧（2026-11-17）：非管理员一律 404 反探测——陌生人、绑定 viewer
    （挂成员行）同面同码。"""
    svc, wdb, rdb, adb, ws, ctx = await _seed(async_pool)
    nodes = [_node("sp", "source"), _node("s", "select", inputs=("sp:main",))]
    await _write_output(rdb, wdb, ws, str(ctx["id"]), nodes, "s",
                        _products())

    stranger = await _user(async_pool)
    with pytest.raises(WorkspaceNotFoundError):  # 陌生人=404 反探测同面
        await _export(svc, ws, _ctx(stranger), f"req-{_hex(12)}",
                      [_item("s")])

    viewer = await _user(async_pool)
    await KbDB(async_pool).bind_domain(
        user_id=str(viewer["id"]), domain=str(ws["domain_id"]))
    await wdb.add_member(workspace_id=str(ws["id"]),
                         user_id=str(viewer["id"]), role="viewer")
    with pytest.raises(WorkspaceNotFoundError) as ei:  # 绑定 viewer → 404
        await _export(svc, ws, _ctx(viewer), f"req-{_hex(12)}",
                      [_item("s")])
    assert str(ei.value.code) == "workspace_not_found"


# ------------------------------------------------------------ 跨工作区回放


async def test_export_cross_workspace_replay_404(async_pool) -> None:
    """审查 Medium-1：request_id 全局唯一但归属工作区——异工作区重放 404
    反探测（不回放他区 result_json、不把他区 failed 行翻 pending 借行执行）；
    原工作区的结果与重试路径不受影响。"""
    svc1, wdb1, rdb1, adb1, ws1, ctx1 = await _seed(async_pool)
    svc2, wdb2, rdb2, adb2, ws2, ctx2 = await _seed(async_pool)
    nodes = [_node("sp", "source"), _node("s", "select", inputs=("sp:main",))]
    await _write_output(rdb1, wdb1, ws1, str(ctx1["id"]), nodes, "s",
                        _products())

    request_id = f"req-{_hex(12)}"
    first = await _export(svc1, ws1, ctx1, request_id, [_item("s")])
    art_id = first["exports"][0]["artifact_id"]

    # 异工作区重放同 request_id → 404 反探测
    with pytest.raises(WorkspaceNotFoundError):
        await _export(svc2, ws2, ctx2, request_id, [_item("s")])

    # 原结果不受影响：原工作区再重放仍 duplicate、同制品
    replay = await _export(svc1, ws1, ctx1, request_id, [_item("s")])
    assert replay["duplicate"] is True
    assert replay["exports"][0]["artifact_id"] == art_id

    # failed 行同样不可被异工作区 reset：ws1 造 failed（坏 owner 全败）→
    # ws2 同 request_id → 404；ws1 重试走复用重跑路径成功（行未被异区翻
    # pending——翻了 ws1 此处会撞 409 export_in_progress）
    request_id2 = f"req-{_hex(12)}"
    with pytest.raises(WorkspaceValidationError):
        await _export(svc1, ws1, ctx1, request_id2, [
            _item("s", owner_user_id=f"u-ghost-{_hex()}")])
    row2 = await rdb1.get_export_by_request(request_id=request_id2)
    assert row2["workspace_id"] == str(ws1["id"])
    assert row2["status"] == "failed"
    with pytest.raises(WorkspaceNotFoundError):
        await _export(svc2, ws2, ctx2, request_id2, [_item("s")])
    ok = await _export(svc1, ws1, ctx1, request_id2, [_item("s")])
    assert ok["exports"][0]["status"] == "succeeded"
    # 异工作区两次调用均未产生任何行/制品
    assert await rdb2.get_binding(
        workspace_id=str(ws2["id"]), node_id="s", port_id="main") is None


async def _mk_ws_owner(async_pool, *, site_role: str, bind: bool):
    """造无绑定/有绑定 owner 的工作台 + 一个 select 节点 current 输出。"""
    u = await _user(async_pool)
    if site_role == "admin":
        async with async_pool.connection() as c:
            async with c.transaction():
                await c.execute(
                    "UPDATE kb_users SET site_role='admin' WHERE id = %s",
                    (str(u["id"]),))
        u["site_role"] = "admin"
    if bind:
        # 2026-11-17 收紧：operator 须域管理员（否则 404 到不了 owner 校验）
        await KbDB(async_pool).bind_domain(
            user_id=str(u["id"]), domain=_BND[0], domain_role="admin")
        domain = _BND[0]
    else:
        domain = f"dom-{_hex()}"
    wdb = WorkspaceDB(async_pool)
    ws = await wdb.create_workspace(
        domain_id=domain, name=f"ws-{site_role}-{bind}", description="",
        owner_user_id=str(u["id"]), created_by=str(u["id"]))
    nodes = [_node("src", "source"),
             _node("s", "select", inputs=("src:main",),
                   config={"columns": [{"id": "型号", "name": "型号"}]})]
    dataset = {"kind": "table", "name": "t", "cols": ["型号"],
               "rows": [{"_id": "r1", "_source": "x", "型号": "P1"}],
               "grain": "一行一产品"}
    await _write_output(WorkspaceRunDB(async_pool), wdb, ws, str(u["id"]),
                        nodes, "s", dataset)
    return u, ws


async def test_export_site_admin_owner_without_binding(async_pool):
    """内网阻断修复：owner 为 active 站点管理员但无 user_domains 绑定行——
    建工作台可走默认域回落、导出此前被严格 user_in_domain 拦死
    （invalid_owner_user 内网必现）。修复口径=is_domain_admin 旁路
    （site admin 全通），普通成员仍要求绑定行。"""
    svc = WorkspaceService(async_pool)
    admin, ws = await _mk_ws_owner(async_pool, site_role="admin", bind=False)
    res = await svc.export_drafts(
        str(ws["id"]), _ctx(admin), request_id=f"req-{_hex(12)}",
        items=[{"node_id": "s", "port_id": "main",
                "artifact_name": "管理员无绑定导出"}])
    assert res["exports"][0]["status"] == "succeeded"
    # 负侧一：无绑定普通用户连工作台都进不去（_assert_ws_role 域绑定
    # 预检先行 404 反探测——比 owner 校验更早，属既定口径）
    member, ws2 = await _mk_ws_owner(async_pool, site_role="member",
                                     bind=False)
    with pytest.raises(WorkspaceNotFoundError):
        await svc.export_drafts(
            str(ws2["id"]), _ctx(member), request_id=f"req-{_hex(12)}",
            items=[{"node_id": "s", "port_id": "main",
                    "artifact_name": "普通成员无绑定导出"}])
    # 负侧二：有绑定 operator 显式指定无绑定用户为 owner —— 严格分支
    # （active+user_in_domain）仍生效
    bound, _b = await _mk_ws_owner(async_pool, site_role="member", bind=True)
    # 单 item 全失败 → 聚合 422 export_all_failed（携带逐项错误码）
    with pytest.raises(WorkspaceError) as ei:
        await svc.export_drafts(
            str(_b["id"]), _ctx(bound), request_id=f"req-{_hex(12)}",
            items=[{"node_id": "s", "port_id": "main",
                    "artifact_name": "指定无绑定owner",
                    "owner_user_id": str(member["id"])}])
    assert "invalid_owner_user" in str(ei.value)


_BND = [f"bnd-{_hex()}"]
