# -*- coding: utf-8 -*-
"""WorkspaceRunDB 运行层测试 —— 真库（67号工作台 MVP Task 3）。

覆盖：try_start_guard 三段式（queued 落库随 INSERT 原子携带冻结快照 /
在飞互斥不建新行——并发语义用两次连续调用+mark_running 模拟 / 工作区
不存在·归档·形态校验）/ run 生命周期（queued→running→四终态；终态不可
再变，重复调用返回 False 不抛）/ update_progress 增量合并（done 追加去
重、failed_rows 按节点并集）/ write_outputs（首写即 current、同
(workspace,node,port) 二写翻转旧 current——部分唯一索引下的合法翻转、
行数护栏拒整批、run·workspace 归属校验、hashes 形参逐 port 落
content_hash——Task 4 dataset_hash 接线）/ set_stale 幂等（同 run 二次
0 行、IS DISTINCT FROM 允许改指更新 run）/ 读四件（get_run / list_runs
DESC+limit / get_current_outputs 解析 content_inline / get_run_outputs）/
绑定 UPSERT / 导出 request_id 幂等与结果回写守卫。

真库跑法与 Task 2 同款（随机域前缀隔离，无模块级 skip）。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceDB,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import (
    MAX_ROWS_PER_PORT,
    WorkspaceRunDB,
)
from knowledge_mining.mining.kb.workspace_dataset import dataset_hash

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ------------------------------------------------------------ 夹具与构造


async def _user(async_pool) -> str:
    kbdb = KbDB(async_pool)
    row = await kbdb.create_user(username=f"run-u-{uuid.uuid4().hex[:8]}")
    return str(row["id"])


async def _seed(async_pool) -> tuple[WorkspaceRunDB, WorkspaceDB, dict, str]:
    """随机域工作台 + 双仓储，返回 (runs_db, workspaces_db, ws, owner)。"""
    owner = await _user(async_pool)
    wdb = WorkspaceDB(async_pool)
    ws = await wdb.create_workspace(
        domain_id=f"dom-{uuid.uuid4().hex[:8]}", name="运行层工作台",
        description="网络集成方案制作", owner_user_id=owner, created_by=owner,
    )
    return WorkspaceRunDB(async_pool), wdb, ws, owner


def _snapshot() -> dict:
    """冻结快照：A(source)→B(filter)，含执行时点的 pending/stale 终态。"""
    return {"nodes": [
        {"id": "A", "op": "source", "name": "源", "config": {}, "inputs": [],
         "ports": [{"id": "main", "name": "主输出", "kind": "table"}],
         "x": 0, "y": 0, "pending": False, "stale": False},
        {"id": "B", "op": "filter", "name": "滤功率",
         "config": {"field": "功率", "cond": "gte", "value": "1000"},
         "inputs": ["A:main"],
         "ports": [{"id": "main", "name": "主输出", "kind": "table"}],
         "x": 200, "y": 0, "pending": True, "stale": False},
    ]}


def _dataset(n: int = 2) -> dict:
    return {"cols": ["型号", "功率"],
            "rows": [{"_id": f"r{i:05d}", "_source": f"表/P{i}",
                      "型号": f"P{i}", "功率": f"{(i + 1) * 100}W"}
                     for i in range(n)]}


async def _start(rdb: WorkspaceRunDB, ws_id: str, owner: str, *,
                 batch_no: int = 1, scope: str = "workspace",
                 target: str | None = None,
                 graph_hash: str = "gh-1") -> dict:
    """守卫快捷入口（成功路径断言见专门用例，这里只取 run 行）。"""
    out = await rdb.try_start_guard(
        None, ws_id, owner, graph_snapshot=_snapshot(),
        graph_hash=graph_hash, scope=scope, target_node_id=target,
        batch_no=batch_no)
    assert out["in_flight"] is None and out["run"] is not None
    return out["run"]


# ------------------------------------------------------------ try_start_guard


async def test_try_start_guard_creates_queued_run_with_snapshot(
        async_pool) -> None:
    rdb, wdb, ws, owner = await _seed(async_pool)
    snap = _snapshot()
    ws_rev = (await wdb.get_workspace(workspace_id=ws["id"]))["graph_revision"]
    out = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=snap, graph_hash="gh-abc",
        scope="workspace", target_node_id=None, batch_no=3)
    assert out["in_flight"] is None
    run = out["run"]
    assert run["id"].startswith("wsr_") and len(run["id"]) == len("wsr_") + 16
    assert run["status"] == "queued"
    assert run["scope"] == "workspace" and run["target_node_id"] is None
    assert run["graph_hash"] == "gh-abc"
    assert run["graph_snapshot_json"] == snap  # 随 INSERT 原子落库
    assert run["graph_revision"] == ws_rev     # 锁内读工作区行
    assert run["batch_no"] == 3
    assert run["progress_json"] == {} and run["error"] is None
    assert run["started_at"] is None and run["finished_at"] is None
    assert run["actor_kind"] == "user" and run["actor_id"] == ""
    assert run["created_by"] == owner and run["created_at"]

    got = await rdb.get_run(run_id=run["id"])
    assert got is not None
    assert got["status"] == "queued" and got["graph_snapshot_json"] == snap

    # actor 通道（MCP 钥匙触发同一路径）：终态放行后再建
    assert await rdb.mark_completed(run["id"]) is True
    mcp_out = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=snap, graph_hash="h",
        scope="workspace", target_node_id=None, batch_no=1,
        actor_kind="mcp_key", actor_id="mk_1")
    assert mcp_out["in_flight"] is None
    assert mcp_out["run"]["actor_kind"] == "mcp_key"
    assert mcp_out["run"]["actor_id"] == "mk_1"


async def test_try_start_guard_in_flight_mutual_exclusion(
        async_pool) -> None:
    rdb, wdb, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)

    # queued 即在飞：第二个 start 拿 in_flight、不建新行
    second = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=_snapshot(), graph_hash="gh-1",
        scope="workspace", target_node_id=None, batch_no=1)
    assert second["run"] is None
    assert second["in_flight"]["id"] == run1["id"]
    assert len(await rdb.list_runs(workspace_id=ws["id"])) == 1

    # 推进 running 仍在飞（两次连续调用模拟并发窗口的两种时点）
    assert await rdb.mark_running(run1["id"]) is True
    third = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=_snapshot(), graph_hash="gh-1",
        scope="workspace", target_node_id=None, batch_no=1)
    assert third["in_flight"]["id"] == run1["id"]

    # 终态放行：completed 后可建新 run
    assert await rdb.mark_completed(run1["id"]) is True
    run2 = await _start(rdb, ws["id"], owner)
    assert run2["id"] != run1["id"]
    assert len(await rdb.list_runs(workspace_id=ws["id"])) == 2


async def test_try_start_guard_not_found_and_validation(
        async_pool) -> None:
    rdb, wdb, ws, owner = await _seed(async_pool)
    good = dict(graph_snapshot=_snapshot(), graph_hash="gh-1",
                scope="workspace", target_node_id=None, batch_no=1)

    with pytest.raises(WorkspaceNotFoundError):
        await rdb.try_start_guard(None, "wsp_missing", owner, **good)

    # scope 非法（trial 试跑不建 run——词表外直接拒）
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(None, ws["id"], owner,
                                  **{**good, "scope": "trial"})
    # scope='node' 缺 target_node_id
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(
            None, ws["id"], owner,
            **{**good, "scope": "node", "target_node_id": None})
    # 空图快照（无节点可执行）
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(
            None, ws["id"], owner,
            **{**good, "graph_snapshot": {"nodes": []}})
    # 空 graph_hash / 非法 actor_kind
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(None, ws["id"], owner,
                                  **{**good, "graph_hash": ""})
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(None, ws["id"], owner,
                                  **{**good, "actor_kind": "robot"})

    # 归档工作区不可开跑
    await wdb.archive_workspace(workspace_id=ws["id"])
    with pytest.raises(WorkspaceValidationError):
        await rdb.try_start_guard(None, ws["id"], owner, **good)


# ------------------------------------------------------------ 生命周期


async def test_run_lifecycle_transitions(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)

    # 主线：queued→running→completed；重复 mark_running 不再推进、不改写
    # started_at（仅 queued→running），终态后全部迁移返回 False 不抛。
    run1 = await _start(rdb, ws["id"], owner)
    assert await rdb.mark_running(run1["id"]) is True
    mid = await rdb.get_run(run_id=run1["id"])
    assert mid["status"] == "running" and mid["started_at"]
    assert await rdb.mark_running(run1["id"]) is False  # 已 running
    assert (await rdb.get_run(run_id=run1["id"]))["started_at"] == \
        mid["started_at"]
    assert await rdb.mark_completed(run1["id"]) is True
    final = await rdb.get_run(run_id=run1["id"])
    assert final["status"] == "completed" and final["finished_at"]
    assert await rdb.mark_completed(run1["id"]) is False
    assert await rdb.mark_partial(run1["id"]) is False
    assert await rdb.mark_failed(run1["id"], "迟到失败") is False
    assert await rdb.mark_cancelled(run1["id"]) is False

    # partial：running→partial
    run2 = await _start(rdb, ws["id"], owner)
    await rdb.mark_running(run2["id"])
    assert await rdb.mark_partial(run2["id"]) is True
    assert (await rdb.get_run(run_id=run2["id"]))["status"] == "partial"

    # failed：queued 直达失败，error 落库
    run3 = await _start(rdb, ws["id"], owner)
    assert await rdb.mark_failed(run3["id"], "节点B算子异常") is True
    row3 = await rdb.get_run(run_id=run3["id"])
    assert row3["status"] == "failed"
    assert "节点B算子异常" in row3["error"] and row3["finished_at"]

    # cancelled：queued 直达取消；终态后 mark_running 也进不去
    run4 = await _start(rdb, ws["id"], owner)
    assert await rdb.mark_cancelled(run4["id"]) is True
    assert await rdb.mark_cancelled(run4["id"]) is False
    assert await rdb.mark_running(run4["id"]) is False
    assert (await rdb.get_run(run_id=run4["id"]))["status"] == "cancelled"

    # 不存在的 run：一律 False（生命周期面无 NotFound 语义——布尔口径）
    assert await rdb.mark_running("wsr_missing0000000") is False


async def test_update_progress_incremental_merge(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run = await _start(rdb, ws["id"], owner)
    await rdb.mark_running(run["id"])

    assert await rdb.update_progress(
        run["id"], done_node="A", current_node="B", failed_rows=[]) is True
    assert (await rdb.get_run(run_id=run["id"]))["progress_json"] == {
        "done": ["A"], "current_node": "B"}

    assert await rdb.update_progress(
        run["id"], done_node="B", current_node="C",
        failed_rows=["r00009"]) is True
    progress = (await rdb.get_run(run_id=run["id"]))["progress_json"]
    assert progress["done"] == ["A", "B"]
    assert progress["current_node"] == "C"
    assert progress["failed_rows"] == {"B": ["r00009"]}

    # done 追加去重 + failed_rows 同节点并集去重
    assert await rdb.update_progress(
        run["id"], done_node="B", current_node="C",
        failed_rows=["r00009", "r00008"]) is True
    progress2 = (await rdb.get_run(run_id=run["id"]))["progress_json"]
    assert progress2["done"] == ["A", "B"]
    assert set(progress2["failed_rows"]["B"]) == {"r00008", "r00009"}

    # 形态防御：failed_rows 必须挂 done_node
    with pytest.raises(WorkspaceValidationError):
        await rdb.update_progress(
            "wsr_missing0000000", done_node=None, current_node="A",
            failed_rows=["x"])
    assert await rdb.update_progress(
        "wsr_missing0000000", done_node="A", current_node=None,
        failed_rows=[]) is False


# ------------------------------------------------------------ write_outputs


async def test_write_outputs_current_pointer_flip(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)
    await rdb.mark_running(run1["id"])
    ds1 = _dataset(2)
    rows = await rdb.write_outputs(
        None, run_id=run1["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": ds1,
                          "quality": {"input_rows": 2, "filtered": 1}}})
    assert len(rows) == 1
    assert rows[0]["is_current"] is True and rows[0]["kind"] == "table"
    assert rows[0]["row_count"] == 2
    assert rows[0]["content_hash"] == ""  # Task 4 落地前缺省空串
    assert rows[0]["stale_since_run"] is None

    current = await rdb.get_current_outputs(workspace_id=ws["id"])
    assert list(current) == ["B:main"]
    cur = current["B:main"]
    assert cur["run_id"] == run1["id"]
    assert cur["content_inline"] == ds1  # jsonb 解析往返
    assert cur["quality_json"] == {"input_rows": 2, "filtered": 1}
    assert cur["is_current"] is True

    # 第二跑同 (workspace,node,port)：旧 current 翻转、新行接任——
    # uq_ws_output_current 部分唯一索引下的合法翻转（单事务原子完成）
    await rdb.mark_completed(run1["id"])
    run2 = await _start(rdb, ws["id"], owner)
    ds2 = _dataset(1)
    await rdb.write_outputs(
        None, run_id=run2["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": ds2, "quality": {}}})
    current2 = await rdb.get_current_outputs(workspace_id=ws["id"])
    assert current2["B:main"]["run_id"] == run2["id"]
    assert current2["B:main"]["content_inline"] == ds2

    # 旧 run 输出行保留为历史（不再 current）
    outs1 = await rdb.get_run_outputs(run_id=run1["id"])
    assert len(outs1) == 1 and outs1[0]["is_current"] is False
    assert (await rdb.get_run_outputs(run_id=run2["id"]))[0]["is_current"] \
        is True


async def test_write_outputs_multi_port(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run = await _start(rdb, ws["id"], owner)
    rows = await rdb.write_outputs(
        None, run_id=run["id"], workspace_id=ws["id"], node_id="C",
        outputs={"main": {"dataset": _dataset(3), "quality": {}},
                 "extra": {"dataset": _dataset(1), "quality": {"n": 1}}})
    assert {r["port_id"] for r in rows} == {"main", "extra"}
    assert all(r["is_current"] for r in rows)
    by_port = {r["port_id"]: r for r in rows}
    assert by_port["main"]["row_count"] == 3
    assert by_port["extra"]["row_count"] == 1
    current = await rdb.get_current_outputs(workspace_id=ws["id"])
    assert set(current) == {"C:main", "C:extra"}


async def test_write_outputs_persists_content_hashes(async_pool) -> None:
    """Task 4 接线（Task 3 遗留 LOW）：write_outputs 的 hashes 形参收
    workspace_dataset.dataset_hash，逐 port 落 content_hash 并随
    get_current_outputs 往返；第二跑（新 run 翻转旧 current）只给部分
    port 的哈希——缺项保持缺省空串口径。"""
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)
    ds_main, ds_extra = _dataset(2), _dataset(1)
    rows = await rdb.write_outputs(
        None, run_id=run1["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": ds_main, "quality": {}},
                 "extra": {"dataset": ds_extra, "quality": {}}},
        hashes={"main": dataset_hash(ds_main),
                "extra": dataset_hash(ds_extra)})
    by_port = {r["port_id"]: r for r in rows}
    assert by_port["main"]["content_hash"] == dataset_hash(ds_main)
    assert by_port["extra"]["content_hash"] == dataset_hash(ds_extra)
    current = await rdb.get_current_outputs(workspace_id=ws["id"])
    assert current["B:main"]["content_hash"] == dataset_hash(ds_main)
    assert current["B:extra"]["content_hash"] == dataset_hash(ds_extra)

    # 第二跑只给部分 port 的哈希：缺项空串、重写 port 换新哈希
    await rdb.mark_completed(run1["id"])
    run2 = await _start(rdb, ws["id"], owner)
    ds2 = _dataset(1)
    rows2 = await rdb.write_outputs(
        None, run_id=run2["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": ds2, "quality": {}},
                 "extra": {"dataset": _dataset(2), "quality": {}}},
        hashes={"main": dataset_hash(ds2)})
    by_port2 = {r["port_id"]: r for r in rows2}
    assert by_port2["main"]["content_hash"] == dataset_hash(ds2)
    assert by_port2["extra"]["content_hash"] == ""


async def test_write_outputs_row_guard_rejects_entire_batch(
        async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run = await _start(rdb, ws["id"], owner)

    with pytest.raises(WorkspaceValidationError):
        await rdb.write_outputs(
            None, run_id=run["id"], workspace_id=ws["id"], node_id="B",
            outputs={"main": {"dataset": _dataset(2), "quality": {}},
                     "extra": {"dataset": _dataset(MAX_ROWS_PER_PORT + 1),
                               "quality": {}}})
    # 整批拒绝：合法的 main 也不落
    assert await rdb.get_run_outputs(run_id=run["id"]) == []
    assert await rdb.get_current_outputs(workspace_id=ws["id"]) == {}

    # 边界恰好=上限放行
    rows = await rdb.write_outputs(
        None, run_id=run["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": _dataset(MAX_ROWS_PER_PORT),
                          "quality": {}}})
    assert rows[0]["row_count"] == MAX_ROWS_PER_PORT


async def test_write_outputs_validation_and_ownership(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run = await _start(rdb, ws["id"], owner)
    args = dict(run_id=run["id"], workspace_id=ws["id"], node_id="B")

    with pytest.raises(WorkspaceValidationError):  # 空 outputs
        await rdb.write_outputs(None, outputs={}, **args)
    with pytest.raises(WorkspaceValidationError):  # payload 非法形态
        await rdb.write_outputs(
            None, outputs={"main": "不是对象"}, **args)
    with pytest.raises(WorkspaceValidationError):  # dataset 缺失
        await rdb.write_outputs(
            None, outputs={"main": {"quality": {}}}, **args)
    with pytest.raises(WorkspaceValidationError):  # rows 非列表
        await rdb.write_outputs(
            None,
            outputs={"main": {"dataset": {"rows": "x"}, "quality": {}}},
            **args)

    # run 不存在 / 不属于该工作区 → NotFound（干净错误优于裸 FK 违例）
    with pytest.raises(WorkspaceNotFoundError):
        await rdb.write_outputs(
            None, outputs={"main": {"dataset": _dataset(1), "quality": {}}},
            run_id="wsr_missing0000000", workspace_id=ws["id"], node_id="B")
    other_owner = await _user(async_pool)
    other_ws = await WorkspaceDB(async_pool).create_workspace(
        domain_id=f"dom-{uuid.uuid4().hex[:8]}", name="他区",
        owner_user_id=other_owner, created_by=other_owner)
    with pytest.raises(WorkspaceNotFoundError):
        await rdb.write_outputs(
            None, outputs={"main": {"dataset": _dataset(1), "quality": {}}},
            run_id=run["id"], workspace_id=other_ws["id"], node_id="B")


# ------------------------------------------------------------ set_stale


async def test_set_stale_idempotent_and_refocus(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)
    await rdb.write_outputs(
        None, run_id=run1["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": _dataset(2), "quality": {}}})

    assert await rdb.set_stale(
        None, ws["id"], [("B", "main")], since_run_id=run1["id"]) == 1
    cur = (await rdb.get_current_outputs(workspace_id=ws["id"]))["B:main"]
    assert cur["stale_since_run"] == run1["id"]

    # 幂等：同 run 二次 → 0 行（IS DISTINCT FROM 拦下）
    assert await rdb.set_stale(
        None, ws["id"], [("B", "main")], since_run_id=run1["id"]) == 0

    # 改指更新的 run 才更新（上游重跑导致新过时源）
    await rdb.mark_completed(run1["id"])
    run2 = await _start(rdb, ws["id"], owner)
    assert await rdb.set_stale(
        None, ws["id"], [("B", "main")], since_run_id=run2["id"]) == 1
    cur2 = (await rdb.get_current_outputs(workspace_id=ws["id"]))["B:main"]
    assert cur2["stale_since_run"] == run2["id"]

    # 不匹配的 (node,port) / 空清单不动
    assert await rdb.set_stale(
        None, ws["id"], [("X", "nope")], since_run_id=run2["id"]) == 0
    assert await rdb.set_stale(None, ws["id"], [], since_run_id=run2["id"]) \
        == 0


# ------------------------------------------------------------ 读


async def test_list_runs_and_get_run(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)
    await asyncio.sleep(0.003)  # created_at TEXT 微秒序兜底
    await rdb.mark_completed(run1["id"])
    run2 = await _start(rdb, ws["id"], owner)
    await asyncio.sleep(0.003)
    await rdb.mark_cancelled(run2["id"])
    run3 = await _start(rdb, ws["id"], owner)

    rows = await rdb.list_runs(workspace_id=ws["id"])
    assert [r["id"] for r in rows] == [run3["id"], run2["id"], run1["id"]]
    assert [r["status"] for r in rows] == ["queued", "cancelled", "completed"]
    limited = await rdb.list_runs(workspace_id=ws["id"], limit=2)
    assert [r["id"] for r in limited] == [run3["id"], run2["id"]]

    assert await rdb.list_runs(workspace_id="wsp_missing") == []
    assert await rdb.get_run(run_id="wsr_missing0000000") is None
    assert await rdb.get_run_outputs(run_id="wsr_missing0000000") == []
    assert await rdb.get_current_outputs(workspace_id="wsp_missing") == {}


# ------------------------------------------------------------ 绑定与导出


async def test_upsert_binding(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)

    with pytest.raises(WorkspaceValidationError):
        await rdb.upsert_binding(
            None, ws["id"], "B", "main", artifact_id="",
            artifact_kind="table", last_run_id=run1["id"], updated_by=owner)

    row = await rdb.upsert_binding(
        None, ws["id"], "B", "main", artifact_id="art_1",
        artifact_kind="table", last_run_id=run1["id"], updated_by=owner)
    assert row["artifact_id"] == "art_1" and row["artifact_kind"] == "table"
    assert row["last_run_id"] == run1["id"] and row["last_exported_at"]
    assert row["created_by"] == owner

    # 同 PK 再绑：更新去处与最近 run，保留首插 created_by
    await rdb.mark_completed(run1["id"])
    run2 = await _start(rdb, ws["id"], owner)
    row2 = await rdb.upsert_binding(
        None, ws["id"], "B", "main", artifact_id="art_2",
        artifact_kind="table", last_run_id=run2["id"], updated_by=owner)
    assert row2["artifact_id"] == "art_2"
    assert row2["last_run_id"] == run2["id"]
    assert row2["last_exported_at"] >= row["last_exported_at"]
    assert row2["created_by"] == owner


async def test_insert_export_idempotent_and_result(async_pool) -> None:
    rdb, _, ws, owner = await _seed(async_pool)
    run1 = await _start(rdb, ws["id"], owner)
    mapping = {"columns": [{"id": "型号", "name": "型号"}]}
    # request_id 随机前缀隔离（真库跨 run 共享，无清表——固定值会撞历史行）
    req_a = f"req-{uuid.uuid4().hex[:12]}"
    req_b = f"req-{uuid.uuid4().hex[:12]}"

    with pytest.raises(WorkspaceValidationError):  # 空 request_id 拒
        await rdb.insert_export(
            None, request_id="", workspace_id=ws["id"], node_id="B",
            port_id="main", snapshot_run_id=run1["id"], mapping_json=mapping,
            created_by=owner)

    row, created = await rdb.insert_export(
        None, request_id=req_a, workspace_id=ws["id"], node_id="B",
        port_id="main", snapshot_run_id=run1["id"], mapping_json=mapping,
        created_by=owner)
    assert created is True
    assert row["id"].startswith("wse_") and len(row["id"]) == len("wse_") + 16
    assert row["status"] == "pending" and row["mode"] == "create_draft"
    assert row["mapping_json"] == mapping
    assert row["snapshot_run_id"] == run1["id"]
    assert row["actor_kind"] == "user" and row["created_by"] == owner

    # request_id 重放：payload 再不同也命中既有行原样返回（幂等入口）
    row2, created2 = await rdb.insert_export(
        None, request_id=req_a, workspace_id=ws["id"], node_id="C",
        port_id="main", target_artifact_id="art_9", snapshot_run_id=run1["id"],
        mapping_json={}, created_by=owner)
    assert created2 is False and row2["id"] == row["id"]
    assert row2["node_id"] == "B" and row2["mapping_json"] == mapping

    # 结果回写：pending → 终态一次性；重放 False；非法 status 拒
    assert await rdb.mark_export_result(
        row["id"], "succeeded", result_json={"artifact_id": "art_x"}) is True
    assert await rdb.mark_export_result(
        row["id"], "succeeded", result_json={"artifact_id": "art_y"}) is False
    with pytest.raises(WorkspaceValidationError):
        await rdb.mark_export_result(row["id"], "pending")
    with pytest.raises(WorkspaceValidationError):
        await rdb.mark_export_result(row["id"], "failed")

    row_f, _ = await rdb.insert_export(
        None, request_id=req_b, workspace_id=ws["id"], node_id="B",
        port_id="main", snapshot_run_id=run1["id"], mapping_json={},
        created_by=owner)
    assert await rdb.mark_export_result(
        row_f["id"], "failed", error="KB 可见性校验失败") is True

    # 落库核验（本层未设导出读方法——raw SQL 断言，避免超出 Task 3 面）
    async with async_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT status, result_json, error FROM kb_workspace_exports
               WHERE id = ANY(%s) ORDER BY id""",
            ([row["id"], row_f["id"]],))
        rows = [dict(r) for r in await cur.fetchall()]
    by_id = {r["status"]: r for r in rows}
    assert by_id["succeeded"]["result_json"] == {"artifact_id": "art_x"}
    assert "KB 可见性校验失败" in by_id["failed"]["error"]


async def test_export_read_reset_and_binding_helpers(async_pool) -> None:
    """Task 9 导出配套面：get_export_by_request / reset_failed_export（仅
    failed 行可回 pending，回退即清 error/result/target）/ get_binding /
    upsert_binding 显式 exported_at / mark_export_result 带
    target_artifact_id（provided 才覆写）。"""
    rdb, _, ws, owner = await _seed(async_pool)
    req = f"req-{uuid.uuid4().hex[:12]}"

    assert await rdb.get_export_by_request(request_id=req) is None
    row, created = await rdb.insert_export(
        None, request_id=req, workspace_id=ws["id"], node_id="B",
        port_id="main", mapping_json={}, created_by=owner)
    assert created is True
    got = await rdb.get_export_by_request(request_id=req)
    assert got["id"] == row["id"] and got["status"] == "pending"

    # reset 仅对「本工作区的 failed 行」生效：pending/不存在/异工作区 →
    # False（审查 Medium-1：归属条件防跨工作区重置）
    assert await rdb.reset_failed_export(
        export_id=row["id"], workspace_id=ws["id"]) is False
    assert await rdb.reset_failed_export(
        export_id="wse_missing", workspace_id=ws["id"]) is False
    assert await rdb.mark_export_result(
        row["id"], "failed", error="boom") is True
    assert await rdb.reset_failed_export(
        export_id=row["id"], workspace_id="wsp_other") is False
    got = await rdb.get_export_by_request(request_id=req)
    assert got["status"] == "failed"  # 异工作区未翻动
    assert await rdb.reset_failed_export(
        export_id=row["id"], workspace_id=ws["id"]) is True
    got = await rdb.get_export_by_request(request_id=req)
    assert got["status"] == "pending"
    assert got["error"] is None and got["result_json"] is None
    assert got["target_artifact_id"] is None

    assert await rdb.mark_export_result(
        row["id"], "succeeded", result_json={"exports": []},
        target_artifact_id="art_z") is True
    got = await rdb.get_export_by_request(request_id=req)
    assert got["target_artifact_id"] == "art_z"
    assert got["result_json"] == {"exports": []}

    # binding 读 + 显式 exported_at（导出事务内 now() 同钟落账）
    assert await rdb.get_binding(
        workspace_id=ws["id"], node_id="B", port_id="main") is None
    binding = await rdb.upsert_binding(
        None, ws["id"], "B", "main", artifact_id="art_1",
        artifact_kind="table", updated_by=owner,
        exported_at="2026-11-16T01:02:03+00:00")
    assert binding["last_exported_at"] == "2026-11-16T01:02:03+00:00"
    got_b = await rdb.get_binding(
        workspace_id=ws["id"], node_id="B", port_id="main")
    assert got_b["artifact_id"] == "art_1"
    assert got_b["last_exported_at"] == "2026-11-16T01:02:03+00:00"
