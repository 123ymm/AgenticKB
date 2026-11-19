# -*- coding: utf-8 -*-
"""KB 知识制品工作台登记层（67号工作台 MVP Task 2）—— kb_workspaces /
kb_workspace_members / kb_workspace_sources 仓储。

风格契约与 db_artifacts.py 一致：TEXT id、参数化 SQL、dict_row、多语句显式
``conn.transaction()``；时间戳 TEXT ISO（同 kb/db.py _utcnow 口径，021 族
建表注释）。与 db_artifacts 的差异：方法首个可选参数 conn 允许调用方注入
连接（服务层组合事务复用），缺省自开 pool 连接——一个方法一个事务的边界
不变（注入连接时嵌套 transaction 退化为 savepoint，行为等价）。

机制权威 = docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 2
（表 DDL = 迁移 012 / 启动链 021）：
- put_graph 锁内乐观锁（模式照 db_wiki_toc.upsert_draft_toc：纯形态校验
  在锁外先行，FOR UPDATE 后比对 graph_revision 整数令牌）。校验四件套：
  节点 id 唯一、inputs 的 node:port 引用存在、source 节点 inputs 必空、
  Kahn 环检测（inputs 当边）。
- 服务端 diff 标志（两标志分工见 67号 §5.2：配置已改看节点态，上游已变
  的输出级 stale_since_run 由执行层落 outputs 行——本模块不碰）：
  config/inputs 变化或新增的节点 pending=true；其严格后代（沿 inputs 反向
  可达）stale=true；两标志随 graph_json 原子写入。
- **继承语义**（Task 2 决策）：未变节点继承旧图标志——标志只由执行清除
  （Task 6 executor 成功后 jsonb_set 定点清除），无关 PUT 不得丢失「未执
  行的已改」状态；变更节点 pending 压过旧 stale（自身重跑覆盖上游语义）。
- upsert_source 同事务推进 kb_workspaces.batch_no+1（WB-07：重新 PUT
  source = 新资料批次，"用新资料重跑"即重新接入触发重读）。
"""
from __future__ import annotations

import json
import math
import secrets
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

__all__ = [
    "WorkspaceConflictError",
    "WorkspaceDB",
    "WorkspaceError",
    "WorkspaceNotFoundError",
    "WorkspaceValidationError",
]

#: 步骤流合法算子（封闭词表，新增算子=改此处 + Task 4 注册表）。
_VALID_OPS = ("source", "select", "filter", "join", "split_sections",
              "llm_extract")

#: 图节点数上限（put_graph 护栏，2026-11-16 质量收口）：模板驱动步骤流的
#: 画布规模边界——超限整图拒绝，防巨型图拖垮 diff / Kahn 拓扑 / 执行器。
MAX_GRAPH_NODES = 200

_VALID_MEMBER_ROLES = ("owner", "editor", "viewer")

_VALID_CONNECTORS = ("kb_dir", "excel")

#: 工作区行完整列清单（RETURNING/SELECT 共用，排布同 021 DDL）。
_WS_COLUMNS = """id, domain_id, name, description, owner_user_id, status,
                 graph_json, graph_revision, batch_no, actor_kind, actor_id,
                 created_by, created_at, updated_at"""


def _new_workspace_id() -> str:
    """wsp_ + 16hex（021 DDL 注释契约；secrets 满足不可预测性）。"""
    return f"wsp_{secrets.token_hex(8)}"


def _new_source_id() -> str:
    return f"wss_{secrets.token_hex(8)}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class WorkspaceError(Exception):
    """工作台仓储层领域异常基类（service 层翻译 HTTP，对齐 WikiError 风格：
    携带机器可读 code，供 Task 8 错误映射复用）。"""

    code = "workspace_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or type(self).code)
        self.code = code or type(self).code


class WorkspaceConflictError(WorkspaceError):
    """乐观锁失配（put_graph 的 graph_revision / patch 的 updated_at 令牌过期）。"""

    code = "workspace_conflict"


class WorkspaceValidationError(WorkspaceError):
    """形态/业务校验失败（图环、引用悬空、非法角色、最后 owner 保护等）。"""

    code = "workspace_validation"


class WorkspaceNotFoundError(WorkspaceError):
    """目标工作区/成员行不存在（service 反探测 404 的库层信号）。"""

    code = "workspace_not_found"


# ------------------------------------------------------------ 图校验与 diff


def _validate_graph(graph: Any) -> list[dict[str, Any]]:
    """图形态校验 + 归一（畸形 → WorkspaceValidationError）：

    nodes 每节点归一为 {id, op, name, config, inputs, ports, x, y}（x/y 可
    缺省 0；客户端携带的 pending/stale 被丢弃——标志服务端权威，由 diff 写
    回）。规则：id 非空唯一且不含 ':'（node:port 引用无歧义）；op ∈ 合法
    词表；ports 非空且 port id 节点内唯一；source 节点 inputs 必空；跨节点
    inputs 引用的节点与端口必须存在；节点数 ≤ MAX_GRAPH_NODES；Kahn 环检测
    （inputs 当边）。单节点字段归一到 _validate_node，跨节点引用见
    _validate_refs（2026-11-16 质量收口切分）。"""
    if not isinstance(graph, dict):
        raise WorkspaceValidationError(
            "graph must be an object with a 'nodes' list")
    nodes_in = graph.get("nodes")
    if not isinstance(nodes_in, list):
        raise WorkspaceValidationError("graph.nodes must be a list")
    if len(nodes_in) > MAX_GRAPH_NODES:
        raise WorkspaceValidationError(
            f"graph.nodes exceeds limit {MAX_GRAPH_NODES}: {len(nodes_in)}")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(nodes_in):
        normalized.append(_validate_node(raw, i, seen_ids))
    _validate_refs(normalized)
    _ensure_acyclic(normalized)
    return normalized


def _validate_node(
    raw: Any, index: int, seen_ids: set[str],
) -> dict[str, Any]:
    """单节点字段校验 + 归一（跨节点引用/环由 _validate_graph 后续段负责；
    seen_ids 就地累积——节点 id 全图唯一在逐节点段内即可判定）。"""
    if not isinstance(raw, dict):
        raise WorkspaceValidationError(f"graph.nodes[{index}] must be an object")
    nid = raw.get("id")
    if not isinstance(nid, str) or not nid:
        raise WorkspaceValidationError(
            f"graph.nodes[{index}].id must be a non-empty string")
    if ":" in nid:
        raise WorkspaceValidationError(
            f"node id must not contain ':' (node:port refs would be"
            f" ambiguous): {nid}")
    if nid in seen_ids:
        raise WorkspaceValidationError(f"duplicate node id: {nid}")
    seen_ids.add(nid)
    op = raw.get("op")
    if op not in _VALID_OPS:
        raise WorkspaceValidationError(
            f"graph.nodes[{index}].op must be one of {list(_VALID_OPS)}:"
            f" {op!r}")
    name = raw.get("name", "")
    if not isinstance(name, str):
        raise WorkspaceValidationError(f"node {nid}: name must be a string")
    config = raw.get("config", {})
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise WorkspaceValidationError(
            f"node {nid}: config must be an object")
    inputs = _validate_inputs(nid, raw.get("inputs", []))
    ports = _validate_ports(nid, raw.get("ports"))
    if op == "source" and inputs:
        raise WorkspaceValidationError(
            f"source node {nid} must not have inputs")
    _validate_coords(nid, raw)
    return {
        "id": nid, "op": op, "name": name, "config": config,
        "inputs": inputs, "ports": ports,
        "x": raw.get("x", 0), "y": raw.get("y", 0),
    }


def _validate_inputs(nid: str, inputs_in: Any) -> list[str]:
    """inputs 归一：非空列表，每项形如 'node:port'（两端皆非空）。"""
    if inputs_in is None:
        inputs_in = []
    if not isinstance(inputs_in, list):
        raise WorkspaceValidationError(f"node {nid}: inputs must be a list")
    inputs: list[str] = []
    for ref in inputs_in:
        if not isinstance(ref, str) or ":" not in ref:
            raise WorkspaceValidationError(
                f"node {nid}: input must be 'node:port': {ref!r}")
        producer, _, port = ref.partition(":")
        if not producer or not port:
            raise WorkspaceValidationError(
                f"node {nid}: input must be 'node:port': {ref}")
        inputs.append(ref)
    return inputs


def _validate_ports(nid: str, ports_in: Any) -> list[dict[str, Any]]:
    """ports 归一：非空列表，port id 节点内唯一，name/kind 为字符串。"""
    if not isinstance(ports_in, list) or not ports_in:
        raise WorkspaceValidationError(
            f"node {nid}: ports must be a non-empty list")
    ports: list[dict[str, Any]] = []
    port_ids: set[str] = set()
    for p in ports_in:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) \
                or not p["id"]:
            raise WorkspaceValidationError(
                f"node {nid}: each port needs a non-empty string id")
        pid = p["id"]
        if pid in port_ids:
            raise WorkspaceValidationError(
                f"node {nid}: duplicate port id: {pid}")
        port_ids.add(pid)
        pname = p.get("name", "")
        pkind = p.get("kind", "table")
        if not isinstance(pname, str) or not isinstance(pkind, str):
            raise WorkspaceValidationError(
                f"node {nid}: port name/kind must be strings")
        ports.append({"id": pid, "name": pname, "kind": pkind})
    return ports


def _validate_coords(nid: str, raw: dict[str, Any]) -> None:
    """x/y 画布坐标校验：数值且**有限**——json.dumps 会把 NaN/inf 原样带出
    （PG jsonb 拒收 NaN），必须在 Python 侧拦成 Validation 而非裸
    InvalidTextRepresentation；超大 int（float 溢出）同判非有限。"""
    for label in ("x", "y"):
        value = raw.get(label, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkspaceValidationError(
                f"node {nid}: {label} must be a number")
        try:
            finite = math.isfinite(value)
        except OverflowError:  # 超出 float 表示域的巨型 int
            finite = False
        if not finite:
            raise WorkspaceValidationError(
                f"node {nid}: {label} must be a finite number"
                f" (NaN/inf rejected): {value!r}")


def _validate_refs(nodes: list[dict[str, Any]]) -> None:
    """跨节点引用：inputs 的 producer 必须存在且含被引端口。"""
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        for ref in n["inputs"]:
            producer, port = ref.split(":", 1)
            target = by_id.get(producer)
            if target is None:
                raise WorkspaceValidationError(
                    f"node {n['id']} input references missing node:"
                    f" {producer}")
            if port not in {p["id"] for p in target["ports"]}:
                raise WorkspaceValidationError(
                    f"node {n['id']} input references missing port: {ref}")


def _ensure_acyclic(nodes: list[dict[str, Any]]) -> None:
    """Kahn 拓扑（inputs 当边：producer→consumer），有环 → Validation。"""
    indegree = {n["id"]: 0 for n in nodes}
    consumers: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        for ref in n["inputs"]:
            producer = ref.split(":", 1)[0]
            consumers[producer].append(n["id"])
            indegree[n["id"]] += 1
    queue = deque(nid for nid, deg in indegree.items() if deg == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for consumer in consumers[current]:
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                queue.append(consumer)
    if visited != len(nodes):
        raise WorkspaceValidationError("graph contains a cycle")


def _apply_run_flags(
    new_nodes: list[dict[str, Any]], old_graph_json: Any,
) -> dict[str, Any]:
    """服务端 diff：旧图 → pending/stale 标志，写入节点态（就地）并返回
    {"nodes": [...]}。

    规则（Task 2 决策，继承语义见模块 docstring）：
    - changed = 新节点（旧图没有）或 config/inputs 与旧图不同 → pending；
    - stale = changed 集合沿 inputs 反向可达的严格后代（不含 changed 自身
      ——pending 压过旧 stale：自身重跑覆盖上游传播）；
    - 未变节点继承旧图标志（执行未清除前不得丢失）；
    - 对比键只有 config/inputs（挪 x/y、改 name 不触发 pending——纯排版）。
    窄防御：旧 graph_json 畸形（绕过写路径的脏数据）视作空图（全部按新节点
    pending），与 resolve_toc_payload 的降级口径同向。"""
    old: dict[str, dict[str, Any]] = {}
    if isinstance(old_graph_json, dict):
        old_nodes = old_graph_json.get("nodes")
        if isinstance(old_nodes, list):
            for n in old_nodes:
                if isinstance(n, dict) and isinstance(n.get("id"), str):
                    old[n["id"]] = n

    changed: set[str] = set()
    for n in new_nodes:
        prev = old.get(n["id"])
        if prev is None or prev.get("config") != n["config"] \
                or prev.get("inputs") != n["inputs"]:
            changed.add(n["id"])

    # 反向可达：producer → 其消费者（沿 inputs 边正向传播污染）
    consumers: dict[str, set[str]] = {}
    for n in new_nodes:
        for ref in n["inputs"]:
            producer = ref.split(":", 1)[0]
            consumers.setdefault(producer, set()).add(n["id"])
    stale: set[str] = set()
    seen = set(changed)
    queue = deque(changed)
    while queue:
        for consumer in consumers.get(queue.popleft(), ()):
            if consumer not in seen:
                seen.add(consumer)
                stale.add(consumer)
                queue.append(consumer)
    stale -= changed  # 严格后代

    for n in new_nodes:
        prev = old.get(n["id"])
        is_changed = n["id"] in changed
        n["pending"] = is_changed or bool(prev and prev.get("pending"))
        n["stale"] = n["id"] in stale or (
            not is_changed and bool(prev and prev.get("stale")))
    return {"nodes": new_nodes}


def _removed_node_ids(
    new_nodes: list[dict[str, Any]], old_graph_json: Any,
) -> list[str]:
    """diff 检出**被删节点**（旧图有、新图无；有序输出便于日志/断言稳定）。

    窄防御与 _apply_run_flags 同款：旧 graph_json 畸形（绕过写路径的脏
    数据）视作空图——无被删节点，不做清理。"""
    old_ids: set[str] = set()
    if isinstance(old_graph_json, dict):
        old_nodes = old_graph_json.get("nodes")
        if isinstance(old_nodes, list):
            for n in old_nodes:
                if isinstance(n, dict) and isinstance(n.get("id"), str):
                    old_ids.add(n["id"])
    return sorted(old_ids - {n["id"] for n in new_nodes})


class WorkspaceDB:
    """kb_workspaces / kb_workspace_members / kb_workspace_sources 仓储。

    构造入参为 row_factory=dict_row 的 psycopg AsyncConnectionPool。run/
    output/binding/export 表归 Task 3 的 db_workspace_runs。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def _use_conn(self, conn: Any | None) -> AsyncIterator[Any]:
        """conn 注入则复用（服务层组合事务），否则自开 pool 连接。"""
        if conn is not None:
            yield conn
        else:
            async with self._pool.connection() as own:
                yield own

    # ------------------------------------------------------------ 登记 CRUD

    async def create_workspace(
        self, conn: Any | None = None, *, domain_id: str, name: str,
        description: str = "", owner_user_id: str, created_by: str,
        actor_kind: str = "user", actor_id: str = "",
    ) -> dict[str, Any]:
        """单事务：INSERT kb_workspaces（graph_revision=0、batch_no=1、
        status='active'、空图）+ INSERT kb_workspace_members(owner 行)，
        返回完整工作区行。"""
        if actor_kind not in ("user", "mcp_key"):
            raise WorkspaceValidationError(
                f"actor_kind must be 'user' or 'mcp_key': {actor_kind!r}")
        if not isinstance(name, str) or not name.strip():
            raise WorkspaceValidationError("name must be a non-empty string")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                now = _utcnow()
                cur = await c.execute(
                    f"""INSERT INTO kb_workspaces
                        (id, domain_id, name, description, owner_user_id,
                         status, graph_json, graph_revision, batch_no,
                         actor_kind, actor_id, created_by, created_at,
                         updated_at)
                        VALUES (%s, %s, %s, %s, %s, 'active', %s::jsonb, 0,
                                1, %s, %s, %s, %s, %s)
                        RETURNING {_WS_COLUMNS}""",
                    (_new_workspace_id(), domain_id, name, description,
                     owner_user_id, _json({"nodes": []}), actor_kind,
                     actor_id, created_by, now, now),
                )
                row = dict(await cur.fetchone())  # type: ignore[arg-type]
                await c.execute(
                    """INSERT INTO kb_workspace_members
                       (workspace_id, user_id, role, created_at)
                       VALUES (%s, %s, 'owner', %s)""",
                    (row["id"], owner_user_id, now),
                )
                return row

    async def get_workspace(
        self, conn: Any | None = None, *, workspace_id: str,
    ) -> dict[str, Any] | None:
        """详情读：LEFT JOIN kb_users 透出 owner_username（用户测试反馈
        修复1——照 list_members 的 JOIN 手法；owner 用户行不存在 → None，
        回落显示归前端）。"""
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                """SELECT w.id, w.domain_id, w.name, w.description,
                          w.owner_user_id, u.username AS owner_username,
                          w.status, w.graph_json, w.graph_revision,
                          w.batch_no, w.actor_kind, w.actor_id, w.created_by,
                          w.created_at, w.updated_at
                   FROM kb_workspaces w
                   LEFT JOIN kb_users u ON u.id = w.owner_user_id
                   WHERE w.id = %s""",
                (workspace_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_workspaces(
        self, conn: Any | None = None, *, domain_id: str, user_id: str,
        is_site_admin: bool = False, is_domain_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """工作区清单（含 active 与 archived，status 字段分列口径）。

        admin 两类（site/domain，调用方判定后传旗）= 本域全部；余人=owner
        行或成员行命中（我参与的）。created_at DESC。owner_username 经 LEFT
        JOIN kb_users 透出（用户测试反馈修复1；用户行不存在 → None）。"""
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                """SELECT w.id, w.domain_id, w.name, w.description,
                          w.owner_user_id, u.username AS owner_username,
                          w.status, w.graph_revision, w.batch_no, w.actor_kind,
                          w.actor_id, w.created_by, w.created_at, w.updated_at
                   FROM kb_workspaces w
                   LEFT JOIN kb_users u ON u.id = w.owner_user_id
                   WHERE w.domain_id = %(dom)s
                     AND (%(admin)s OR w.owner_user_id = %(u)s
                          OR EXISTS (SELECT 1 FROM kb_workspace_members m
                                     WHERE m.workspace_id = w.id
                                       AND m.user_id = %(u)s))
                   ORDER BY w.created_at DESC""",
                {"dom": domain_id, "u": user_id,
                 "admin": bool(is_site_admin or is_domain_admin)},
            )
            return [dict(r) for r in await cur.fetchall()]

    async def patch_workspace(
        self, conn: Any | None = None, *, workspace_id: str,
        name: str | None = None, description: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        """改 name/description（皆可选，None=不动）。expected_updated_at
        可选乐观锁（锁内全精度比对，失配 Conflict）；空补丁原样返回不推
        令牌。返回更新后的完整行。"""
        if name is not None and (not isinstance(name, str)
                                 or not name.strip()):
            raise WorkspaceValidationError("name must be a non-empty string")
        if description is not None and not isinstance(description, str):
            raise WorkspaceValidationError("description must be a string")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    f"""SELECT {_WS_COLUMNS} FROM kb_workspaces
                        WHERE id = %s FOR UPDATE""",
                    (workspace_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise WorkspaceNotFoundError(
                        f"workspace not found: {workspace_id}")
                if expected_updated_at is not None \
                        and row["updated_at"] != expected_updated_at:
                    raise WorkspaceConflictError(
                        "workspace updated concurrently"
                        f" (stale updated_at: {expected_updated_at})")
                if name is None and description is None:
                    return dict(row)
                sets: list[str] = []
                params: list[Any] = []
                if name is not None:
                    sets.append("name = %s")
                    params.append(name)
                if description is not None:
                    sets.append("description = %s")
                    params.append(description)
                sets.append("updated_at = %s")
                params.append(_utcnow())
                params.append(workspace_id)
                cur = await c.execute(
                    f"""UPDATE kb_workspaces SET {", ".join(sets)}
                        WHERE id = %s RETURNING {_WS_COLUMNS}""",
                    tuple(params),
                )
                return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def archive_workspace(
        self, conn: Any | None = None, *, workspace_id: str,
    ) -> dict[str, Any]:
        """软删（status='archived'）幂等：重复归档不报错也不推 updated_at；
        不存在 → NotFound。"""
        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    f"""UPDATE kb_workspaces
                        SET status = 'archived', updated_at = %s
                        WHERE id = %s AND status = 'active'
                        RETURNING {_WS_COLUMNS}""",
                    (_utcnow(), workspace_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    return dict(row)
                cur = await c.execute(
                    f"SELECT {_WS_COLUMNS} FROM kb_workspaces WHERE id = %s",
                    (workspace_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise WorkspaceNotFoundError(
                        f"workspace not found: {workspace_id}")
                return dict(row)  # 已归档：幂等返回现态

    async def purge_workspace(
        self, conn: Any | None = None, *, workspace_id: str,
    ) -> int:
        """真删（用户反馈问题1——两步门禁，照制品「先停用再删除」）：仅
        status='archived' 可执行（active → WorkspaceValidationError 提示先
        归档；不存在 → NotFound）。单事务：先手清无 FK 的
        kb_workspace_output_bindings / kb_workspace_exports 两表，再 DELETE
        kb_workspaces 主行——members/sources/runs 靠既有 FK ON DELETE
        CASCADE 连带清理，run_outputs 随 runs 级联（run_id FK）。返回删除
        主表行数（调用门禁在前，恒 1）。"""
        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    "SELECT status FROM kb_workspaces WHERE id = %s"
                    " FOR UPDATE",
                    (workspace_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise WorkspaceNotFoundError(
                        f"workspace not found: {workspace_id}")
                if row["status"] != "archived":
                    raise WorkspaceValidationError(
                        f"workspace is active, 先归档再删除: {workspace_id}")
                await c.execute(
                    "DELETE FROM kb_workspace_output_bindings"
                    " WHERE workspace_id = %s",
                    (workspace_id,),
                )
                await c.execute(
                    "DELETE FROM kb_workspace_exports WHERE workspace_id = %s",
                    (workspace_id,),
                )
                cur = await c.execute(
                    "DELETE FROM kb_workspaces WHERE id = %s", (workspace_id,))
                return int(cur.rowcount or 0)

    # ------------------------------------------------------------ 图（put_graph）

    async def put_graph(
        self, conn: Any | None = None, *, workspace_id: str, graph: Any,
        expected_revision: int,
    ) -> dict[str, Any]:
        """整图替换（锁内乐观锁，模式照 db_wiki_toc.upsert_draft_toc）：
        纯形态校验锁外先行 → FOR UPDATE → graph_revision 整数令牌比对（失配
        Conflict）→ 服务端 diff 写标志 → graph_revision+1 同事务推进。

        **删节点清理语义**（用户测试反馈修复2）：diff 检出被删节点（旧图有、
        新图无）→ 与图替换**同事务**清理其依赖行——①kb_workspace_sources
        （来源绑定）②kb_workspace_run_outputs 的**当前行**（is_current——
        历史行保留审计）③kb_workspace_output_bindings（导出去处）。下游
        仍在图且 inputs 引用被删节点时，锁外的 _validate_refs 先行 422
        （消息点名违规节点与缺失节点），图与依赖行整体不动——用户须先改接
        或删除下游。Conflict/NotFound 同理整体回滚不清理。

        返回 {"graph": 新图（节点含 pending/stale）, "graph_revision": 新
        版本号, "updated_at": 新令牌}。"""
        normalized = _validate_graph(graph)
        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    """SELECT graph_revision, graph_json FROM kb_workspaces
                       WHERE id = %s FOR UPDATE""",
                    (workspace_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise WorkspaceNotFoundError(
                        f"workspace not found: {workspace_id}")
                if row["graph_revision"] != expected_revision:
                    raise WorkspaceConflictError(
                        "graph_revision mismatch: expected"
                        f" {expected_revision}, current"
                        f" {row['graph_revision']}")
                new_graph = _apply_run_flags(normalized, row["graph_json"])
                removed = _removed_node_ids(normalized, row["graph_json"])
                if removed:
                    await c.execute(
                        """DELETE FROM kb_workspace_sources
                           WHERE workspace_id = %s AND node_id = ANY(%s)""",
                        (workspace_id, removed),
                    )
                    await c.execute(
                        """DELETE FROM kb_workspace_run_outputs
                           WHERE workspace_id = %s AND node_id = ANY(%s)
                             AND is_current""",
                        (workspace_id, removed),
                    )
                    await c.execute(
                        """DELETE FROM kb_workspace_output_bindings
                           WHERE workspace_id = %s AND node_id = ANY(%s)""",
                        (workspace_id, removed),
                    )
                cur = await c.execute(
                    """UPDATE kb_workspaces
                       SET graph_json = %s::jsonb,
                           graph_revision = graph_revision + 1,
                           updated_at = %s
                       WHERE id = %s
                       RETURNING graph_json, graph_revision, updated_at""",
                    (_json(new_graph), _utcnow(), workspace_id),
                )
                out = dict(await cur.fetchone())  # type: ignore[arg-type]
                return {
                    "graph": out["graph_json"],
                    "graph_revision": out["graph_revision"],
                    "updated_at": out["updated_at"],
                }

    async def clear_node_flags(
        self, workspace_id: str, node_id: str, expected_revision: int,
    ) -> bool:
        """节点 pending/stale 标志定点清除（67号 Task 6 决策 4②/5；Task 7
        质量收口 B 自 executor 的 jsonb_set 裸 SQL 下沉）：只动目标节点两键
        （jsonb_set create_if_missing=false——键不存在不动），WHERE
        graph_revision=expected_revision 守卫——运行中被并发 put_graph 推过
        版本则 UPDATE 落空返回 False（既不撞乐观锁，也绝不把新图的标志抹
        掉）。节点下标按当前图定位（revision 相等 ⟹ 当前图与 run 快照逐字
        节一致——一切图写路径都经 put_graph 推 revision，决策 5）；节点不
        存在 / revision 失配 → False。返回是否命中清除。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                """WITH target AS (
                       SELECT (ord - 1)::text AS node_idx
                       FROM kb_workspaces w,
                            LATERAL jsonb_array_elements(
                                w.graph_json -> 'nodes')
                                WITH ORDINALITY AS t(node_obj, ord)
                       WHERE w.id = %s
                         AND t.node_obj ->> 'id' = %s
                   )
                   UPDATE kb_workspaces w
                   SET graph_json = jsonb_set(
                           jsonb_set(w.graph_json,
                                     ARRAY['nodes', target.node_idx,
                                           'pending'],
                                     'false'::jsonb, false),
                           ARRAY['nodes', target.node_idx, 'stale'],
                           'false'::jsonb, false)
                   FROM target
                   WHERE w.id = %s AND w.graph_revision = %s""",
                (workspace_id, node_id, workspace_id, int(expected_revision)),
            )
            return bool(cur.rowcount or 0)

    # ------------------------------------------------------------ 成员

    async def list_members(
        self, conn: Any | None = None, *, workspace_id: str,
    ) -> list[dict[str, Any]]:
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                """SELECT m.user_id, m.role, u.username,
                          COALESCE(NULLIF(u.display_name, ''), u.username)
                              AS user_name
                   FROM kb_workspace_members m
                   LEFT JOIN kb_users u ON u.id = m.user_id
                   WHERE m.workspace_id = %s
                   ORDER BY m.role, u.username""",
                (workspace_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def member_role(
        self, conn: Any | None = None, *, workspace_id: str, user_id: str,
    ) -> str | None:
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                "SELECT role FROM kb_workspace_members"
                " WHERE workspace_id = %s AND user_id = %s",
                (workspace_id, user_id),
            )
            row = await cur.fetchone()
            return str(row["role"]) if row else None

    async def add_member(
        self, conn: Any | None = None, *, workspace_id: str, user_id: str,
        role: str,
    ) -> dict[str, Any]:
        """加成员（已存在即改角色——UPSERT 口径同 db_artifacts.upsert_member）。
        owner 降级保护同样生效（upsert 冲突覆盖也可能清掉最后一个 owner）。"""
        if role not in _VALID_MEMBER_ROLES:
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                await self._assert_workspace_exists(c, workspace_id)
                if role != "owner":
                    await self._guard_last_owner(c, workspace_id, user_id)
                cur = await c.execute(
                    """INSERT INTO kb_workspace_members
                       (workspace_id, user_id, role, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (workspace_id, user_id)
                       DO UPDATE SET role = EXCLUDED.role
                       RETURNING workspace_id, user_id, role, created_at""",
                    (workspace_id, user_id, role, _utcnow()),
                )
                return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def update_member_role(
        self, conn: Any | None = None, *, workspace_id: str, user_id: str,
        role: str,
    ) -> dict[str, Any]:
        """锁序：降级路径 owner 集有序锁**先行**、后锁目标行（LOW-1，见
        _guard_last_owner）；提升为 owner 不减计数，无需守卫锁。"""
        if role not in _VALID_MEMBER_ROLES:
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                if role != "owner":
                    await self._guard_last_owner(c, workspace_id, user_id)
                cur = await c.execute(
                    """SELECT role FROM kb_workspace_members
                       WHERE workspace_id = %s AND user_id = %s FOR UPDATE""",
                    (workspace_id, user_id),
                )
                if await cur.fetchone() is None:
                    raise WorkspaceNotFoundError(
                        f"member not found: {user_id}")
                cur = await c.execute(
                    """UPDATE kb_workspace_members SET role = %s
                       WHERE workspace_id = %s AND user_id = %s
                       RETURNING workspace_id, user_id, role, created_at""",
                    (role, workspace_id, user_id),
                )
                return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def remove_member(
        self, conn: Any | None = None, *, workspace_id: str, user_id: str,
    ) -> bool:
        """移除成员（无该成员行 → False）；最后一个 owner → Validation。

        锁序：owner 集有序锁**先行**、后锁目标行（LOW-1，见
        _guard_last_owner）。"""
        async with self._use_conn(conn) as c:
            async with c.transaction():
                await self._guard_last_owner(c, workspace_id, user_id)
                cur = await c.execute(
                    """SELECT 1 FROM kb_workspace_members
                       WHERE workspace_id = %s AND user_id = %s FOR UPDATE""",
                    (workspace_id, user_id),
                )
                if await cur.fetchone() is None:
                    return False
                await c.execute(
                    "DELETE FROM kb_workspace_members"
                    " WHERE workspace_id = %s AND user_id = %s",
                    (workspace_id, user_id),
                )
                return True

    async def _assert_workspace_exists(
        self, c: Any, workspace_id: str,
    ) -> None:
        cur = await c.execute(
            "SELECT 1 FROM kb_workspaces WHERE id = %s", (workspace_id,))
        if await cur.fetchone() is None:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace_id}")

    async def _guard_last_owner(
        self, c: Any, workspace_id: str, user_id: str,
    ) -> None:
        """owner 集有序锁 + 最后 owner 保护（移除/降级都不行——工作区永远
        至少留一个 owner）。

        锁序（规格审查 LOW-1）：**先**按 user_id 定序锁全部 owner 行、**再**
        由调用方锁目标成员行——所有减 owner 的事务在同一有序锁集上串行，
        两 owner 并发降级不再出现 T1/O1↔T2/O2 互等的死锁窗（败者收裸
        psycopg 死锁错的问题一并消失）。目标是否 owner 由**锁内**行集判定
        （不做另行无锁预读）：守卫时刻的全部 owner 行被持有到事务提交，
        并发提升/移除竞态下计数也不可能被清零。"""
        cur = await c.execute(
            """SELECT user_id FROM kb_workspace_members
               WHERE workspace_id = %s AND role = 'owner'
               ORDER BY user_id FOR UPDATE""",
            (workspace_id,),
        )
        owners = {str(r["user_id"]) for r in await cur.fetchall()}
        if user_id in owners and len(owners) <= 1:
            raise WorkspaceValidationError(
                "cannot remove or demote the last owner")

    # ------------------------------------------------------------ 来源

    async def upsert_source(
        self, conn: Any | None = None, *, workspace_id: str, node_id: str,
        connector: str, config: dict[str, Any],
        input_manifest: dict[str, Any], updated_by: str = "",
    ) -> dict[str, Any]:
        """来源 UPSERT（UNIQUE(workspace_id,node_id) 冲突即更新），**同事务
        推进 kb_workspaces.batch_no+1 与 updated_at**（WB-07：重新 PUT
        source = 新资料批次）。input_manifest 是冻结输入（kb_dir=文件清单/
        excel=整份数据集，Task 5 适配器产出）。表无 updated_by 列——
        updated_by 落 created_by（仅首插）。"""
        if connector not in _VALID_CONNECTORS:
            raise WorkspaceValidationError(f"invalid connector: {connector!r}")
        if not isinstance(node_id, str) or not node_id:
            raise WorkspaceValidationError(
                "node_id must be a non-empty string")
        if not isinstance(config, dict) \
                or not isinstance(input_manifest, dict):
            raise WorkspaceValidationError(
                "config/input_manifest must be objects")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                await self._assert_workspace_exists(c, workspace_id)
                now = _utcnow()
                cur = await c.execute(
                    """INSERT INTO kb_workspace_sources
                       (id, workspace_id, node_id, connector, config,
                        input_manifest, last_read_at, created_by, created_at,
                        updated_at)
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                               %s, %s)
                       ON CONFLICT (workspace_id, node_id) DO UPDATE SET
                           connector = EXCLUDED.connector,
                           config = EXCLUDED.config,
                           input_manifest = EXCLUDED.input_manifest,
                           last_read_at = EXCLUDED.last_read_at,
                           updated_at = EXCLUDED.updated_at
                       RETURNING id, workspace_id, node_id, connector, config,
                                 input_manifest, last_read_at, created_by,
                                 created_at, updated_at""",
                    (_new_source_id(), workspace_id, node_id, connector,
                     _json(config), _json(input_manifest), now, updated_by,
                     now, now),
                )
                row = dict(await cur.fetchone())  # type: ignore[arg-type]
                await c.execute(
                    """UPDATE kb_workspaces
                       SET batch_no = batch_no + 1, updated_at = %s
                       WHERE id = %s""",
                    (now, workspace_id),
                )
                return row

    async def list_sources(
        self, conn: Any | None = None, *, workspace_id: str,
    ) -> list[dict[str, Any]]:
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                """SELECT id, workspace_id, node_id, connector, config,
                          input_manifest, last_read_at, created_by,
                          created_at, updated_at
                   FROM kb_workspace_sources
                   WHERE workspace_id = %s
                   ORDER BY node_id""",
                (workspace_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def delete_source(
        self, conn: Any | None = None, *, workspace_id: str, node_id: str,
    ) -> bool:
        """删来源行（不存在 → False）。不推 batch_no（删除不引入新资料）。"""
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                "DELETE FROM kb_workspace_sources"
                " WHERE workspace_id = %s AND node_id = %s",
                (workspace_id, node_id),
            )
            return bool(cur.rowcount)
