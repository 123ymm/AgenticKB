# -*- coding: utf-8 -*-
"""工作台 REST 路由测试（67号 MVP Task 8）—— httpx ASGI + fake（不碰真库）。

**2026-11-17 收紧口径（用户指令：工作台仅站点/域管理员可用）**：
_assert_ws_role 只剩 admin 两判定（site→"admin" / is_domain_admin→
"domain_admin"），其余一律 404 反探测——绑定成员/成员行/owner 兜底/ viewer
全退役；create 与 list 加管理员门禁（403 workspace_forbidden）。本文件
seed 相应升级：默认操作者 u-alice=域管理员，u-viewer/u-bob=绑定普通成员
（负侧），u-domadm=第二域管理员，u-root=site admin，u-foreigndm=他域
域管理员。

手法照 test_artifacts_routes.py：dependency_overrides 注入真实
WorkspaceService（内部换 fake 仓储/执行器——构造注入面只有 pool/executor 两
个生产参数，测试以保护属性直替，先例=env.db.update_cell = raise_stale），
read_kb_dir 经 service 模块属性 monkeypatch（即读即冻链路的接缝在适配器
边界）。打真实路由层：反探测 404（非管理员与不存在同码）/ 建台·列表管理员
门禁 403 / admin 全通 / 图环 422 / 乐观锁 409 / 来源 KB 无权 403 /
即读即冻 batch_no 推进 / runs 202·409·422 / excel 201·413·422 /
members 最后 owner 保护。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
    _apply_run_flags,
    _validate_graph,
)
from knowledge_mining.mining.kb.deps import get_artifact_db, get_workspace_service
from knowledge_mining.mining.kb.routes.workspaces import router as workspaces_router
from knowledge_mining.mining.kb.workspace_service import WorkspaceService
from knowledge_mining.mining.kb.workspace_sources import WorkspacePermissionError

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
DOMAIN2 = "power_grid"


def _user(uid: str, site_role: str = "member") -> dict:
    """auth.current_user 返回的 kb_users 行形态（id/site_role）。"""
    return {"id": uid, "username": uid, "site_role": site_role, "status": "active"}


_CLOCK_TICK = 0


def _tick() -> str:
    """fake 时间戳：单调递增（乐观锁令牌语义）。"""
    global _CLOCK_TICK
    _CLOCK_TICK += 1
    return (datetime(2026, 11, 16, tzinfo=timezone.utc)
            + timedelta(microseconds=_CLOCK_TICK)).isoformat()


class FakeConnPool:
    """kb_dir 即读即冻用的假 pool——read_kb_dir 已被 monkeypatch，conn 只是
    位置参数；fake 仓储一律忽略注入连接。"""

    @asynccontextmanager
    async def connection(self):
        yield object()


class FakeWorkspaceDB:
    """WorkspaceDB 接口的内存实现；图校验/diff 委托 db_workspaces 真函数
    （环/引用/形态 422 与服务端 diff 标志的路由行为因此与真库同源）。"""

    def __init__(self) -> None:
        self.workspaces: dict[str, dict] = {}
        self.members: dict[tuple[str, str], str] = {}   # (ws_id, uid) -> role
        self.sources: dict[tuple[str, str], dict] = {}  # (ws_id, node_id)

    async def create_workspace(self, conn=None, *, domain_id: str, name: str,
                               description: str = "", owner_user_id: str,
                               created_by: str, actor_kind: str = "user",
                               actor_id: str = "") -> dict:
        ws = {
            "id": f"wsp_{uuid.uuid4().hex[:16]}", "domain_id": domain_id,
            "name": name, "description": description,
            "owner_user_id": owner_user_id,
            "owner_username": f"uname-{owner_user_id}", "status": "active",
            "graph_json": {"nodes": []}, "graph_revision": 0, "batch_no": 1,
            "actor_kind": actor_kind, "actor_id": actor_id,
            "created_by": created_by, "created_at": _tick(),
            "updated_at": _tick(),
        }
        self.workspaces[ws["id"]] = ws
        self.members[(ws["id"], owner_user_id)] = "owner"
        return dict(ws)

    async def get_workspace(self, conn=None, *, workspace_id: str):
        ws = self.workspaces.get(workspace_id)
        return dict(ws) if ws else None

    async def list_workspaces(self, conn=None, *, domain_id: str, user_id: str,
                              is_site_admin: bool = False,
                              is_domain_admin: bool = False) -> list[dict]:
        out = []
        for ws in self.workspaces.values():
            if ws["domain_id"] != domain_id:
                continue
            visible = (is_site_admin or is_domain_admin
                       or ws["owner_user_id"] == user_id
                       or any(k == (ws["id"], user_id)
                              for k in self.members))
            if visible:
                out.append({k: v for k, v in ws.items()
                            if k != "graph_json"})
        return sorted(out, key=lambda r: r["created_at"], reverse=True)

    async def patch_workspace(self, conn=None, *, workspace_id: str,
                              name: str | None = None,
                              description: str | None = None,
                              expected_updated_at: str | None = None) -> dict:
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if expected_updated_at is not None \
                and ws["updated_at"] != expected_updated_at:
            raise WorkspaceConflictError("workspace updated concurrently")
        if name is not None:
            ws["name"] = name
        if description is not None:
            ws["description"] = description
        if name is not None or description is not None:
            ws["updated_at"] = _tick()
        return dict(ws)

    async def archive_workspace(self, conn=None, *, workspace_id: str) -> dict:
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if ws["status"] == "active":
            ws["status"] = "archived"
            ws["updated_at"] = _tick()
        return dict(ws)

    async def purge_workspace(self, conn=None, *, workspace_id: str) -> int:
        """真删（两步门禁对齐真库）：仅 archived 可删；行与成员/来源行
        （fake 态级联）一并清除，返回主表删除行数。"""
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if ws["status"] != "archived":
            raise WorkspaceValidationError(
                f"workspace is active, 先归档: {workspace_id}")
        del self.workspaces[workspace_id]
        self.members = {k: v for k, v in self.members.items()
                        if k[0] != workspace_id}
        self.sources = {k: v for k, v in self.sources.items()
                        if k[0] != workspace_id}
        return 1

    async def put_graph(self, conn=None, *, workspace_id: str, graph,
                        expected_revision: int) -> dict:
        normalized = _validate_graph(graph)
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if ws["graph_revision"] != expected_revision:
            raise WorkspaceConflictError(
                f"graph_revision mismatch: expected {expected_revision},"
                f" current {ws['graph_revision']}")
        new_graph = _apply_run_flags(normalized, ws["graph_json"])
        ws["graph_json"] = new_graph
        ws["graph_revision"] = int(ws["graph_revision"]) + 1
        ws["updated_at"] = _tick()
        return {"graph": new_graph,
                "graph_revision": ws["graph_revision"],
                "updated_at": ws["updated_at"]}

    async def member_role(self, conn=None, *, workspace_id: str,
                          user_id: str):
        return self.members.get((workspace_id, user_id))

    async def list_members(self, conn=None, *, workspace_id: str) -> list[dict]:
        rows = [{"user_id": uid, "role": role, "username": uid,
                 "user_name": uid}
                for (w, uid), role in self.members.items() if w == workspace_id]
        return sorted(rows, key=lambda r: (r["role"], r["user_id"]))

    def _guard_last_owner(self, workspace_id: str, user_id: str) -> None:
        owners = {uid for (w, uid), role in self.members.items()
                  if w == workspace_id and role == "owner"}
        if user_id in owners and len(owners) <= 1:
            raise WorkspaceValidationError(
                "cannot remove or demote the last owner")

    async def add_member(self, conn=None, *, workspace_id: str, user_id: str,
                         role: str) -> dict:
        if workspace_id not in self.workspaces:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if role not in ("owner", "editor", "viewer"):
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        if role != "owner":
            self._guard_last_owner(workspace_id, user_id)
        self.members[(workspace_id, user_id)] = role
        return {"workspace_id": workspace_id, "user_id": user_id,
                "role": role, "created_at": _tick()}

    async def update_member_role(self, conn=None, *, workspace_id: str,
                                 user_id: str, role: str) -> dict:
        if role not in ("owner", "editor", "viewer"):
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        if role != "owner":
            self._guard_last_owner(workspace_id, user_id)
        if (workspace_id, user_id) not in self.members:
            raise WorkspaceNotFoundError(f"member not found: {user_id}")
        self.members[(workspace_id, user_id)] = role
        return {"workspace_id": workspace_id, "user_id": user_id,
                "role": role, "created_at": _tick()}

    async def remove_member(self, conn=None, *, workspace_id: str,
                            user_id: str) -> bool:
        self._guard_last_owner(workspace_id, user_id)
        return self.members.pop((workspace_id, user_id), None) is not None

    async def upsert_source(self, conn=None, *, workspace_id: str,
                            node_id: str, connector: str, config: dict,
                            input_manifest: dict, updated_by: str = "") -> dict:
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        now = _tick()
        key = (workspace_id, node_id)
        if key in self.sources:
            row = self.sources[key]
            row.update({"connector": connector, "config": config,
                        "input_manifest": input_manifest,
                        "last_read_at": now, "updated_at": now})
        else:
            row = {"id": f"wss_{uuid.uuid4().hex[:16]}",
                   "workspace_id": workspace_id, "node_id": node_id,
                   "connector": connector, "config": config,
                   "input_manifest": input_manifest, "last_read_at": now,
                   "created_by": updated_by, "created_at": now,
                   "updated_at": now}
            self.sources[key] = row
        ws["batch_no"] = int(ws["batch_no"]) + 1  # WB-07 同事务推进
        ws["updated_at"] = now
        return dict(row)

    async def list_sources(self, conn=None, *, workspace_id: str) -> list[dict]:
        return [dict(r) for (w, _), r in sorted(self.sources.items())
                if w == workspace_id]

    async def delete_source(self, conn=None, *, workspace_id: str,
                            node_id: str) -> bool:
        return self.sources.pop((workspace_id, node_id), None) is not None


class FakeDomainDirectory:
    """ArtifactDB 域谓词面的内存实现（is_domain_admin / user_in_domain /
    can_create_in_domain / first_domain_for_user / get_user_status）。"""

    def __init__(self) -> None:
        self.domain_admins: set[tuple[str, str]] = set()
        self.bindings: set[tuple[str, str]] = set()
        self.site_admins: set[str] = set()
        self.users: dict[str, str] = {}

    async def is_domain_admin(self, user_id: str, domain_id: str) -> bool:
        # 真库口径（db_artifacts.is_domain_admin）：active site admin 全通 /
        # 本域 domain_role='admin' —— fake 同步含 site_admins。
        return ((user_id, domain_id) in self.domain_admins
                or user_id in self.site_admins)

    async def user_in_domain(self, user_id: str, domain_id: str) -> bool:
        return (user_id, domain_id) in self.bindings

    async def can_create_in_domain(self, user_id: str, domain_id: str) -> bool:
        return ((user_id, domain_id) in self.bindings
                or (user_id, domain_id) in self.domain_admins
                or user_id in self.site_admins)

    async def first_domain_for_user(self, user_id: str):
        doms = sorted(d for (u, d) in self.bindings if u == user_id)
        return doms[0] if doms else None

    async def get_user_status(self, user_id: str):
        return self.users.get(user_id)


class FakeRunDB:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}

    async def get_run(self, run_id: str):
        row = self.runs.get(run_id)
        return dict(row) if row else None

    async def list_runs(self, workspace_id: str, limit: int = 10) -> list[dict]:
        rows = [dict(r) for r in self.runs.values()
                if r["workspace_id"] == workspace_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [{k: v for k, v in r.items()
                 if k != "graph_snapshot_json"} for r in rows[:limit]]

    async def get_current_outputs(self, workspace_id: str) -> dict:
        return {}


class FakeExecutor:
    """执行器四入口的内存桩：单飞冲突/NotFound 反探测语义与真执行器对齐。"""

    def __init__(self, rdb: FakeRunDB) -> None:
        self.rdb = rdb
        self.in_flight: str | None = None
        self.started: list[dict] = []
        self.trial_results: dict[str, dict] = {}

    async def start_run(self, workspace_id: str, *, scope: str,
                        target_node_id: str | None = None,
                        created_by: str = "", actor_kind: str = "user",
                        actor_id: str = "") -> str:
        if scope not in ("workspace", "node"):
            raise WorkspaceValidationError(
                f"scope 未接线（仅 workspace/node，subgraph 预留）: {scope!r}")
        if self.in_flight is not None:
            raise WorkspaceConflictError(
                f"run in progress (workspace 单飞): {self.in_flight}")
        run_id = f"wsr_{uuid.uuid4().hex[:16]}"
        self.in_flight = run_id
        self.started.append({"workspace_id": workspace_id, "scope": scope,
                             "target_node_id": target_node_id,
                             "created_by": created_by})
        self.rdb.runs[run_id] = {
            "id": run_id, "workspace_id": workspace_id, "scope": scope,
            "target_node_id": target_node_id, "status": "running",
            "graph_snapshot_json": {"nodes": []}, "created_at": _tick(),
        }
        return run_id

    async def get_status(self, workspace_id: str, run_id: str) -> dict:
        row = self.rdb.runs.get(run_id)
        if row is None or row["workspace_id"] != workspace_id:
            raise WorkspaceNotFoundError(
                f"run not found in workspace: {run_id}")
        slim = {k: v for k, v in row.items()
                if k != "graph_snapshot_json"}
        return {"run": slim, "nodes": []}

    async def cancel_run(self, run_id: str) -> bool:
        row = self.rdb.runs.get(run_id)
        if row is not None and row["status"] in ("queued", "running"):
            row["status"] = "cancelled"
            self.in_flight = None
            return True
        return False

    async def trial_node(self, workspace_id: str, node_id: str) -> dict:
        result = self.trial_results.get(node_id)
        if result is None:
            raise WorkspaceValidationError(f"node not found: {node_id}")
        return result


def _make_fake_read_kb_dir(kb_states: dict):
    """read_kb_dir 桩：kb_states[id] = int(文档数) | 'denied' | 缺失=不存在。"""
    async def read_kb_dir(conn, *, kb_id: str, directories, user):
        state = kb_states.get(kb_id)
        if state is None:
            raise WorkspaceNotFoundError(f"kb not found: {kb_id}")
        if state == "denied":
            raise WorkspacePermissionError(f"kb not readable: {kb_id}")
        n = int(state)
        rows = [{"_id": f"doc-{i}", "_source": f"{kb_id}//doc-{i}",
                 "知识库": kb_id, "目录": "", "文件": f"doc-{i}",
                 "文档内容_JSON": "{}"} for i in range(n)]
        dataset = {"kind": "table",
                   "cols": ["知识库", "目录", "文件", "文档内容_JSON"],
                   "rows": rows}
        manifest = {"frozen_at": _tick(), "doc_count": n,
                    "unparsed_docs": [], "documents": [],
                    "dataset": dataset}
        return dataset, manifest
    return read_kb_dir


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    from knowledge_mining.mining.kb import workspace_service as ws_mod

    db = FakeWorkspaceDB()
    adb = FakeDomainDirectory()
    rdb = FakeRunDB()
    ex = FakeExecutor(rdb)
    svc = WorkspaceService(pool=FakeConnPool(), executor=ex)
    svc._db = db            # 测试注入面：保护属性直替（见模块 docstring）
    svc._adb = adb
    svc._rdb = rdb

    adb.bindings.update((uid, DOMAIN) for uid in
                        ("u-alice", "u-viewer", "u-bob", "u-domadm"))
    adb.bindings.add(("u-foreigndm", DOMAIN2))
    # 2026-11-17 收紧：默认操作者 u-alice 提升为域管理员（正路径全走
    # admin）；u-viewer/u-bob 留绑定普通成员（负侧 404/403）。
    adb.domain_admins.update({("u-alice", DOMAIN), ("u-domadm", DOMAIN),
                              ("u-foreigndm", DOMAIN2)})
    adb.site_admins.add("u-root")
    adb.users.update({"u-viewer": "active", "u-bob": "active"})

    kb_states = {"kb_ok": 2, "kb_denied": "denied"}
    monkeypatch.setattr(ws_mod, "read_kb_dir",
                        _make_fake_read_kb_dir(kb_states))

    app = FastAPI()
    app.include_router(workspaces_router)
    holder = {"user": _user("u-alice")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_workspace_service] = lambda: svc
    app.dependency_overrides[get_artifact_db] = lambda: adb
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport,
                           base_url="http://test") as client:
        yield SimpleNamespace(
            db=db, adb=adb, rdb=rdb, ex=ex, svc=svc, app=app,
            client=client, holder=holder, tmp_path=tmp_path,
            kb_states=kb_states,
        )


async def _create_ws(env, name: str = "组网工作台") -> str:
    r = await env.client.post("/api/workspaces", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _node(nid: str, op: str = "filter", *, inputs: tuple[str, ...] = ()) -> dict:
    return {"id": nid, "op": op, "name": nid, "config": {},
            "inputs": list(inputs),
            "ports": [{"id": "main", "name": "主输出", "kind": "table"}],
            "x": 0, "y": 0}


def _xlsx_bytes(path: Path, header: list[str], rows: list[tuple]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Sheet1")
    sheet.append(header)
    for row in rows:
        sheet.append(list(row))
    workbook.save(str(path))
    return path.read_bytes()


# ------------------------------------------------------------ CRUD 与反探测

async def test_crud_roundtrip_and_archive(env):
    wid = await _create_ws(env)
    assert wid.startswith("wsp_")
    assert env.db.members[(wid, "u-alice")] == "owner"

    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "组网工作台"
    assert body["my_role"] == "domain_admin"  # 收紧后 my_role=合成角色
    assert body["status"] == "active"
    assert body["graph_json"] == {"nodes": []}
    # 负责人用户名透传（用户测试反馈修复1：db 层 JOIN，路由/服务不滤列）
    assert body["owner_username"] == "uname-u-alice"

    r = await env.client.get("/api/workspaces")
    assert r.status_code == 200
    listed = r.json()["workspaces"]
    assert [w["id"] for w in listed] == [wid]
    assert listed[0]["owner_username"] == "uname-u-alice"

    r = await env.client.patch(
        f"/api/workspaces/{wid}",
        json={"name": "组网工作台2", "description": "网络集成方案"})
    assert r.status_code == 200 and r.json()["name"] == "组网工作台2"

    # DELETE = 归档（幂等软删）：行保留、状态翻 archived
    r = await env.client.delete(f"/api/workspaces/{wid}")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 200 and r.json()["status"] == "archived"


async def test_delete_two_state_archive_then_purge(env):
    """DELETE 两态（用户反馈问题1——归档后真删，两步门禁）：active → 200
    status=archived（行保留）；已归档 → 200 status=deleted（行真删，此后
    404 反探测）；绑定成员（原 editor 负侧）收紧后=404 反探测（非管理员
    与不存在同面）；site admin 旁路可删。"""
    wid = await _create_ws(env)
    r = await env.client.delete(f"/api/workspaces/{wid}")
    assert r.status_code == 200 and r.json()["status"] == "archived"

    # 收紧（2026-11-17）：绑定成员即使挂 editor 成员行也 → 404 反探测
    env.db.members[(wid, "u-viewer")] = "editor"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.delete(f"/api/workspaces/{wid}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"

    # owner 再 DELETE → 真删：status=deleted，行消失（fake 层级联成员行）
    env.holder["user"] = _user("u-alice")
    r = await env.client.delete(f"/api/workspaces/{wid}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"
    assert env.db.workspaces.get(wid) is None
    assert (wid, "u-viewer") not in env.db.members
    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 404
    # 已删干净后再 DELETE → 404（不复活）
    r = await env.client.delete(f"/api/workspaces/{wid}")
    assert r.status_code == 404

    # site admin 旁路对已归档工作区可真删
    wid2 = await _create_ws(env, "admin-purge")
    await env.db.archive_workspace(workspace_id=wid2)
    env.holder["user"] = _user("u-root", site_role="admin")
    r = await env.client.delete(f"/api/workspaces/{wid2}")
    assert r.status_code == 200 and r.json()["status"] == "deleted"


async def test_detail_404_antiprobe_stranger_same_as_missing(env):
    """反探测（收紧后口径）：非管理员（陌生人/绑定成员/他域域管理员）与
    不存在的 wid 同响应（404 + code=workspace_not_found）。"""
    wid = await _create_ws(env)
    env.holder["user"] = _user("u-stranger")  # 无域绑定非成员
    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"
    # 绑定普通成员（即使挂成员行）→ 同面 404
    env.db.members[(wid, "u-viewer")] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"
    # 他域域管理员 → 同面 404（is_domain_admin 按工作区域判定）
    env.holder["user"] = _user("u-foreigndm")
    r = await env.client.get(f"/api/workspaces/{wid}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"
    r = await env.client.get("/api/workspaces/wsp_missing")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"


async def test_list_visibility_admin_flags(env):
    """收紧（2026-11-17）：site/domain admin 见本域全量；绑定普通成员与
    陌生人 → 403 workspace_forbidden（列表门禁——不再有「自己参与的」面）。"""
    wid = await _create_ws(env, "alices-ws")
    env.holder["user"] = _user("u-root", site_role="admin")
    r = await env.client.get(f"/api/workspaces?domain_id={DOMAIN}")
    assert r.status_code == 200
    assert [w["id"] for w in r.json()["workspaces"]] == [wid]
    env.holder["user"] = _user("u-domadm")
    r = await env.client.get(f"/api/workspaces?domain_id={DOMAIN}")
    assert [w["id"] for w in r.json()["workspaces"]] == [wid]
    env.holder["user"] = _user("u-viewer")  # 绑定普通成员
    r = await env.client.get(f"/api/workspaces?domain_id={DOMAIN}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_forbidden"
    env.holder["user"] = _user("u-stranger")
    r = await env.client.get(f"/api/workspaces?domain_id={DOMAIN}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_forbidden"


async def test_create_requires_domain_grant(env):
    """收紧（2026-11-17）：建台管理员门禁——陌生人/绑定普通成员 → 403
    workspace_forbidden（创建无反探测对象）；域管理员 → 201；缺省域不可
    解析仍 422。"""
    env.holder["user"] = _user("u-stranger")
    r = await env.client.post(
        "/api/workspaces", json={"name": "x", "domain_id": DOMAIN})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_forbidden"
    # 绑定普通成员（can_create_in_domain 旧口径曾放行）→ 403
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(
        "/api/workspaces", json={"name": "x", "domain_id": DOMAIN})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_forbidden"
    # 域管理员（bind_domain domain_role='admin'）建台 → 成功
    env.holder["user"] = _user("u-domadm")
    r = await env.client.post(
        "/api/workspaces", json={"name": "域管建台", "domain_id": DOMAIN})
    assert r.status_code == 201, r.text
    # 他域域管理员对本域建台 → 403（is_domain_admin 按目标域判定）
    env.holder["user"] = _user("u-foreigndm")
    r = await env.client.post(
        "/api/workspaces", json={"name": "x", "domain_id": DOMAIN})
    assert r.status_code == 403
    # 未提供 domain 且无绑定域 → 422
    env.holder["user"] = _user("u-stranger")
    r = await env.client.post("/api/workspaces", json={"name": "x"})
    assert r.status_code == 422


# ----------------------------------------------------------------- 图

async def _put_graph(client, wid: str, nodes: list[dict],
                     expected_revision: int = 0):
    return await client.put(
        f"/api/workspaces/{wid}/graph",
        json={"graph": {"nodes": nodes},
              "expected_revision": expected_revision})


async def test_graph_permissions_member_404_admins_pass(env):
    """收紧（2026-11-17）：绑定成员（原 viewer 403 用例）读写图均 → 404
    反探测（与不存在同码）；陌生人同面；domain/site admin 全通。"""
    wid = await _create_ws(env)
    env.db.members[(wid, "u-viewer")] = "viewer"
    nodes = [_node("sp", "source"), _node("f", "filter", inputs=("sp:main",))]

    env.holder["user"] = _user("u-viewer")
    r = await _put_graph(env.client, wid, nodes)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "workspace_not_found"
    # 收紧后读图同样 404（read+ 角色面已退役）
    r = await env.client.get(f"/api/workspaces/{wid}/graph")
    assert r.status_code == 404

    env.holder["user"] = _user("u-stranger")
    r = await _put_graph(env.client, wid, nodes)
    assert r.status_code == 404  # 非管理员与不存在同面

    env.holder["user"] = _user("u-domadm")  # domain admin
    r = await _put_graph(env.client, wid, nodes)
    assert r.status_code == 200, r.text
    assert r.json()["graph_revision"] == 1

    env.holder["user"] = _user("u-root", site_role="admin")  # site admin
    r = await _put_graph(env.client, wid, nodes, expected_revision=1)
    assert r.status_code == 200


async def test_graph_cycle_422_and_revision_conflict_409(env):
    wid = await _create_ws(env)
    cycle = [
        {**_node("a", "filter", inputs=("b:main",))},
        {**_node("b", "filter", inputs=("a:main",))},
    ]
    r = await _put_graph(env.client, wid, cycle)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "graph_invalid"
    assert "cycle" in r.json()["detail"]["message"]

    ok = [_node("sp", "source")]
    r = await _put_graph(env.client, wid, ok)
    assert r.status_code == 200
    r = await _put_graph(env.client, wid, ok, expected_revision=0)  # 旧令牌
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "graph_conflict"


async def test_patch_stale_token_409(env):
    wid = await _create_ws(env)
    r = await env.client.patch(
        f"/api/workspaces/{wid}",
        json={"name": "新名", "expected_updated_at": "2000-01-01T00:00:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "workspace_conflict"


# ----------------------------------------------------------------- 来源

async def test_source_kb_dir_forbidden_403_and_ok_201(env):
    wid = await _create_ws(env)
    # 工作区有权但来源 KB 无权 → 403 workspace_source_forbidden
    r = await env.client.post(f"/api/workspaces/{wid}/sources", json={
        "connector": "kb_dir", "kb_id": "kb_denied", "node_id": "sp"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_source_forbidden"
    # KB 不存在 → 同面 403（不向无权者泄露 KB 存在性）
    r = await env.client.post(f"/api/workspaces/{wid}/sources", json={
        "connector": "kb_dir", "kb_id": "kb_missing", "node_id": "sp"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "workspace_source_forbidden"

    # 正常：即读即冻 → 201 带 frozen 计数 + batch_no 推进（1→2）
    r = await env.client.post(f"/api/workspaces/{wid}/sources", json={
        "connector": "kb_dir", "kb_id": "kb_ok", "node_id": "sp",
        "directories": ["/配置"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["node_id"] == "sp" and body["connector"] == "kb_dir"
    assert body["frozen"]["doc_count"] == 2
    assert body["batch_no"] == 2
    # 重接入 = 新资料批次（WB-07：再推 batch_no）
    r = await env.client.post(f"/api/workspaces/{wid}/sources", json={
        "connector": "kb_dir", "kb_id": "kb_ok", "node_id": "sp"})
    assert r.status_code == 201 and r.json()["batch_no"] == 3

    # 列表：manifest 剥 dataset（重字段不下发）
    r = await env.client.get(f"/api/workspaces/{wid}/sources")
    assert r.status_code == 200
    [row] = r.json()["sources"]
    assert row["node_id"] == "sp"
    assert "dataset" not in row["input_manifest"]
    assert row["input_manifest"]["doc_count"] == 2

    # 收紧（2026-11-17）：绑定成员接来源 → 404 反探测；DELETE 正常 → 200；再删 → 404
    env.db.members[(wid, "u-viewer")] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(f"/api/workspaces/{wid}/sources", json={
        "connector": "kb_dir", "kb_id": "kb_ok", "node_id": "sp2"})
    assert r.status_code == 404
    env.holder["user"] = _user("u-alice")
    r = await env.client.delete(f"/api/workspaces/{wid}/sources/sp")
    assert r.status_code == 200
    r = await env.client.delete(f"/api/workspaces/{wid}/sources/sp")
    assert r.status_code == 404


async def test_source_excel_upload_201(env):
    wid = await _create_ws(env)
    data = _xlsx_bytes(env.tmp_path / "A.xlsx", ["MOP", "L1"],
                       [("NC-1", "数据业务"), ("NC-2", "网络维护")])
    r = await env.client.post(
        f"/api/workspaces/{wid}/sources/excel",
        files={"file": ("A.xlsx", data, "application/octet-stream")},
        data={"node_id": "sp"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["connector"] == "excel"
    assert body["frozen"]["row_count"] == 2
    assert body["batch_no"] == 2
    # 冻结整份数据集（执行不重读文件）
    row = env.db.sources[(wid, "sp")]
    assert row["input_manifest"]["dataset"]["rows"][0]["MOP"] == "NC-1"


async def test_source_excel_bad_file_422_and_too_large_413(env, monkeypatch):
    import knowledge_mining.mining.kb.routes.workspaces as ws_routes

    wid = await _create_ws(env)
    r = await env.client.post(
        f"/api/workspaces/{wid}/sources/excel",
        files={"file": ("bad.xlsx", b"not an excel", "application/octet-stream")},
        data={"node_id": "sp"},
    )
    assert r.status_code == 422
    assert "invalid_xlsx" in r.text

    data = _xlsx_bytes(env.tmp_path / "B.xlsx", ["MOP"], [("NC-1",)])
    monkeypatch.setattr(ws_routes, "_UPLOAD_BODY_BUDGET", 10)
    r = await env.client.post(
        f"/api/workspaces/{wid}/sources/excel",
        files={"file": ("B.xlsx", data, "application/octet-stream")},
        data={"node_id": "sp"},
    )
    assert r.status_code == 413


async def test_source_excel_wrong_extension_422(env):
    """Task 9 补口：非 .xlsx 后缀（.csv）→ 422 unsupported_file_type。"""
    wid = await _create_ws(env)
    r = await env.client.post(
        f"/api/workspaces/{wid}/sources/excel",
        files={"file": ("库存.csv", b"model,power\nP1,3000", "text/csv")},
        data={"node_id": "sp"},
    )
    assert r.status_code == 422
    assert "unsupported_file_type" in r.text


# ----------------------------------------------------------------- 运行面

async def test_run_start_202_subgraph_422_inflight_409_cancel(env):
    wid = await _create_ws(env)
    r = await env.client.post(f"/api/workspaces/{wid}/runs", json={
        "scope": "workspace"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert run_id.startswith("wsr_")

    r = await env.client.post(f"/api/workspaces/{wid}/runs", json={
        "scope": "subgraph", "target_node_id": "a"})
    assert r.status_code == 422  # 与执行器同步：subgraph 词表预留未接线

    r = await env.client.post(f"/api/workspaces/{wid}/runs", json={
        "scope": "workspace"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_in_progress"

    r = await env.client.get(f"/api/workspaces/{wid}/runs")
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["runs"]] == [run_id]
    assert "graph_snapshot_json" not in r.json()["runs"][0]  # 剥大快照

    r = await env.client.get(f"/api/workspaces/{wid}/runs/{run_id}")
    assert r.status_code == 200 and r.json()["run"]["id"] == run_id

    r = await env.client.post(f"/api/workspaces/{wid}/runs/{run_id}/cancel")
    assert r.status_code == 200 and r.json()["cancelled"] is True
    # 取消幂等：已终态 → cancelled=False 不抛
    r = await env.client.post(f"/api/workspaces/{wid}/runs/{run_id}/cancel")
    assert r.status_code == 200 and r.json()["cancelled"] is False

    # 收紧（2026-11-17）：绑定成员启动/取消 → 404 反探测；取消他人工作区的 run → 404
    env.db.members[(wid, "u-viewer")] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(f"/api/workspaces/{wid}/runs", json={
        "scope": "workspace"})
    assert r.status_code == 404
    env.holder["user"] = _user("u-alice")
    r = await env.client.post(f"/api/workspaces/{wid}/runs/wsr_missing/cancel")
    assert r.status_code == 404


async def test_run_status_foreign_run_404(env):
    """run 不属于本工作区（或不存在）→ 404 反探测。"""
    wid = await _create_ws(env)
    r = await env.client.get(f"/api/workspaces/{wid}/runs/wsr_missing")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "run_not_found"


async def test_outputs_current_read_roles(env):
    """收紧（2026-11-17）：管理员读输出面 200；绑定成员（原 read+ 面已
    退役）与陌生人 → 404。"""
    wid = await _create_ws(env)
    env.ex.rdb.runs["wsr_x"] = {
        "id": "wsr_x", "workspace_id": wid, "status": "completed"}
    env.db.members[(wid, "u-viewer")] = "viewer"
    r = await env.client.get(f"/api/workspaces/{wid}/outputs/current")
    assert r.status_code == 200 and r.json()["outputs"] == {}
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/workspaces/{wid}/outputs/current")
    assert r.status_code == 404
    env.holder["user"] = _user("u-stranger")
    r = await env.client.get(f"/api/workspaces/{wid}/outputs/current")
    assert r.status_code == 404


async def test_trial_design_role_and_port_mismatch(env):
    wid = await _create_ws(env)
    env.ex.trial_results["f"] = {
        "outputs": {"main": {"kind": "table", "cols": ["a"], "rows": []}},
        "quality": {}, "trial": True, "input_rows": 3}
    # 空 outputs 边缘（Task 9 补口）：无声明端口 → 404 output_not_found
    env.ex.trial_results["e"] = {"quality": {}, "trial": True,
                                 "input_rows": 0}
    r = await env.client.post(
        f"/api/workspaces/{wid}/outputs/e/main/trial")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "output_not_found"
    r = await env.client.post(
        f"/api/workspaces/{wid}/outputs/f/main/trial")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trial"] is True and body["port_id"] == "main"
    # 声明端口不存在 → 404 output_not_found（与导出门禁同码族）
    r = await env.client.post(
        f"/api/workspaces/{wid}/outputs/f/nope/trial")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "output_not_found"
    # 收紧（2026-11-17）：绑定成员试跑 → 404 反探测；节点不存在 → 422
    env.db.members[(wid, "u-viewer")] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(
        f"/api/workspaces/{wid}/outputs/f/main/trial")
    assert r.status_code == 404
    env.holder["user"] = _user("u-alice")
    r = await env.client.post(
        f"/api/workspaces/{wid}/outputs/ghost/main/trial")
    assert r.status_code == 422


# ----------------------------------------------------------------- 导出


async def test_export_route_status_codes(env):
    """POST /exports：201 首发成功 / 200 幂等重放（duplicate）/ 409
    export_in_progress / 422 export_all_failed / items 空数组 422（pydantic）。
    服务编排真库行为归 test_workspace_export.py；此处 stub 打路由翻译面。"""
    wid = await _create_ws(env)
    calls: list[dict] = []

    async def fake_export(workspace_id, user, *, request_id, items):
        calls.append({"wid": workspace_id, "request_id": request_id,
                      "items": items})
        if request_id == "r-dup":
            return {"exports": [{"node_id": "s", "port_id": "main",
                                 "artifact_id": "art_1", "name": "功率表",
                                 "status": "succeeded"}],
                    "duplicate": True}
        if request_id == "r-busy":
            raise WorkspaceConflictError("export in progress",
                                         code="export_in_progress")
        if request_id == "r-allbad":
            raise WorkspaceValidationError(
                "export_all_failed: stale_output,empty_output",
                code="export_all_failed")
        return {"exports": [{"node_id": "s", "port_id": "main",
                             "artifact_id": "art_2", "name": "功率表",
                             "status": "succeeded"}]}

    env.svc.export_drafts = fake_export
    body = {"request_id": "r-ok", "items": [
        {"node_id": "s", "port_id": "main", "artifact_name": "功率表",
         "owner_user_id": "u-bob"}]}

    r = await env.client.post(f"/api/workspaces/{wid}/exports", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["exports"][0]["artifact_id"] == "art_2"
    assert calls[0]["wid"] == wid
    assert calls[0]["items"][0]["owner_user_id"] == "u-bob"

    r = await env.client.post(f"/api/workspaces/{wid}/exports", json={
        **body, "request_id": "r-dup"})
    assert r.status_code == 200 and r.json()["duplicate"] is True

    r = await env.client.post(f"/api/workspaces/{wid}/exports", json={
        **body, "request_id": "r-busy"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "export_in_progress"

    r = await env.client.post(f"/api/workspaces/{wid}/exports", json={
        **body, "request_id": "r-allbad"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "export_all_failed"

    # items 空数组 / artifact_name 超长 → pydantic 422
    r = await env.client.post(f"/api/workspaces/{wid}/exports", json={
        "request_id": "r-empty", "items": []})
    assert r.status_code == 422
    r = await env.client.post(f"/api/workspaces/{wid}/exports", json={
        "request_id": "r-long", "items": [
            {"node_id": "s", "port_id": "main",
             "artifact_name": "x" * 201}]})
    assert r.status_code == 422


# ----------------------------------------------------------------- 成员

async def test_members_last_owner_guard_and_admin_governance(env):
    wid = await _create_ws(env)

    r = await env.client.get(f"/api/workspaces/{wid}/members")
    assert r.status_code == 200
    assert r.json()["members"][0]["user_id"] == "u-alice"

    r = await env.client.post(f"/api/workspaces/{wid}/members", json={
        "user_id": "u-viewer", "role": "viewer"})
    assert r.status_code == 201, r.text
    r = await env.client.post(f"/api/workspaces/{wid}/members", json={
        "user_id": "u-ghost", "role": "viewer"})
    assert r.status_code == 422  # 目标用户不在 kb_users

    # 收紧（2026-11-17）：绑定成员治理成员 → 404 反探测（与不存在同码）
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(f"/api/workspaces/{wid}/members", json={
        "user_id": "u-bob", "role": "viewer"})
    assert r.status_code == 404

    # owner 改角色；成员行不存在 → 404
    env.holder["user"] = _user("u-alice")
    r = await env.client.patch(
        f"/api/workspaces/{wid}/members/u-viewer", json={"role": "editor"})
    assert r.status_code == 200
    r = await env.client.patch(
        f"/api/workspaces/{wid}/members/u-ghost", json={"role": "viewer"})
    assert r.status_code == 404
    r = await env.client.delete(f"/api/workspaces/{wid}/members/u-ghost")
    assert r.status_code == 404

    # 最后一个 owner：降级/移除 → 422（last-owner 保护）
    r = await env.client.patch(
        f"/api/workspaces/{wid}/members/u-alice", json={"role": "editor"})
    assert r.status_code == 422
    r = await env.client.delete(f"/api/workspaces/{wid}/members/u-alice")
    assert r.status_code == 422

    # domain admin 治理（收紧后能进门的都是管理员，天然有权）
    env.holder["user"] = _user("u-domadm")
    r = await env.client.delete(f"/api/workspaces/{wid}/members/u-viewer")
    assert r.status_code == 200


# --------------------------------------------------- 收紧钉测（2026-11-17）


async def test_bound_member_every_write_endpoint_404(env):
    """收紧钉测：绑定普通成员（非管理员，挂 owner 级成员行也不放行）对既有
    工作区**任何写端点**=404，且与不存在的 wid 响应同码同面（反探测信封
    完全一致——status+code 逐一比对，不向无权者泄露存在性）。"""
    wid = await _create_ws(env)
    # 成员行给到最高角色 owner —— 收紧后成员行不再参与判定，仍应 404
    env.db.members[(wid, "u-viewer")] = "owner"
    env.holder["user"] = _user("u-viewer")

    graph = {"graph": {"nodes": [_node("sp", "source")]},
             "expected_revision": 0}
    export_body = {"request_id": "r-pin", "items": [
        {"node_id": "s", "port_id": "main", "artifact_name": "钉测"}]}
    xlsx = _xlsx_bytes(env.tmp_path / "pin.xlsx", ["MOP"], [("NC-1",)])
    writes: list[tuple[str, str, dict | None]] = [
        ("patch", f"/api/workspaces/{wid}", {"name": "越权改名"}),
        ("delete", f"/api/workspaces/{wid}", None),
        ("post", f"/api/workspaces/{wid}/members",
         {"user_id": "u-bob", "role": "viewer"}),
        ("patch", f"/api/workspaces/{wid}/members/u-bob", {"role": "editor"}),
        ("delete", f"/api/workspaces/{wid}/members/u-bob", None),
        ("put", f"/api/workspaces/{wid}/graph", graph),
        ("post", f"/api/workspaces/{wid}/sources",
         {"connector": "kb_dir", "kb_id": "kb_ok", "node_id": "sp"}),
        ("delete", f"/api/workspaces/{wid}/sources/sp", None),
        ("post", f"/api/workspaces/{wid}/runs", {"scope": "workspace"}),
        ("post", f"/api/workspaces/{wid}/runs/wsr_x/cancel", None),
        ("post", f"/api/workspaces/{wid}/outputs/f/main/trial", None),
        ("post", f"/api/workspaces/{wid}/exports", export_body),
    ]
    excel = ("post", f"/api/workspaces/{wid}/sources/excel",
             {"files": {"file": ("pin.xlsx", xlsx,
                                 "application/octet-stream")},
              "data": {"node_id": "sp"}})

    async def _call(entry):
        method, path, payload = entry
        if path.endswith("/sources/excel"):
            return await env.client.post(path, **payload)
        if payload is None:
            return await getattr(env.client, method)(path)
        return await getattr(env.client, method)(path, json=payload)

    for entry in writes + [excel]:
        r = await _call(entry)
        assert r.status_code == 404, (entry[1], r.text)
        assert r.json()["detail"]["code"] == "workspace_not_found", entry[1]
        # 反探测同码：与不存在的 wid 响应面完全一致（wid 段替换）
        missing = entry[1].replace(f"/{wid}", "/wsp_missing")
        if entry is excel:
            r2 = await env.client.post(missing, **entry[2])
        else:
            r2 = await _call((entry[0], missing, entry[2]))
        assert r2.status_code == r.status_code, missing
        assert r2.json()["detail"]["code"] == \
            r.json()["detail"]["code"], missing
