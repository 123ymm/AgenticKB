# -*- coding: utf-8 -*-
"""工作台执行引擎（67号工作台 MVP Task 6）——真库 TDD。

图与来源直接用 WorkspaceDB/upsert_source 造数（excel 连接器的冻结
manifest 内嵌 dataset——materialize_source 同构取数，无须真实 xlsx 字节）。
真库跑法与 Task 2/3 同款（随机域前缀隔离；容器命令见计划文档 Task 6）。

覆盖：
- 双分支 partial 语义（核心用例）：join 分支成功 + select 分支成功 +
  filter 故意配错（字段不存在）失败 + 其下游级联标「上游失败」→
  run=partial；node_errors 落 progress_json；失败节点无 current 输出；
  graph_hash=图 canonical sha256（与 dataset 契约同款）。
- stale 传播 + 节点标志清除：全区跑完 → 改 select 配置（diff 打
  pending/stale）→ node scope 重跑 → 下游 current 行 stale_since_run=新
  run、非下游不动；执行节点 pending 定点清（graph_revision 不推）、
  未执行后代 stale 留；复用 current 输入的 quality 记 reused_snapshot。
- trial：不建 run 不写 outputs（runs/outputs 双断言）；input_rows=切片
  实数；无 current 输入 → Validation「输入未执行」。
- in_flight：running 残留行时 start_run → WorkspaceConflictError。
- 崩溃恢复：running + 旧 started_at → reconcile_stale_running 判
  failed(interrupted)；start_run 入口自动清残（残 run 不挡新 run）。
- source 缺行：节点失败（未接入来源）不炸 run，下游级联，全败→failed。
- llm_extract 接线（Task 7）：单链 source(excel)→llm_extract 注入 fake
  llm_factory，全区跑 → completed+输出列/quality/幂等键/域断言；试跑同 fake。
- cancel：受控桩按住首算子节点 → cancel → 后台在节点边界自停（下一节点
  不执行）+ 幂等（二次 False）。
- start 校验：NotFound / 归档 / 空图 / subgraph 未接线 / node 缺目标。
- get_status：run 行（剥大快照）+ 节点态（graph 标志 + 输出 stale）合成；
  run 不属于该工作区 → NotFound。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.workspace_dataset import dataset_hash
from knowledge_mining.mining.kb.workspace_executor import (
    WorkspaceExecutor,
    reconcile_stale_running,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ------------------------------------------------------------ 夹具与材料

def _hex(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


async def _seed(async_pool) -> tuple[WorkspaceDB, dict, str]:
    """随机域工作台，返回 (wdb, ws, owner)。"""
    owner = str((await KbDB(async_pool).create_user(
        username=f"wex-u-{_hex(10)}"))["id"])
    wdb = WorkspaceDB(async_pool)
    ws = await wdb.create_workspace(
        domain_id=f"dom-{_hex()}", name="执行引擎工作台", description="步骤流",
        owner_user_id=owner, created_by=owner,
    )
    return wdb, ws, owner


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


async def _put_source(wdb: WorkspaceDB, ws_id: str, node_id: str,
                      dataset: dict) -> None:
    await wdb.upsert_source(
        workspace_id=ws_id, node_id=node_id, connector="excel", config={},
        input_manifest={"dataset": dataset}, updated_by="t")


def _products() -> dict:
    return {"kind": "table", "cols": ["型号", "功率_W"],
            "rows": [{"_id": "p1", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P1", "功率_W": 3000},
                     {"_id": "p2", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P2", "功率_W": 2000},
                     {"_id": "p3", "_source": "产品表.xlsx/Sheet1",
                      "型号": "P3", "功率_W": 1200}]}


def _inventory() -> dict:
    return {"kind": "table", "cols": ["型号", "可用库存"],
            "rows": [{"_id": "i1", "_source": "库存表.xlsx/Sheet1",
                      "型号": "P1", "可用库存": 5},
                     {"_id": "i2", "_source": "库存表.xlsx/Sheet1",
                      "型号": "P3", "可用库存": 0}]}


def _two_branch_nodes() -> list[dict]:
    """核心用例图：sp→j(←si) 关联分支 与 sp→f(坏配置)→fd、sp→s 独立分支。"""
    return [
        _node("sp", "source"),
        _node("si", "source"),
        _node("j", "join", inputs=("sp:main", "si:main"),
              config={"method": "left", "left_key": "型号",
                      "right_key": "型号"}),
        _node("f", "filter", inputs=("sp:main",),
              config={"field": "不存在的列", "cond": "eq", "value": "x"}),
        _node("fd", "select", inputs=("f:main",),
              config={"columns": [{"id": "型号", "name": "型号"}]}),
        _node("s", "select", inputs=("sp:main",),
              config={"columns": [{"id": "型号", "name": "型号"},
                                  {"id": "功率_W", "name": "功率_W"}]}),
    ]


def _chain_nodes(s_columns: list | None = None) -> list[dict]:
    """好链图 sp→s→sd；s_columns 覆盖 s 配置（改配置触发 diff pending）。"""
    return [
        _node("sp", "source"),
        _node("s", "select", inputs=("sp:main",),
              config={"columns": s_columns or [
                  {"id": "型号", "name": "型号"},
                  {"id": "功率_W", "name": "功率_W"}]}),
        _node("sd", "select", inputs=("s:main",),
              config={"columns": [{"id": "型号", "name": "型号"}]}),
    ]


async def _seed_chain(async_pool) -> tuple[WorkspaceExecutor, WorkspaceDB,
                                           dict, str]:
    """好链图 + sp 来源 + 执行器（未起 run——各用例自行 start）。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], _chain_nodes())
    await _put_source(wdb, ws["id"], "sp", _products())
    return WorkspaceExecutor(async_pool), wdb, ws, owner


async def _wait_terminal(rdb: WorkspaceRunDB, run_id: str,
                         timeout: float = 15.0) -> dict:
    """轮询 run 到终态（后台 _execute 与测试共用 pool，逐次让出）。"""
    async def _poll() -> dict:
        while True:
            run = await rdb.get_run(run_id)
            assert run is not None, f"run disappeared: {run_id}"
            if run["status"] in ("completed", "partial", "failed",
                                 "cancelled"):
                return run
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(_poll(), timeout)


def _snapshot_1node() -> dict:
    """守卫最小合法快照（直接造 running 残留行用）。"""
    return {"nodes": [_node("sp", "source")]}


# ------------------------------------------------------------ 核心用例


async def test_two_branch_partial_semantics(async_pool) -> None:
    """filter 配错失败：join/select 分支独立完成 → partial + node_errors。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], _two_branch_nodes())
    await _put_source(wdb, ws["id"], "sp", _products())
    await _put_source(wdb, ws["id"], "si", _inventory())
    graph = (await wdb.get_workspace(workspace_id=ws["id"]))["graph_json"]

    ex = WorkspaceExecutor(async_pool)
    run_id = await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner)
    rdb = WorkspaceRunDB(async_pool)
    run = await _wait_terminal(rdb, run_id)

    assert run["status"] == "partial"
    assert run["error"] is None  # partial 摘要走 progress，不占 error
    assert run["graph_hash"] == dataset_hash(graph)  # canonical sha256
    progress = run["progress_json"]
    assert set(progress["done"]) == {"sp", "si", "j", "f", "fd", "s"}
    errors = progress["node_errors"]
    assert set(errors) == {"f", "fd"}
    assert "field not in input columns" in errors["f"]
    assert errors["fd"].startswith("上游失败")

    outputs = await rdb.get_current_outputs(ws["id"])
    assert set(outputs) == {"sp:main", "si:main", "j:main", "s:main"}
    assert outputs["sp:main"]["row_count"] == 3
    assert outputs["j:main"]["row_count"] == 3  # left 关联 3 行（P2 库存空）
    assert outputs["j:main"]["quality_json"] == {
        "matched": 2, "unmatched_left": 1}
    assert outputs["s:main"]["row_count"] == 3
    assert outputs["sp:main"]["content_hash"] == dataset_hash(_products())


# ------------------------------------------------------------ node scope


async def test_node_scope_rerun_stale_and_flag_clear(async_pool) -> None:
    """重跑单节点：下游 current 行 stale_since_run=新 run、非下游不动；
    执行节点 pending 定点清（不推 revision）、未执行后代 stale 留。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    run1 = await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))
    assert run1["status"] == "completed"

    # 改 s 配置 → 服务端 diff：s pending、sd stale（严格后代）
    rev1 = (await wdb.get_workspace(workspace_id=ws["id"]))["graph_revision"]
    put2 = await _put_graph(
        wdb, ws["id"],
        _chain_nodes(s_columns=[{"id": "型号", "name": "型号"}]),
        expected_revision=rev1)
    rev2 = put2["graph_revision"]
    flags0 = {n["id"]: (n.get("pending"), n.get("stale"))
              for n in put2["graph"]["nodes"]}
    assert flags0["s"] == (True, False)   # 预置条件自检
    assert flags0["sd"] == (False, True)

    run2_id = await ex.start_run(ws["id"], scope="node", target_node_id="s",
                                 created_by=owner)
    run2 = await _wait_terminal(rdb, run2_id)
    assert run2["status"] == "completed"
    assert run2["scope"] == "node" and run2["target_node_id"] == "s"
    assert run2["progress_json"]["done"] == ["s"]

    outputs = await rdb.get_current_outputs(ws["id"])
    assert outputs["s:main"]["run_id"] == run2_id          # 刚写的新 current
    assert outputs["s:main"]["stale_since_run"] is None
    assert outputs["sd:main"]["run_id"] == run1["id"]      # 未重跑的下游
    assert outputs["sd:main"]["stale_since_run"] == run2_id
    assert outputs["sp:main"]["stale_since_run"] is None   # 非下游不动

    rows2 = await rdb.get_run_outputs(run2_id)
    s_row = next(r for r in rows2 if r["node_id"] == "s")
    assert s_row["quality_json"].get("reused_snapshot") is True

    after = await wdb.get_workspace(workspace_id=ws["id"])
    flags = {n["id"]: (n.get("pending"), n.get("stale"))
             for n in after["graph_json"]["nodes"]}
    assert flags["s"] == (False, False)    # 执行成功定点清除
    assert flags["sd"] == (False, True)    # 未执行后代保留 stale
    assert flags["sp"] == (False, False)
    assert after["graph_revision"] == rev2  # 定点清不推 revision


async def test_source_node_rerun_same_weight_stale_and_flag_clear(
        async_pool) -> None:
    """决策 4 钉测：重跑 source 节点与算子同权——下游 current 行
    stale_since_run=新 run + source 自身 pending 定点清；未执行后代
    stale 标志保留。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    run1 = await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))
    assert run1["status"] == "completed"

    # 改 source 节点 config → diff：sp pending、s/sd stale（sp 的严格后代）
    rev1 = (await wdb.get_workspace(workspace_id=ws["id"]))["graph_revision"]
    put2 = await _put_graph(
        wdb, ws["id"],
        [_node("sp", "source", config={"refresh": "manual"}),
         *_chain_nodes()[1:]],
        expected_revision=rev1)
    flags0 = {n["id"]: (n.get("pending"), n.get("stale"))
              for n in put2["graph"]["nodes"]}
    assert flags0["sp"] == (True, False)   # 预置条件自检
    assert flags0["s"] == (False, True)
    assert flags0["sd"] == (False, True)

    run2_id = await ex.start_run(ws["id"], scope="node", target_node_id="sp",
                                 created_by=owner)
    run2 = await _wait_terminal(rdb, run2_id)
    assert run2["status"] == "completed"
    assert run2["progress_json"]["done"] == ["sp"]

    outputs = await rdb.get_current_outputs(ws["id"])
    assert outputs["sp:main"]["run_id"] == run2_id       # 重新取冻结数
    assert outputs["sp:main"]["stale_since_run"] is None
    assert outputs["s:main"]["run_id"] == run1["id"]     # 下游 stale 由
    assert outputs["s:main"]["stale_since_run"] == run2_id  # source 重跑打上
    assert outputs["sd:main"]["stale_since_run"] == run2_id

    after = await wdb.get_workspace(workspace_id=ws["id"])
    flags = {n["id"]: (n.get("pending"), n.get("stale"))
             for n in after["graph_json"]["nodes"]}
    assert flags["sp"] == (False, False)  # source 自身 pending 定点清
    assert flags["s"] == (False, True)    # 未执行后代保留 stale
    assert flags["sd"] == (False, True)


async def test_after_node_success_revision_mismatch_skips_flag_clear(
        async_pool) -> None:
    """决策 5 钉测（M1）：started_revision 与当前 graph_revision 不匹配
    （错值 / -1 逃生值）→ jsonb_set UPDATE 落空、节点标志原样；stale 传播
    不受 revision 守卫（账记输出行，不碰 graph）；revision 匹配时同方法可
    正常清除（对照侧）。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    run1 = await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))
    assert run1["status"] == "completed"
    rev1 = (await wdb.get_workspace(workspace_id=ws["id"]))["graph_revision"]
    put2 = await _put_graph(
        wdb, ws["id"],
        _chain_nodes(s_columns=[{"id": "型号", "name": "型号"}]),
        expected_revision=rev1)
    rev2 = put2["graph_revision"]
    snapshot_nodes = put2["graph"]["nodes"]

    def _flags(row: dict) -> dict:
        return {n["id"]: (n.get("pending"), n.get("stale"))
                for n in row["graph_json"]["nodes"]}

    # 错值 revision：UPDATE 落空，标志不动
    await ex._after_node_success(run1["id"], ws["id"], "s",
                                 snapshot_nodes, started_revision=99,
                                 run_new={})
    after = await wdb.get_workspace(workspace_id=ws["id"])
    assert _flags(after)["s"] == (True, False)
    assert _flags(after)["sd"] == (False, True)
    assert after["graph_revision"] == rev2
    # stale 传播不被 revision 守卫：s 的后代 sd 的 current 行已记账
    outputs = await rdb.get_current_outputs(ws["id"])
    assert outputs["sd:main"]["stale_since_run"] == run1["id"]
    assert outputs["s:main"]["stale_since_run"] is None  # 自身不打

    # -1 逃生值（读取与落锁间被并发 PUT 推版本）：同样全程跳过
    await ex._after_node_success(run1["id"], ws["id"], "s",
                                 snapshot_nodes, started_revision=-1,
                                 run_new={})
    after = await wdb.get_workspace(workspace_id=ws["id"])
    assert _flags(after)["s"] == (True, False)

    # 对照侧：revision 匹配 → 同方法正常定点清（正向路径另有 node_scope
    # 用例覆盖，此处证明方法本身可清、错值确因守卫落空）
    await ex._after_node_success(run1["id"], ws["id"], "s",
                                 snapshot_nodes, started_revision=rev2,
                                 run_new={})
    after = await wdb.get_workspace(workspace_id=ws["id"])
    assert _flags(after)["s"] == (False, False)
    assert _flags(after)["sd"] == (False, True)  # 未执行节点不受牵连
    assert after["graph_revision"] == rev2


# ------------------------------------------------------------ trial


async def test_trial_no_db_writes(async_pool) -> None:
    """试跑不落库：runs 无新行、current outputs 无变化；切片前 3 行。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))
    runs_before = await rdb.list_runs(ws["id"])
    outputs_before = await rdb.get_current_outputs(ws["id"])

    result = await ex.trial_node(ws["id"], "sd")
    assert result["trial"] is True
    assert result["input_rows"] == 3
    assert result["quality"] == {}
    main = result["outputs"]["main"]
    assert main["cols"] == ["型号"]
    assert len(main["rows"]) == 3

    assert await rdb.list_runs(ws["id"]) == runs_before
    outputs_after = await rdb.get_current_outputs(ws["id"])
    assert {k: v["created_at"] for k, v in outputs_after.items()} == \
        {k: v["created_at"] for k, v in outputs_before.items()}


async def test_trial_input_rows_short_input(async_pool) -> None:
    """L2：不足 3 行的输入 → input_rows=切片后实际行数（2 行输入→2）。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], _chain_nodes())
    short = _products()
    short["rows"] = short["rows"][:2]
    await _put_source(wdb, ws["id"], "sp", short)
    ex = WorkspaceExecutor(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))

    result = await ex.trial_node(ws["id"], "s")
    assert result["trial"] is True
    assert result["input_rows"] == 2
    assert len(result["outputs"]["main"]["rows"]) == 2


async def test_trial_requires_current_input(async_pool) -> None:
    """从未执行 → 无 current 输入 → Validation「输入未执行」。"""
    wdb, ws, owner = await _seed(async_pool)
    ex = WorkspaceExecutor(async_pool)
    await _put_graph(wdb, ws["id"], _chain_nodes())
    await _put_source(wdb, ws["id"], "sp", _products())
    with pytest.raises(WorkspaceValidationError) as ei:
        await ex.trial_node(ws["id"], "s")
    assert "输入未执行" in str(ei.value)


# ------------------------------------------------------------ 启动门禁


async def test_start_run_conflicts_with_in_flight(async_pool) -> None:
    """在飞 run（running 残留行）→ start → WorkspaceConflictError。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], _chain_nodes())
    await _put_source(wdb, ws["id"], "sp", _products())
    rdb = WorkspaceRunDB(async_pool)
    seeded = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=_snapshot_1node(),
        graph_hash="gh-x", scope="workspace", target_node_id=None, batch_no=1)
    assert seeded["run"] is not None
    assert await rdb.mark_running(seeded["run"]["id"]) is True

    ex = WorkspaceExecutor(async_pool)
    with pytest.raises(WorkspaceConflictError) as ei:
        await ex.start_run(ws["id"], scope="workspace", created_by=owner)
    assert seeded["run"]["id"] in str(ei.value)


async def test_start_run_validation_gates(async_pool) -> None:
    """NotFound / 空图 / subgraph 未接线 / node 缺目标 / 归档。"""
    wdb, ws, owner = await _seed(async_pool)
    ex = WorkspaceExecutor(async_pool)
    with pytest.raises(WorkspaceNotFoundError):
        await ex.start_run("wsp_missing", scope="workspace",
                           created_by=owner)
    with pytest.raises(WorkspaceValidationError, match="nodes"):
        await ex.start_run(ws["id"], scope="workspace", created_by=owner)

    await _put_graph(wdb, ws["id"], _chain_nodes())
    with pytest.raises(WorkspaceValidationError, match="未接线"):
        await ex.start_run(ws["id"], scope="subgraph", created_by=owner)
    with pytest.raises(WorkspaceValidationError):
        await ex.start_run(ws["id"], scope="node", created_by=owner)
    with pytest.raises(WorkspaceValidationError):
        await ex.start_run(ws["id"], scope="node", target_node_id="nope",
                           created_by=owner)

    await wdb.archive_workspace(workspace_id=ws["id"])
    with pytest.raises(WorkspaceValidationError, match="archived"):
        await ex.start_run(ws["id"], scope="workspace", created_by=owner)


# ------------------------------------------------------------ 崩溃恢复


async def test_reconcile_stale_running(async_pool) -> None:
    """running + started_at 超 60 分钟（阈值随 INFO-1 提到 60：llm 双轮等待
    理论上限 2×900s=30min 不得被误判）→ failed(interrupted)；start_run
    入口自动清残——残 run 不再挡同工作区的新 run。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    stale = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=_snapshot_1node(),
        graph_hash="gh", scope="workspace", target_node_id=None, batch_no=1)
    stale_run = stale["run"]
    assert stale_run is not None
    assert await rdb.mark_running(stale_run["id"]) is True
    async with async_pool.connection() as conn:
        await conn.execute(
            "UPDATE kb_workspace_runs SET started_at = %s WHERE id = %s",
            ((datetime.now(timezone.utc) - timedelta(minutes=70)).isoformat(),
             stale_run["id"]),
        )

    assert await reconcile_stale_running(async_pool) >= 1
    row = await rdb.get_run(stale_run["id"])
    assert row["status"] == "failed"
    assert "interrupted" in row["error"]

    # 入口清残：同工作区新 run 不被残 run 的 in_flight 挡住
    run_id = await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner)
    assert (await _wait_terminal(rdb, run_id))["status"] == "completed"


# ------------------------------------------------------------ 失败口径


async def test_source_row_missing_node_fails_run_survives(async_pool) -> None:
    """图有 source 节点但 sources 表无该 node_id → 节点失败不炸 run；
    下游级联「上游失败」；全败 → failed（error 摘要带首错节点）。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], [
        _node("sp", "source"),
        _node("s", "select", inputs=("sp:main",),
              config={"columns": [{"id": "型号", "name": "型号"}]}),
    ])  # 故意不 upsert_source
    ex = WorkspaceExecutor(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    run = await _wait_terminal(
        rdb, await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner))
    assert run["status"] == "failed"
    errors = run["progress_json"]["node_errors"]
    assert "未接入来源" in errors["sp"]
    assert errors["s"].startswith("上游失败")
    assert "[sp]" in run["error"]
    assert await rdb.get_current_outputs(ws["id"]) == {}


async def test_llm_extract_node_full_run(async_pool) -> None:
    """llm_extract 真库走通（Task 7 接线）：source(excel)→llm_extract 单链，
    注入 fake llm_factory，全区跑 → completed；输出=输入列+新列（int cast）、
    quality 全账、幂等键/域传参；试跑通道同 fake（input_rows=切片实数）。"""
    from knowledge_mining.tests.kb.test_workspace_llm import FakeLlmTaskClient

    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], [
        _node("sp", "source"),
        _node("lx", "llm_extract", inputs=("sp:main",),
              config={"input_column": "型号",
                      "prompt_template": "从下文抽取：{型号}",
                      "output_fields": [{"name": "标签", "type": "enum"},
                                        {"name": "分数", "type": "int"}]}),
    ])
    await _put_source(wdb, ws["id"], "sp", _products())
    fake = FakeLlmTaskClient(default_outcome={
        "status": "succeeded",
        "result": {"text_output": '{"标签": "高端", "分数": 88}',
                   "parse_status": "ok"},
    })
    ex = WorkspaceExecutor(async_pool, llm_factory=lambda: fake)
    rdb = WorkspaceRunDB(async_pool)
    run_id = await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner)
    run = await _wait_terminal(rdb, run_id)
    assert run["status"] == "completed"
    assert "node_errors" not in (run["progress_json"] or {})

    outputs = await rdb.get_current_outputs(ws["id"])
    assert set(outputs) == {"sp:main", "lx:main"}
    lx = outputs["lx:main"]
    assert lx["row_count"] == 3
    assert lx["content_inline"]["cols"] == ["型号", "功率_W", "标签", "分数"]
    rows = {r["_id"]: r for r in lx["content_inline"]["rows"]}
    assert rows["p1"]["标签"] == "高端" and rows["p1"]["分数"] == 88
    assert rows["p1"]["型号"] == "P1"
    assert rows["p3"]["分数"] == 88
    assert lx["quality_json"] == {
        "total": 3, "succeeded": 3, "failed_row_ids": [], "retried": 0,
        "partial": False}
    # 传参钉：幂等键 wsop:{run}:{node}:{row}、域=工作区 domain_id
    assert fake.submits[0]["idempotency_key"] == f"wsop:{run_id}:lx:p1"
    assert fake.submits[0]["knowledge_domain"] == ws["domain_id"]

    # 试跑通道（同 fake）：前 3 行、单轮、幂等键 wstrial 形态
    result = await ex.trial_node(ws["id"], "lx")
    assert result["trial"] is True
    assert result["input_rows"] == 3
    assert result["outputs"]["main"]["rows"][0]["分数"] == 88
    assert fake.submits[-1]["idempotency_key"].startswith("wstrial:lx:")


# ------------------------------------------------------------ cancel


async def test_cancel_stops_at_node_boundary(
        async_pool, monkeypatch) -> None:
    """受控桩按住首算子节点：cancel → 后台在节点边界自停（下一节点不执
    行、已写输出保留）；cancel 幂等（二次 False）。"""
    wdb, ws, owner = await _seed(async_pool)
    await _put_graph(wdb, ws["id"], [
        _node("sp", "source"),
        _node("s1", "select", inputs=("sp:main",),
              config={"columns": [{"id": "型号", "name": "型号"}]}),
        _node("s2", "select", inputs=("s1:main",),
              config={"columns": [{"id": "型号", "name": "型号"}]}),
    ])
    await _put_source(wdb, ws["id"], "sp", _products())
    ex = WorkspaceExecutor(async_pool)
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _hold(run_id, workspace_id, node, run_new, current, *, domain):
        calls.append(node["id"])
        if node["id"] == "s1":
            entered.set()
            await release.wait()
        return {}

    monkeypatch.setattr(ex, "_exec_operator", _hold)
    rdb = WorkspaceRunDB(async_pool)
    run_id = await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner)
    await asyncio.wait_for(entered.wait(), 10.0)
    assert await ex.cancel_run(run_id) is True
    assert await ex.cancel_run(run_id) is False  # 幂等
    release.set()
    run = await _wait_terminal(rdb, run_id)
    assert run["status"] == "cancelled"
    assert calls == ["s1"]                       # s2 从未进入算子执行
    assert "s2" not in run["progress_json"]["done"]
    # 已写输出保留（sp 的 current 行仍在）
    outputs = await rdb.get_current_outputs(ws["id"])
    assert "sp:main" in outputs


# ------------------------------------------------------------ get_status


async def test_get_status_synthesis(async_pool) -> None:
    """run 行（剥大快照）+ 节点态（graph 标志 + 输出 stale）合成；run 不
    属于该工作区 → NotFound。"""
    ex, wdb, ws, owner = await _seed_chain(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    run_id = await ex.start_run(ws["id"], scope="workspace",
                                created_by=owner)
    await _wait_terminal(rdb, run_id)

    status = await ex.get_status(ws["id"], run_id)
    assert status["run"]["status"] == "completed"
    assert "graph_snapshot_json" not in status["run"]
    assert status["run"]["progress_json"]["done"] == ["sp", "s", "sd"]
    by_id = {n["id"]: n for n in status["nodes"]}
    assert set(by_id) == {"sp", "s", "sd"}
    assert by_id["sp"]["pending"] is False and by_id["sp"]["stale"] is False
    assert by_id["s"]["outputs"]["main"]["row_count"] == 3
    assert by_id["s"]["outputs"]["main"]["stale"] is False
    assert by_id["sd"]["outputs"]["main"]["content_hash"]

    _, ws2, _ = await _seed(async_pool)
    with pytest.raises(WorkspaceNotFoundError):
        await ex.get_status(ws2["id"], run_id)
