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
