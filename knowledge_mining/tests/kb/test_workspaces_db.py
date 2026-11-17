# -*- coding: utf-8 -*-
"""WorkspaceDB 登记层测试 —— 真库（67号工作台 MVP Task 2）。

覆盖：工作区 CRUD 往返（owner 成员行同事务）/ list 的 admin·成员两口径与
归档分列（status 字段，created_at DESC）/ patch 乐观锁 / archive 幂等 /
put_graph（正常写入 revision+1、乐观锁冲突回滚、Kahn 环拒绝、引用与形态
校验、服务端 diff 标志 pending·stale 及继承语义）/ 成员生命周期与最后一个
owner 保护 / 来源 UPSERT 同事务推进 batch_no（WB-07）。

真库跑法与 Task 1 同款（无模块级 skip；容器命令见
docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 2）。测试隔离靠
随机域前缀 + 随机用户名（无 KB_ALLOW_TEST_TRUNCATE 清表时跨 run 不互踩）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ------------------------------------------------------------ 夹具与图构造


async def _user(async_pool) -> str:
    """造一个 kb_users 行，返回 id。"""
    kbdb = KbDB(async_pool)
    row = await kbdb.create_user(username=f"ws-u-{uuid.uuid4().hex[:8]}")
    return str(row["id"])


async def _seed(async_pool) -> tuple[WorkspaceDB, dict, str]:
    """建随机域上的工作台（owner 随机用户），返回 (db, ws, owner_id)。"""
    owner = await _user(async_pool)
    db = WorkspaceDB(async_pool)
    ws = await db.create_workspace(
        domain_id=f"dom-{uuid.uuid4().hex[:8]}", name="组网工作台",
        description="网络集成方案制作", owner_user_id=owner, created_by=owner,
    )
    return db, ws, owner


def _node(nid: str, op: str = "filter", *, config: dict | None = None,
          inputs: tuple[str, ...] = (), name: str | None = None) -> dict:
    return {
        "id": nid, "op": op, "name": name or nid,
        "config": {} if config is None else config,
        "inputs": list(inputs),
        "ports": [{"id": "main", "name": "主输出", "kind": "table"}],
        "x": 0, "y": 0,
    }


def _graph(*nodes: dict) -> dict:
    return {"nodes": list(nodes)}


def _abc_graph() -> dict:
    """A(source)→B(filter)→C(select) 三点链。"""
    return _graph(
        _node("A", "source", name="源"),
        _node("B", "filter",
              config={"field": "状态", "cond": "eq", "value": "在网"},
              inputs=["A:main"]),
        _node("C", "select", config={"columns": [{"id": "名称", "name": "名称"}]},
              inputs=["B:main"]),
    )


# ------------------------------------------------------------ 登记 CRUD

async def test_create_and_get_roundtrip(async_pool) -> None:
    db, ws, owner = await _seed(async_pool)
    assert ws["id"].startswith("wsp_") and len(ws["id"]) == len("wsp_") + 16
    assert ws["status"] == "active"
    assert ws["graph_revision"] == 0
    assert ws["batch_no"] == 1
    assert ws["graph_json"] == {"nodes": []}
    assert ws["owner_user_id"] == owner
    assert ws["actor_kind"] == "user" and ws["actor_id"] == ""
    assert ws["created_at"] and ws["updated_at"]

    got = await db.get_workspace(workspace_id=ws["id"])
    assert got is not None and got["id"] == ws["id"]
    assert got["graph_json"] == {"nodes": []}

    # owner 成员行与登记 INSERT 同事务落库
    assert await db.member_role(
        workspace_id=ws["id"], user_id=owner) == "owner"
    assert await db.get_workspace(workspace_id="wsp_missing") is None


async def test_list_workspaces_admin_member_and_archived(async_pool) -> None:
    db = WorkspaceDB(async_pool)
    domain = f"dom-{uuid.uuid4().hex[:8]}"
    u1 = await _user(async_pool)
    u2 = await _user(async_pool)
    u3 = await _user(async_pool)
    ws1 = await db.create_workspace(
        domain_id=domain, name="一厂", owner_user_id=u1, created_by=u1)
    await asyncio.sleep(0.003)  # created_at 微秒序兜底
    ws2 = await db.create_workspace(
        domain_id=domain, name="二厂", owner_user_id=u2, created_by=u2)
    await db.add_member(workspace_id=ws2["id"], user_id=u1, role="viewer")
    # 他域工作台：同 owner，域过滤下不可见
    await db.create_workspace(
        domain_id=f"dom-{uuid.uuid4().hex[:8]}", name="他域",
        owner_user_id=u1, created_by=u1)

    # admin 两口径=本域全部（无关用户 u3 以 admin 身份仍见全集）
    for admin_kwargs in ({"is_site_admin": True}, {"is_domain_admin": True}):
        rows = await db.list_workspaces(
            domain_id=domain, user_id=u3, **admin_kwargs)
        assert [r["id"] for r in rows] == [ws2["id"], ws1["id"]]  # DESC
        assert all(r["status"] == "active" for r in rows)

    # 余人=我参与的（ws1 owner 行 + ws2 viewer 成员行）
    rows = await db.list_workspaces(domain_id=domain, user_id=u1)
    assert [r["id"] for r in rows] == [ws2["id"], ws1["id"]]
    # 无关者不可见
    assert await db.list_workspaces(domain_id=domain, user_id=u3) == []

    # 归档分列（status 字段口径——active/archived 由调用方分列）：ws2 归档后
    # 成员口径 active 只剩 ws1；admin 口径 archived 单列 ws2
    await db.archive_workspace(workspace_id=ws2["id"])
    rows = await db.list_workspaces(domain_id=domain, user_id=u1)
    active = [r for r in rows if r["status"] == "active"]
    assert [r["id"] for r in active] == [ws1["id"]]
    admin_rows = await db.list_workspaces(
        domain_id=domain, user_id=u3, is_site_admin=True)
    archived = [r for r in admin_rows if r["status"] == "archived"]
    assert [r["id"] for r in archived] == [ws2["id"]]


async def test_list_and_get_carry_owner_username(async_pool) -> None:
    """负责人显示用户名（用户测试反馈修复1）：get/list 均 LEFT JOIN kb_users
    透出 owner_username（照 list_members 的 JOIN 手法）；owner 用户行不
    存在（外部写入的悬空 id，表间无 FK）→ None 不炸（回落显示归前端：
    id 前 8 位）。create 的 RETURNING 不带该列（INSERT 无从 JOIN）——前端
    建后进详情走 get，不依赖 create 响应携带。"""
    kbdb = KbDB(async_pool)
    uname = f"ws-u-{uuid.uuid4().hex[:8]}"
    owner = str((await kbdb.create_user(username=uname))["id"])
    db = WorkspaceDB(async_pool)
    dom = f"dom-{uuid.uuid4().hex[:8]}"
    ws = await db.create_workspace(
        domain_id=dom, name="负责人显示", owner_user_id=owner, created_by=owner)
    ghost = await db.create_workspace(
        domain_id=dom, name="悬空负责人", owner_user_id="u-no-such-user-row",
        created_by="u-no-such-user-row")

    got = await db.get_workspace(workspace_id=ws["id"])
    assert got is not None and got["owner_username"] == uname
    got_ghost = await db.get_workspace(workspace_id=ghost["id"])
    assert got_ghost is not None and got_ghost["owner_username"] is None

    rows = await db.list_workspaces(
        domain_id=dom, user_id=owner, is_site_admin=True)
    by_id = {r["id"]: r.get("owner_username") for r in rows}
    assert by_id[ws["id"]] == uname
    assert by_id[ghost["id"]] is None
    # 老列原样保留（owner_user_id 仍是审计口径的权威值）
    assert all(r["owner_user_id"] for r in rows)


async def test_patch_workspace_and_optimistic_token(async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    with pytest.raises(WorkspaceNotFoundError):
        await db.patch_workspace(workspace_id="wsp_missing", name="x")

    token = ws["updated_at"]
    row = await db.patch_workspace(
        workspace_id=ws["id"], name="新名", description="新描述")
    assert row["name"] == "新名" and row["description"] == "新描述"
    assert row["updated_at"] > token  # TEXT ISO 字典序=时间序

    # 空补丁：原样返回不推令牌
    same = await db.patch_workspace(workspace_id=ws["id"])
    assert same["updated_at"] == row["updated_at"]

    # stale 令牌 → Conflict，且不覆盖已提交的改动
    with pytest.raises(WorkspaceConflictError):
        await db.patch_workspace(
            workspace_id=ws["id"], name="迟到者", expected_updated_at=token)
    got = await db.get_workspace(workspace_id=ws["id"])
    assert got["name"] == "新名"

    ok = await db.patch_workspace(
        workspace_id=ws["id"], description="再改",
        expected_updated_at=same["updated_at"])
    assert ok["description"] == "再改"


async def test_archive_workspace_idempotent(async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    row = await db.archive_workspace(workspace_id=ws["id"])
    assert row["status"] == "archived"
    token = row["updated_at"]
    # 幂等：重复归档不报错、不推令牌
    again = await db.archive_workspace(workspace_id=ws["id"])
    assert again["status"] == "archived" and again["updated_at"] == token
    with pytest.raises(WorkspaceNotFoundError):
        await db.archive_workspace(workspace_id="wsp_missing")


# ------------------------------------------------------------ put_graph

async def test_put_graph_happy_path_writes_flags(
        async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    out = await db.put_graph(
        workspace_id=ws["id"], graph=_abc_graph(), expected_revision=0)
    assert out["graph_revision"] == 1
    # 全新节点（旧图为空）→ pending=true
    flags = {n["id"]: (n["pending"], n["stale"])
             for n in out["graph"]["nodes"]}
    assert flags == {"A": (True, False), "B": (True, False),
                     "C": (True, False)}

    row = await db.get_workspace(workspace_id=ws["id"])
    assert row["graph_revision"] == 1
    assert {n["id"] for n in row["graph_json"]["nodes"]} == {"A", "B", "C"}
    assert row["updated_at"] == out["updated_at"]  # 同事务原子推进


async def test_put_graph_optimistic_conflict_and_not_found(async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    await db.put_graph(
        workspace_id=ws["id"], graph=_abc_graph(), expected_revision=0)
    # 旧 revision 二次 PUT → Conflict（FOR UPDATE 锁内权威比对）
    with pytest.raises(WorkspaceConflictError):
        await db.put_graph(
            workspace_id=ws["id"], graph=_abc_graph(), expected_revision=0)
    row = await db.get_workspace(workspace_id=ws["id"])
    assert row["graph_revision"] == 1  # 冲突整体回滚不推进

    with pytest.raises(WorkspaceNotFoundError):
        await db.put_graph(
            workspace_id="wsp_missing", graph=_abc_graph(),
            expected_revision=0)


async def test_put_graph_rejects_cycle(async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    cyclic = _graph(
        _node("A", "filter", inputs=["B:main"]),
        _node("B", "filter", inputs=["A:main"]),
    )
    with pytest.raises(WorkspaceValidationError):
        await db.put_graph(workspace_id=ws["id"], graph=cyclic,
                           expected_revision=0)
    assert (await db.get_workspace(
        workspace_id=ws["id"]))["graph_revision"] == 0


async def test_put_graph_structural_validation(async_pool) -> None:
    db, ws, _ = await _seed(async_pool)
    bad_graphs = [
        # inputs 引用不存在的节点
        _graph(_node("A", "source"),
               _node("B", "filter", inputs=["ghost:main"])),
        # inputs 引用存在节点上不存在的端口
        _graph(_node("A", "source"),
               _node("B", "filter", inputs=["A:missing_port"])),
        # source 节点带 inputs
        _graph(_node("A", "source", inputs=["B:main"]),
               _node("B", "source")),
        # 节点 id 重复
        _graph(_node("A", "source"), _node("A", "filter")),
        # 节点 id 含冒号（node:port 引用无歧义的扩展校验专属负例）
        _graph(_node("A:B", "source")),
        # 非法 op
        _graph(_node("A", "神仙算子")),
        # 空端口
        _graph({**_node("A", "source"), "ports": []}),
        # 自环
        _graph(_node("A", "filter", inputs=["A:main"])),
        # 结构畸形：nodes 非列表 / 节点非对象 / graph 非对象
        {"nodes": "不是列表"},
        {"nodes": ["不是节点"]},
        [],
    ]
    for bad in bad_graphs:
        with pytest.raises(WorkspaceValidationError):
            await db.put_graph(workspace_id=ws["id"], graph=bad,
                               expected_revision=0)
    assert (await db.get_workspace(
        workspace_id=ws["id"]))["graph_revision"] == 0


async def _clear_run_flags(async_pool, workspace_id: str) -> None:
    """模拟执行器成功后清标志（Task 6 jsonb_set 定点清除的同款终态：
    只清 graph_json 里的 pending/stale，不推 graph_revision）。"""
    db = WorkspaceDB(async_pool)
    row = await db.get_workspace(workspace_id=workspace_id)
    cleared = {"nodes": [
        {**n, "pending": False, "stale": False}
        for n in row["graph_json"]["nodes"]]}
    async with async_pool.connection() as conn:
        await conn.execute(
            "UPDATE kb_workspaces SET graph_json = %s::jsonb WHERE id = %s",
            (json.dumps(cleared, ensure_ascii=False), workspace_id))


async def test_put_graph_diff_flags_pending_stale(async_pool) -> None:
    """服务端 diff 标志（继承语义：标志只由执行清除，PUT 对未变节点保留
    旧值——无关 PUT 不丢「未执行的已改」状态）：
    - v1 全新节点 pending；同图重 PUT（无关 PUT）不丢 pending；
    - 模拟执行清零后改 B.config → B.pending / C.stale（严格后代）/ 上游
      A 不动（两标志皆 False）——规格正典场景；
    - v3 新节点 D pending；未变节点继承 v2 标志；
    - v4 inputs 重接 → C.pending（inputs 变化与 config 同权，旧 stale 让位）。"""
    db, ws, _ = await _seed(async_pool)
    v1 = _abc_graph()
    out1 = await db.put_graph(workspace_id=ws["id"], graph=v1,
                              expected_revision=0)
    assert {n["id"]: (n["pending"], n["stale"])
            for n in out1["graph"]["nodes"]} == {
        "A": (True, False), "B": (True, False), "C": (True, False)}

    # 同图重 PUT（无任何变化）：继承语义正向证明——未执行的 pending 不丢
    out_noop = await db.put_graph(workspace_id=ws["id"], graph=_abc_graph(),
                                  expected_revision=1)
    assert {n["id"]: (n["pending"], n["stale"])
            for n in out_noop["graph"]["nodes"]} == {
        "A": (True, False), "B": (True, False), "C": (True, False)}

    # 模拟执行成功（清标志）后再改 B.config：正典 diff 场景
    await _clear_run_flags(async_pool, ws["id"])
    v2 = _abc_graph()
    v2["nodes"][1]["config"] = {"field": "状态", "cond": "ne", "value": "退网"}
    out2 = await db.put_graph(workspace_id=ws["id"], graph=v2,
                              expected_revision=2)
    assert out2["graph_revision"] == 3
    flags = {n["id"]: (n["pending"], n["stale"])
             for n in out2["graph"]["nodes"]}
    assert flags["B"] == (True, False)   # config 变化 → pending
    assert flags["C"] == (False, True)   # B 的严格下游 → stale
    assert flags["A"] == (False, False)  # 上游不动（未被本次 diff 触碰）

    # v3：只新增 D（挂 A 下游）——新节点 pending；B/C 未变继承 v2 标志
    v3 = _graph(
        _node("A", "source", name="源"),
        _node("B", "filter",
              config={"field": "状态", "cond": "ne", "value": "退网"},
              inputs=["A:main"]),
        _node("C", "select",
              config={"columns": [{"id": "名称", "name": "名称"}]},
              inputs=["B:main"]),
        _node("D", "select",
              config={"columns": [{"id": "名称", "name": "名称"}]},
              inputs=["A:main"]),
    )
    out3 = await db.put_graph(workspace_id=ws["id"], graph=v3,
                              expected_revision=3)
    assert out3["graph_revision"] == 4
    flags3 = {n["id"]: (n["pending"], n["stale"])
              for n in out3["graph"]["nodes"]}
    assert flags3["D"] == (True, False)   # 新节点 pending
    assert flags3["B"] == (True, False)   # 继承 v2 pending
    assert flags3["C"] == (False, True)   # 继承 v2 stale
    assert flags3["A"] == (False, False)

    # 两标志随 graph_json 原子写入（落库即带）
    row = await db.get_workspace(workspace_id=ws["id"])
    stored = {n["id"]: (n["pending"], n["stale"])
              for n in row["graph_json"]["nodes"]}
    assert stored == flags3

    # v4：C 的 inputs 重接 B→A（config 不动）→ C 变更节点 pending
    v4 = _graph(
        _node("A", "source", name="源"),
        _node("B", "filter",
              config={"field": "状态", "cond": "ne", "value": "退网"},
              inputs=["A:main"]),
        _node("C", "select",
              config={"columns": [{"id": "名称", "name": "名称"}]},
              inputs=["A:main"]),
        _node("D", "select",
              config={"columns": [{"id": "名称", "name": "名称"}]},
              inputs=["A:main"]),
    )
    out4 = await db.put_graph(workspace_id=ws["id"], graph=v4,
                              expected_revision=4)
    flags4 = {n["id"]: (n["pending"], n["stale"])
              for n in out4["graph"]["nodes"]}
    assert flags4["C"] == (True, False)   # inputs 变化 → pending（旧 stale 让位）
    assert flags4["B"] == (True, False)   # 未变继承
    assert flags4["D"] == (True, False)   # 未变继承


def _ds(n: int = 2) -> dict:
    """write_outputs 用的最小数据集（Task 4 契约形态）。"""
    return {"cols": ["型号"], "rows": [
        {"_id": f"r{i:05d}", "_source": "表/P{i}", "型号": f"P{i}"}
        for i in range(n)]}


async def test_put_graph_node_deletion_cascade_cleanup(async_pool) -> None:
    """删节点清理语义（用户测试反馈修复2）：put_graph diff 检出**被删节点**
    （旧图有、新图无）→ 同事务清理 ①kb_workspace_sources 行 ②
    kb_workspace_run_outputs 当前行（is_current——历史行保留）③
    kb_workspace_output_bindings 行；下游仍在图且引用被删节点 → 既有
    inputs 引用校验 422（消息点名违规节点），整体拒绝不清理。"""
    db, ws, owner = await _seed(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    v1 = _graph(
        _node("A", "source", name="库源"),
        _node("B", "filter", inputs=["A:main"]),
        _node("C", "select", inputs=["B:main"]),
        _node("D", "source", name="表源"),
    )
    await db.put_graph(workspace_id=ws["id"], graph=v1, expected_revision=0)
    await db.upsert_source(
        workspace_id=ws["id"], node_id="A", connector="kb_dir",
        config={"kb_id": "kb-1", "directories": []},
        input_manifest={"doc_count": 1, "documents": []}, updated_by=owner)
    await db.upsert_source(
        workspace_id=ws["id"], node_id="D", connector="excel",
        config={"file_name": "a.xlsx", "sheet_name": "S"},
        input_manifest={"row_count": 2, "rows": []}, updated_by=owner)

    # run1 全节点各写一版；run2 再写 B/D → B/D 有 run1 历史 + run2 当前行
    async def _start_run(graph_hash: str) -> dict:
        out = await rdb.try_start_guard(
            None, ws["id"], owner, graph_snapshot=v1, graph_hash=graph_hash,
            scope="workspace", target_node_id=None, batch_no=1)
        assert out["in_flight"] is None and out["run"] is not None
        return out["run"]

    run1 = await _start_run("h1")
    for nid in ("A", "B", "C", "D"):
        await rdb.write_outputs(
            None, run_id=run1["id"], workspace_id=ws["id"], node_id=nid,
            outputs={"main": {"dataset": _ds(2), "quality": {}}})
    assert await rdb.mark_completed(run1["id"]) is True
    run2 = await _start_run("h2")
    for nid in ("B", "D"):
        await rdb.write_outputs(
            None, run_id=run2["id"], workspace_id=ws["id"], node_id=nid,
            outputs={"main": {"dataset": _ds(1), "quality": {}}})
    await rdb.upsert_binding(
        None, workspace_id=ws["id"], node_id="B", port_id="main",
        artifact_id="art_b", artifact_kind="table", updated_by=owner)
    await rdb.upsert_binding(
        None, workspace_id=ws["id"], node_id="D", port_id="main",
        artifact_id="art_d", artifact_kind="table", updated_by=owner)

    # ① 删中间节点 B，但 C 仍引用 B:main → 422（消息点名 C 与 B），整体
    # 拒绝：revision 不动、来源/当前输出/绑定原样
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await db.put_graph(workspace_id=ws["id"], graph=_graph(
            _node("A", "source", name="库源"),
            _node("C", "select", inputs=["B:main"]),
            _node("D", "source", name="表源")), expected_revision=1)
    assert "C" in str(exc_info.value) and "B" in str(exc_info.value)
    assert (await db.get_workspace(
        workspace_id=ws["id"]))["graph_revision"] == 1
    assert [s["node_id"]
            for s in await db.list_sources(workspace_id=ws["id"])] == ["A", "D"]
    assert set(await rdb.get_current_outputs(ws["id"])) == {
        "A:main", "B:main", "C:main", "D:main"}
    assert await rdb.get_binding(
        workspace_id=ws["id"], node_id="D", port_id="main") is not None

    # ② 删叶节点 D（source 节点带来源行+当前输出+绑定）：成功 + revision+1
    # + 三处依赖同事务清理（D 的 run1 历史行保留）
    out = await db.put_graph(workspace_id=ws["id"], graph=_graph(
        _node("A", "source", name="库源"),
        _node("B", "filter", inputs=["A:main"]),
        _node("C", "select", inputs=["B:main"])), expected_revision=1)
    assert out["graph_revision"] == 2
    assert [s["node_id"]
            for s in await db.list_sources(workspace_id=ws["id"])] == ["A"]
    current = await rdb.get_current_outputs(ws["id"])
    assert set(current) == {"A:main", "B:main", "C:main"}
    assert current["B:main"]["run_id"] == run2["id"]  # B 当前行仍是 run2 版
    hist1 = {(r["node_id"], r["port_id"])
             for r in await rdb.get_run_outputs(run1["id"])}
    assert ("D", "main") in hist1  # 历史行不删（审计口径）
    hist2 = {(r["node_id"], r["port_id"])
             for r in await rdb.get_run_outputs(run2["id"])}
    assert ("D", "main") not in hist2  # 当前行（run2）被清理
    assert await rdb.get_binding(
        workspace_id=ws["id"], node_id="D", port_id="main") is None
    assert await rdb.get_binding(
        workspace_id=ws["id"], node_id="B", port_id="main") is not None


async def test_purge_workspace_two_step_gate_and_cascade(async_pool) -> None:
    """归档后真删（用户反馈问题1——两步门禁，照制品「先停用再删除」）：

    - 门禁：active purge → WorkspaceValidationError（消息「先归档」），行与
      依赖原样不动；
    - 已归档 purge → 单事务清理：bindings/exports 两表**手清**（无 FK），
      kb_workspaces 主行删除后 members/sources/runs 靠既有 FK ON DELETE
      CASCADE 连带、run_outputs 随 runs 级联（run_id FK）——七表全空；
    - 不存在的工作区 → NotFound；返回删除主表行数（1）。
    """
    db, ws, owner = await _seed(async_pool)
    rdb = WorkspaceRunDB(async_pool)
    graph = _graph(_node("A", "source"), _node("B", "filter",
                                               inputs=["A:main"]))
    await db.put_graph(workspace_id=ws["id"], graph=graph, expected_revision=0)
    await db.upsert_source(
        workspace_id=ws["id"], node_id="A", connector="kb_dir",
        config={"kb_id": "kb-1", "directories": []},
        input_manifest={"doc_count": 1, "documents": []}, updated_by=owner)
    out = await rdb.try_start_guard(
        None, ws["id"], owner, graph_snapshot=graph, graph_hash="h1",
        scope="workspace", target_node_id=None, batch_no=2)
    run = out["run"]
    await rdb.write_outputs(
        None, run_id=run["id"], workspace_id=ws["id"], node_id="B",
        outputs={"main": {"dataset": _ds(2), "quality": {}}})
    await rdb.upsert_binding(
        None, workspace_id=ws["id"], node_id="B", port_id="main",
        artifact_id="art_b", artifact_kind="table", updated_by=owner)
    await rdb.insert_export(
        None, request_id=f"req-{ws['id']}", workspace_id=ws["id"],
        node_id="B", port_id="main", mapping_json={}, created_by=owner)

    # 门禁第一步：active → Validation（提示先归档），行与依赖原样
    with pytest.raises(WorkspaceValidationError, match="先归档"):
        await db.purge_workspace(workspace_id=ws["id"])
    assert await db.get_workspace(workspace_id=ws["id"]) is not None
    assert await rdb.get_binding(
        workspace_id=ws["id"], node_id="B", port_id="main") is not None
    assert set(await rdb.get_current_outputs(ws["id"])) == {"B:main"}

    # 第二步：归档后 purge → 主行 + 六张依赖表全空（级联验证）
    await db.archive_workspace(workspace_id=ws["id"])
    assert await db.purge_workspace(workspace_id=ws["id"]) == 1
    assert await db.get_workspace(workspace_id=ws["id"]) is None
    async with async_pool.connection() as conn:
        for table in ("kb_workspace_members", "kb_workspace_sources",
                      "kb_workspace_runs", "kb_workspace_run_outputs",
                      "kb_workspace_output_bindings",
                      "kb_workspace_exports"):
            cur = await conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
                " WHERE workspace_id = %s", (ws["id"],))
            assert (await cur.fetchone())["n"] == 0, table

    # 不存在的工作区 → NotFound（与归档同面）
    with pytest.raises(WorkspaceNotFoundError):
        await db.purge_workspace(workspace_id="wsp_missing")


# ------------------------------------------------------------ 成员

async def test_members_lifecycle_and_last_owner_protection(async_pool) -> None:
    db, ws, owner = await _seed(async_pool)
    u2 = await _user(async_pool)

    row = await db.add_member(workspace_id=ws["id"], user_id=u2, role="editor")
    assert row["role"] == "editor"
    listed = await db.list_members(workspace_id=ws["id"])
    roles = {m["user_id"]: m["role"] for m in listed}
    assert roles == {owner: "owner", u2: "editor"}
    # 成员清单带用户名（kb_users JOIN）
    by_user = {m["user_id"]: m for m in listed}
    assert by_user[owner].get("username")

    upd = await db.update_member_role(
        workspace_id=ws["id"], user_id=u2, role="viewer")
    assert upd["role"] == "viewer"
    assert await db.member_role(workspace_id=ws["id"], user_id=u2) == "viewer"
    assert await db.member_role(
        workspace_id=ws["id"], user_id="u_none") is None

    # 最后一个 owner 保护：移除 / 降级均拒
    with pytest.raises(WorkspaceValidationError):
        await db.remove_member(workspace_id=ws["id"], user_id=owner)
    with pytest.raises(WorkspaceValidationError):
        await db.update_member_role(
            workspace_id=ws["id"], user_id=owner, role="editor")

    # 第二个 owner 就位后，原 owner 可移除
    await db.add_member(workspace_id=ws["id"], user_id=u2, role="owner")
    assert await db.remove_member(workspace_id=ws["id"], user_id=owner) is True
    assert await db.member_role(workspace_id=ws["id"], user_id=owner) is None
    # 移除不存在的成员 → False；更新不存在成员角色 → NotFound
    assert await db.remove_member(
        workspace_id=ws["id"], user_id=owner) is False
    with pytest.raises(WorkspaceNotFoundError):
        await db.update_member_role(
            workspace_id=ws["id"], user_id=owner, role="viewer")
    # 非法角色 / 不存在的工作区
    with pytest.raises(WorkspaceValidationError):
        await db.add_member(workspace_id=ws["id"], user_id=u2, role="神仙")
    with pytest.raises(WorkspaceNotFoundError):
        await db.add_member(workspace_id="wsp_missing", user_id=u2,
                            role="viewer")


# ------------------------------------------------------------ 质量收口（护栏）


async def test_put_graph_node_cap(async_pool) -> None:
    """节点数上限（MAX_GRAPH_NODES=200）：超限整图拒绝，恰好 200 放行。"""
    db, ws, _ = await _seed(async_pool)
    ok200 = _graph(*(
        _node(f"n{i:03d}", "source", name=f"源{i}") for i in range(200)))
    out = await db.put_graph(workspace_id=ws["id"], graph=ok200,
                             expected_revision=0)
    assert out["graph_revision"] == 1

    over = _graph(*(
        _node(f"m{i:03d}", "source", name=f"源{i}") for i in range(201)))
    with pytest.raises(WorkspaceValidationError):
        await db.put_graph(workspace_id=ws["id"], graph=over,
                           expected_revision=1)
    assert (await db.get_workspace(
        workspace_id=ws["id"]))["graph_revision"] == 1  # 拒绝不推进


async def test_put_graph_rejects_nonfinite_coords(async_pool) -> None:
    """x/y 非有限值（NaN/±inf）拒绝——json.dumps 能带出 NaN，DB jsonb 也
    存得进，必须在形态校验层拦下；10**400 巨型 int 走 math.isfinite 的
    OverflowError 分支（Task 3 规格审查 M1 携带补口）。"""
    db, ws, _ = await _seed(async_pool)
    for bad in (float("nan"), float("inf"), float("-inf"), 10 ** 400):
        for axis in ("x", "y"):
            graph = _graph({**_node("A", "source"), axis: bad})
            with pytest.raises(WorkspaceValidationError):
                await db.put_graph(workspace_id=ws["id"], graph=graph,
                                   expected_revision=0)
    assert (await db.get_workspace(
        workspace_id=ws["id"]))["graph_revision"] == 0


# ------------------------------------------------------------ 来源

async def test_upsert_source_batch_progression_and_crud(async_pool) -> None:
    db, ws, owner = await _seed(async_pool)
    with pytest.raises(WorkspaceValidationError):
        await db.upsert_source(
            workspace_id=ws["id"], node_id="n1", connector="ftp",
            config={}, input_manifest={}, updated_by=owner)
    with pytest.raises(WorkspaceNotFoundError):
        await db.upsert_source(
            workspace_id="wsp_missing", node_id="n1", connector="kb_dir",
            config={}, input_manifest={}, updated_by=owner)

    row = await db.upsert_source(
        workspace_id=ws["id"], node_id="n1", connector="kb_dir",
        config={"kb_id": "kb-1", "directories": ["方案"]},
        input_manifest={"doc_count": 3, "documents": []}, updated_by=owner)
    assert row["id"].startswith("wss_")
    assert row["connector"] == "kb_dir"
    assert row["config"] == {"kb_id": "kb-1", "directories": ["方案"]}
    assert row["input_manifest"]["doc_count"] == 3
    assert row["last_read_at"]
    assert (await db.get_workspace(workspace_id=ws["id"]))["batch_no"] == 2

    # 同 node_id 重 PUT：UNIQUE(workspace_id,node_id) 冲突即更新 + 再推批次
    token_before = (await db.get_workspace(
        workspace_id=ws["id"]))["updated_at"]
    row2 = await db.upsert_source(
        workspace_id=ws["id"], node_id="n1", connector="kb_dir",
        config={"kb_id": "kb-2", "directories": []},
        input_manifest={"doc_count": 5, "documents": []}, updated_by=owner)
    assert row2["id"] == row["id"]  # UPSERT 命中同一行
    assert row2["config"] == {"kb_id": "kb-2", "directories": []}
    ws_row = await db.get_workspace(workspace_id=ws["id"])
    assert ws_row["batch_no"] == 3
    assert ws_row["updated_at"] > token_before  # 同事务推进工作台令牌

    # 第二个来源节点：批次再 +1
    await db.upsert_source(
        workspace_id=ws["id"], node_id="n2", connector="excel",
        config={"file_name": "a.xlsx", "sheet_name": "S1"},
        input_manifest={"rows": []}, updated_by=owner)
    assert (await db.get_workspace(workspace_id=ws["id"]))["batch_no"] == 4

    listed = await db.list_sources(workspace_id=ws["id"])
    assert [s["node_id"] for s in listed] == ["n1", "n2"]
    assert listed[1]["connector"] == "excel"

    assert await db.delete_source(workspace_id=ws["id"], node_id="n1") is True
    assert await db.delete_source(workspace_id=ws["id"], node_id="n1") is False
    assert [s["node_id"]
            for s in await db.list_sources(workspace_id=ws["id"])] == ["n2"]
    assert await db.list_sources(workspace_id="wsp_missing") == []
