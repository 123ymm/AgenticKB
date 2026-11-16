# -*- coding: utf-8 -*-
"""KB 知识制品工作台服务层（67号 MVP Task 8）——权限反探测 + CRUD/图/来源/
运行面/成员编排。

机制权威 = docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 8：

- **权限判定序照 artifact_service._assert_role**：site admin（user.site_role
  现值）→ domain admin（ArtifactDB.is_domain_admin 现查，谓词复用不自造）→
  非成员还须当前绑定在工作区域内（user_in_domain——撤销域绑定后残留成员行
  不放行，codex二审批号⑧同源）→ member_role 成员行 → owner_user_id 兜底；
  **其余一律 WorkspaceNotFoundError**（404 反探测：不存在与无权同响应）。
  成员但角色不足 → WorkspaceForbiddenError（403）。design=(owner,editor)；
  read=+viewer；成员治理与归档 owner-only（admin 旁路满足任意 allowed）。
- **来源 KB 权限拍板**：工作区有权但来源 KB 无权（含 KB 不存在/非 active
  ——两者同面，不向无权者泄露 KB 存在性）→ 403
  WorkspaceSourceForbiddenError（workspace_source_forbidden）。
- **错误翻译归路由**：本层把仓储/执行器异常重新打码——图 Conflict→
  graph_conflict（409）、图 Validation→graph_invalid（422）、run 在飞→
  run_in_progress（409）、patch 乐观锁→workspace_conflict（409）；其余
  WorkspaceError 族原样上抛（code 即路由表键）。
- **JSONB 落库契约（Task 5）**：manifest 进库前经 _jsonable 归一——行值含
  非 JSON 原生类型（Excel cast 失败兜底残留的 timedelta/Decimal 等）时以
  str 落库（json.dumps default=str 同源口径），db 层 _json 不带 default
  才不会 500。
- kb_dir 即读即冻（Task 5 决策 3）：read_kb_dir 与 upsert_source 共用一条
  pool 连接——权限判定与冻结落库同连接（upsert 自带事务；batch_no+1 与
  manifest 原子）。执行不重读 KB；重接入=重新 PUT source 触发重读。
"""
from __future__ import annotations

import json
from typing import Any

import anyio

from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.workspace_executor import WorkspaceExecutor
from knowledge_mining.mining.kb.workspace_sources import (
    WorkspacePermissionError,
    parse_excel,
    read_kb_dir,
)

__all__ = [
    "WorkspaceForbiddenError",
    "WorkspaceService",
    "WorkspaceSourceForbiddenError",
]

#: 运行态读（详情/图读/来源读/outputs/runs 查询）：成员任一角色。
ROLE_READ = ("owner", "editor", "viewer")
#: 设计态（改图/接来源/启动·取消 run/试跑/patch）：owner+editor。
ROLE_DESIGN = ("owner", "editor")
#: 成员治理与归档：仅 owner（site/domain admin 由旁路放行）。
ROLE_OWNER_ONLY = ("owner",)

#: admin 旁路角色（_assert_ws_role 合成，满足任意 allowed）。
_ADMIN_ROLES = ("admin", "domain_admin")

#: run scope 服务端词表（subgraph 与执行器同步拒绝——迁移 012 词表预留）。
_STARTABLE_SCOPES = ("workspace", "node")

_VALID_MEMBER_ROLES = ("owner", "editor", "viewer")


class WorkspaceForbiddenError(WorkspaceError):
    """403——工作区成员但角色不足（非成员走 404 反探测，见模块 docstring）。"""

    code = "workspace_forbidden"


class WorkspaceSourceForbiddenError(WorkspaceError):
    """403——工作区有权但来源 KB 无权/不可读（含不存在，同面反探测）。"""

    code = "workspace_source_forbidden"


def _uid(user: dict[str, Any]) -> str:
    return str(user["id"])


def _is_site_admin(user: dict[str, Any]) -> bool:
    return user.get("site_role") == "admin"


def _jsonable(obj: Any) -> Any:
    """JSON 原生类型归一（Task 5 契约的落库侧）：round-trip
    ``json.dumps(default=str)`` → 非 JSON 原生值（timedelta/Decimal/残余
    datetime 等）转字符串；dict/list/str/number/bool/None 原样保留（int 键
    归一为 str 键——jsonb 口径本就如此）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _frozen_summary(connector: str, manifest: Any) -> dict[str, Any]:
    """来源响应的冻结计数（manifest 本体不回传——excel 整表可达 20MB）。"""
    m = manifest if isinstance(manifest, dict) else {}
    if connector == "kb_dir":
        unparsed = m.get("unparsed_docs")
        return {"doc_count": int(m.get("doc_count") or 0),
                "unparsed_count": (len(unparsed)
                                   if isinstance(unparsed, list) else 0)}
    return {"row_count": int(m.get("row_count") or 0),
            "sheet_name": m.get("sheet_name")}


def _slim_manifest(manifest: Any) -> Any:
    """来源列表的 manifest 减重：剥 dataset（保留 frozen_at/计数/文档账）。"""
    if isinstance(manifest, dict):
        return {k: v for k, v in manifest.items() if k != "dataset"}
    return manifest


class WorkspaceService:
    """工作台服务层（行为规格与决策见模块 docstring）。

    构造入参 pool 为 row_factory=dict_row 的 psycopg AsyncConnectionPool；
    executor 可注入（缺省自建 WorkspaceExecutor(pool)）。域谓词复用
    ArtifactDB（is_domain_admin/user_in_domain/can_create_in_domain/
    get_user_status——谓词唯一真相源不自造，口径同 kb/db.py）。
    """

    def __init__(self, pool: Any, executor: Any = None) -> None:
        self._pool = pool
        self._db = WorkspaceDB(pool)
        self._adb = ArtifactDB(pool)
        self._rdb = WorkspaceRunDB(pool)
        self._executor = (executor if executor is not None
                          else WorkspaceExecutor(pool))

    # ------------------------------------------------------------ 权限核心

    async def _assert_ws_role(
        self, workspace: dict[str, Any], user: dict[str, Any],
        *allowed: str,
    ) -> tuple[dict[str, Any], str]:
        """返回 (workspace, role)。判定序见模块 docstring；workspace 行由
        调用方（_require）先行取好——本方法不做行读取。"""
        if _is_site_admin(user):
            return workspace, "admin"
        uid = _uid(user)
        domain_id = str(workspace["domain_id"])
        if await self._adb.is_domain_admin(uid, domain_id):
            return workspace, "domain_admin"
        if not await self._adb.user_in_domain(uid, domain_id):
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace['id']}")
        role = await self._db.member_role(
            workspace_id=str(workspace["id"]), user_id=uid)
        if role is None and workspace.get("owner_user_id") == uid:
            role = "owner"  # owner 成员行缺失的兜底（创建路径必有行）
        if role is None:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace['id']}")
        if allowed and role not in allowed and role not in _ADMIN_ROLES:
            raise WorkspaceForbiddenError(
                f"role {role} not allowed (requires one of"
                f" {list(allowed)})")
        return workspace, role

    async def _require(
        self, user: dict[str, Any], workspace_id: str, *allowed: str,
    ) -> tuple[dict[str, Any], str]:
        """取行 + 判权一步（绝大多数端点的入口）。"""
        ws = await self._db.get_workspace(workspace_id=workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        return await self._assert_ws_role(ws, user, *allowed)

    # ------------------------------------------------------------ CRUD

    async def create_workspace(
        self, *, user: dict[str, Any], domain_id: str, name: str,
        description: str = "",
    ) -> dict[str, Any]:
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise WorkspaceValidationError("domain_id must be non-empty")
        if not isinstance(name, str) or not name.strip():
            raise WorkspaceValidationError("name must be a non-empty string")
        if not await self._adb.can_create_in_domain(_uid(user), domain_id):
            raise WorkspaceForbiddenError(
                f"domain not bound: {domain_id}")
        return await self._db.create_workspace(
            domain_id=domain_id, name=name.strip(),
            description=description or "", owner_user_id=_uid(user),
            created_by=_uid(user))

    async def list_workspaces(
        self, *, user: dict[str, Any], domain_id: str,
    ) -> list[dict[str, Any]]:
        """admin 两旗（site=user.site_role 现值；domain=库现查）= 本域全部；
        余人=owner 行或成员行命中。域解析归路由层（缺省域回落）。"""
        is_site = _is_site_admin(user)
        is_dom_admin = (
            not is_site
            and await self._adb.is_domain_admin(_uid(user), domain_id))
        return await self._db.list_workspaces(
            domain_id=domain_id, user_id=_uid(user),
            is_site_admin=is_site, is_domain_admin=is_dom_admin)

    async def get_workspace(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        """详情附 my_role（admin 旁路归一为 owner——前端可写判定口径，同
        artifact_service.get_artifact）。"""
        ws, role = await self._require(user, workspace_id, *ROLE_READ)
        my_role = "owner" if role in _ADMIN_ROLES else role
        return {**ws, "my_role": my_role}

    async def patch_workspace(
        self, *, user: dict[str, Any], workspace_id: str,
        name: str | None = None, description: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        await self._require(user, workspace_id, *ROLE_DESIGN)
        try:
            return await self._db.patch_workspace(
                workspace_id=workspace_id, name=name,
                description=description,
                expected_updated_at=expected_updated_at)
        except WorkspaceConflictError as exc:
            raise WorkspaceConflictError(
                str(exc), code="workspace_conflict") from exc

    async def archive_workspace(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        """归档（幂等软删）：owner-only + admin 旁路。"""
        await self._require(user, workspace_id, *ROLE_OWNER_ONLY)
        return await self._db.archive_workspace(workspace_id=workspace_id)

    # ------------------------------------------------------------ 图

    async def get_graph(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        ws, _ = await self._require(user, workspace_id, *ROLE_READ)
        return {"graph": ws["graph_json"],
                "graph_revision": ws["graph_revision"],
                "batch_no": ws["batch_no"]}

    async def put_graph(
        self, *, user: dict[str, Any], workspace_id: str, graph: Any,
        expected_revision: int,
    ) -> dict[str, Any]:
        """整图替换：Conflict→409 graph_conflict；形态/环/引用→422
        graph_invalid（db 层校验四件套，消息透传定位）。"""
        if isinstance(expected_revision, bool) \
                or not isinstance(expected_revision, int) \
                or expected_revision < 0:
            raise WorkspaceValidationError(
                "expected_revision must be a non-negative integer")
        await self._require(user, workspace_id, *ROLE_DESIGN)
        try:
            return await self._db.put_graph(
                workspace_id=workspace_id, graph=graph,
                expected_revision=expected_revision)
        except WorkspaceConflictError as exc:
            raise WorkspaceConflictError(
                str(exc), code="graph_conflict") from exc
        except WorkspaceValidationError as exc:
            raise WorkspaceValidationError(
                str(exc), code="graph_invalid") from exc

    # ------------------------------------------------------------ 成员

    async def list_members(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> list[dict[str, Any]]:
        await self._require(user, workspace_id, *ROLE_READ)
        return await self._db.list_members(workspace_id=workspace_id)

    async def upsert_member(
        self, *, user: dict[str, Any], workspace_id: str,
        member_user_id: str, role: str,
    ) -> dict[str, Any]:
        """加成员/改角色（UPSERT 同口径，同 artifacts.upsert_member）。目标
        用户须在 kb_users 且 active（以库为准）；owner-only + admin 旁路。"""
        await self._require(user, workspace_id, *ROLE_OWNER_ONLY)
        if role not in _VALID_MEMBER_ROLES:
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        status = await self._adb.get_user_status(member_user_id)
        if status != "active":
            raise WorkspaceValidationError(
                f"invalid member user: {member_user_id}",
                code="invalid_member_user")
        return await self._db.add_member(
            workspace_id=workspace_id, user_id=member_user_id, role=role)

    async def update_member_role(
        self, *, user: dict[str, Any], workspace_id: str,
        member_user_id: str, role: str,
    ) -> dict[str, Any]:
        await self._require(user, workspace_id, *ROLE_OWNER_ONLY)
        if role not in _VALID_MEMBER_ROLES:
            raise WorkspaceValidationError(f"invalid role: {role!r}")
        current = await self._db.member_role(
            workspace_id=workspace_id, user_id=member_user_id)
        if current is None:
            raise WorkspaceNotFoundError(
                f"member not found: {member_user_id}",
                code="member_not_found")
        try:
            return await self._db.update_member_role(
                workspace_id=workspace_id, user_id=member_user_id, role=role)
        except WorkspaceNotFoundError as exc:  # 并发窗口兜底（行刚被删）
            raise WorkspaceNotFoundError(
                str(exc), code="member_not_found") from exc

    async def remove_member(
        self, *, user: dict[str, Any], workspace_id: str,
        member_user_id: str,
    ) -> bool:
        await self._require(user, workspace_id, *ROLE_OWNER_ONLY)
        removed = await self._db.remove_member(
            workspace_id=workspace_id, user_id=member_user_id)
        if not removed:
            raise WorkspaceNotFoundError(
                f"member not found: {member_user_id}",
                code="member_not_found")
        return True

    # ------------------------------------------------------------ 来源

    async def list_sources(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> list[dict[str, Any]]:
        await self._require(user, workspace_id, *ROLE_READ)
        rows = await self._db.list_sources(workspace_id=workspace_id)
        return [{**row, "input_manifest": _slim_manifest(
            row.get("input_manifest"))} for row in rows]

    async def upsert_source_kb_dir(
        self, *, user: dict[str, Any], workspace_id: str, node_id: str,
        kb_id: str, directories: list[str] | None = None,
    ) -> dict[str, Any]:
        """kb_dir 接入（即读即冻）：read_kb_dir 与 upsert_source 同连接——
        ACL 判定、快照读取、manifest 落库、batch_no+1 一条链（WB-07）。"""
        await self._require(user, workspace_id, *ROLE_DESIGN)
        async with self._pool.connection() as conn:
            try:
                _, manifest = await read_kb_dir(
                    conn, kb_id=kb_id,
                    directories=directories or [], user=user)
            except WorkspacePermissionError as exc:
                raise WorkspaceSourceForbiddenError(
                    f"source kb not readable: {kb_id}") from exc
            except WorkspaceNotFoundError as exc:
                # KB 不存在/非 active 与无权同面（不泄露存在性）
                raise WorkspaceSourceForbiddenError(
                    f"source kb not readable: {kb_id}") from exc
            row = await self._db.upsert_source(
                conn, workspace_id=workspace_id, node_id=node_id,
                connector="kb_dir",
                config={"kb_id": kb_id, "directories": directories or []},
                input_manifest=_jsonable(manifest), updated_by=_uid(user))
        return await self._source_response(workspace_id, row)

    async def upsert_source_excel(
        self, *, user: dict[str, Any], workspace_id: str, node_id: str,
        content: bytes, file_name: str, sheet_name: str | None = None,
    ) -> dict[str, Any]:
        """excel 接入：字节 → parse_excel（openpyxl 纯 CPU，经 to_thread 移出
        事件循环——先例=artifacts 上传）→ 冻结落库。"""
        await self._require(user, workspace_id, *ROLE_DESIGN)
        _, manifest = await anyio.to_thread.run_sync(
            lambda: parse_excel(content, file_name=file_name,
                                sheet_name=sheet_name))
        row = await self._db.upsert_source(
            None, workspace_id=workspace_id, node_id=node_id,
            connector="excel",
            config={"file_name": file_name,
                    "sheet_name": manifest.get("sheet_name")},
            input_manifest=_jsonable(manifest), updated_by=_uid(user))
        return await self._source_response(workspace_id, row)

    async def delete_source(
        self, *, user: dict[str, Any], workspace_id: str, node_id: str,
    ) -> bool:
        await self._require(user, workspace_id, *ROLE_DESIGN)
        if not await self._db.delete_source(
                workspace_id=workspace_id, node_id=node_id):
            raise WorkspaceNotFoundError(
                f"source not found: {node_id}", code="source_not_found")
        return True

    async def _source_response(
        self, workspace_id: str, row: dict[str, Any],
    ) -> dict[str, Any]:
        """来源写响应：行（剥 manifest 本体）+ frozen 计数 + 推进后的
        batch_no（重读工作区行——并发下的权威值）。"""
        manifest = row.get("input_manifest")
        ws = await self._db.get_workspace(workspace_id=workspace_id)
        return {
            "id": row.get("id"),
            "node_id": row.get("node_id"),
            "connector": row.get("connector"),
            "config": row.get("config"),
            "last_read_at": row.get("last_read_at"),
            "created_by": row.get("created_by"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "frozen": _frozen_summary(str(row.get("connector")), manifest),
            "batch_no": (int(ws["batch_no"]) if ws is not None
                         else None),
        }

    # ------------------------------------------------------------ 运行面

    async def start_run(
        self, *, user: dict[str, Any], workspace_id: str, scope: str,
        target_node_id: str | None = None,
    ) -> dict[str, Any]:
        """启动 run（design 角色；服务端先拒 subgraph——与执行器词表同步）。
        在飞 → 409 run_in_progress；归档/空图/目标缺失 → 422（执行器口径）。
        """
        await self._require(user, workspace_id, *ROLE_DESIGN)
        if scope not in _STARTABLE_SCOPES:
            raise WorkspaceValidationError(
                f"scope 未接线（仅 workspace/node，subgraph 预留）: {scope!r}",
                code="invalid_scope")
        try:
            run_id = await self._executor.start_run(
                workspace_id, scope=scope, target_node_id=target_node_id,
                created_by=_uid(user))
        except WorkspaceConflictError as exc:
            raise WorkspaceConflictError(
                str(exc), code="run_in_progress") from exc
        return {"run_id": run_id}

    async def list_runs(
        self, *, user: dict[str, Any], workspace_id: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """run 清单剥 graph_snapshot_json（审计快照可达 20MB，清单不下发；
        单 run 详情经 get_run_status 同样剥快照）。"""
        await self._require(user, workspace_id, *ROLE_READ)
        rows = await self._rdb.list_runs(workspace_id, limit=limit)
        return [{k: v for k, v in r.items()
                 if k != "graph_snapshot_json"} for r in rows]

    async def get_run_status(
        self, *, user: dict[str, Any], workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        """run 状态轮询（read+）。执行器 NotFound（run 不属于本工作区/不
        存在）重打 run_not_found 码——与 cancel 的反探测信封同面。"""
        await self._require(user, workspace_id, *ROLE_READ)
        try:
            return await self._executor.get_status(workspace_id, run_id)
        except WorkspaceNotFoundError as exc:
            raise WorkspaceNotFoundError(
                str(exc), code="run_not_found") from exc

    async def cancel_run(
        self, *, user: dict[str, Any], workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        """取消（幂等）：先验 run 属于本工作区（反探测 404），再走执行器；
        已终态 → cancelled=False 不抛。"""
        await self._require(user, workspace_id, *ROLE_DESIGN)
        run = await self._rdb.get_run(run_id)
        if run is None or str(run["workspace_id"]) != workspace_id:
            raise WorkspaceNotFoundError(
                f"run not found in workspace: {run_id}",
                code="run_not_found")
        cancelled = await self._executor.cancel_run(run_id)
        return {"cancelled": bool(cancelled)}

    # ------------------------------------------------------------ 输出与试跑

    async def get_current_outputs(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        """当前输出面（read+）：dict["node:port" -> 输出行]，口径与 Agent
        消费/前端画布同源（db_workspace_runs.get_current_outputs）。"""
        await self._require(user, workspace_id, *ROLE_READ)
        outputs = await self._rdb.get_current_outputs(workspace_id)
        return {"outputs": outputs}

    async def trial_node(
        self, *, user: dict[str, Any], workspace_id: str, node_id: str,
        port_id: str,
    ) -> dict[str, Any]:
        """试跑（design 角色；不建 run 不写库——执行器口径）。port_id 须是
        节点声明的输出端口（结果键校验，给 URL 端口段以语义）。"""
        await self._require(user, workspace_id, *ROLE_DESIGN)
        result = await self._executor.trial_node(workspace_id, node_id)
        outputs = result.get("outputs")
        if isinstance(outputs, dict) and outputs \
                and port_id not in outputs:
            raise WorkspaceValidationError(
                f"port not found: {port_id}", code="port_not_found")
        return {"node_id": node_id, "port_id": port_id, **result}
