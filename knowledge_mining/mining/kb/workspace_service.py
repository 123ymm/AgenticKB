# -*- coding: utf-8 -*-
"""KB 知识制品工作台服务层（67号 MVP Task 8/9）——权限反探测 + CRUD/图/
来源/运行面/成员编排 + 导出交付。

机制权威 = docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 8；
**2026-11-17 收紧（用户指令：工作台仅站点/域管理员可用）**：

- **权限判定序收口为仅管理员**（_assert_ws_role）：site admin
  （user.site_role 现值）→ 返回 "admin"；domain admin
  （ArtifactDB.is_domain_admin 现查——口径=site_role='admin' OR
  user_domains 行 domain_role='admin'）→ 返回 "domain_admin"；**其余一律
  WorkspaceNotFoundError**（404 反探测：不存在与无权同响应）。原「域绑定
  预检→成员行→owner 兜底」的 owner/editor/viewer 成员面全部退役——allowed
  参数保留签名但恒满足（能过判定的都是管理员，天然满足任意角色档）。
  治理操作（成员管理/归档/真删除）原 owner-only 限定随之失效（管理员即
  有权）。create_workspace 与 list_workspaces 加管理员门禁：不过 → 403
  WorkspaceForbiddenError（workspace_forbidden；创建/列表无反探测对象——
  域本身可见，只有台不可建不可列）。
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

Task 9 导出交付（export_drafts，机制权威=计划文档 Task 9）：

- **request_id 幂等三态**：succeeded → 原样回放 result_json（响应 duplicate
  标记，200 语义）；pending → 409 export_in_progress；failed → **复用该行
  重跑**（reset 回 pending——拍板：新导出新 id 会撞 request_id UNIQUE，
  行保留账记同处）。幂等行**先建后执行**（偏离计划字面的「exports 行随首
  item 事务」）：多 item 请求首 item 可能门禁全败不进事务，行不建则终态
  无处回写；且先建行让并发重放在任何 item 落地前即被 409 拦截（不产生
  双份制品副作用）。**归属校验先于回放/重置**（审查 Medium-1）：request_id
  全局唯一但行归属工作区——异工作区重放 404 反探测（不泄露他区制品/
  节点元数据），reset 带 workspace_id 条件防异区翻 pending 借行执行。
- **逐 item 门禁（任一不过→该 item 失败不阻断其余）**：a) 输出存在且
  is_current 且非 stale 且 row_count>0；b) 图上溯 kb_dir 血缘对 config.kb_id
  重验 KB 可见性（read_kb_dir 同款谓词 KbDB.is_visible——执行不重读 KB 的
  撤权检查移到这里，67号 §7.1 后半句）；c) 绑定存在且制品 updated_at >
  binding.last_exported_at → artifact_modified_since_export（防覆盖人工
  改动；owner 须同域有效用户）。
- **单 item 单事务（手法铁律）**：db_artifacts 各方法自开事务不能串调凑
  单事务——service 内联 pool.connection() 事务调 conn 级内核（Task 9b
  提炼）：创建制品+owner 成员行+操作者 editor 行（owner≠操作者时）+建列
  （样本推断六列型）+rev0 行（row_key=hex(_id)；provenance=[{"_source",
  "workspace":{id,node,port,run_id}}] 数组口径）+upsert_binding 同事务。
  **同钟口径**：制品 updated_at（DB now() 写入）与 binding.last_exported_at
  （同事务 SELECT now() 显式传入）取同一事务时间戳——人工改动门禁不因
  Python/DB 双钟偏差误伤（无改动重导出恒等放行= B.5 重写草稿语义：删除
  重插 rev0，provenance 不拼接、不解除绑定、不新建制品）。
- 终态回写：任一 item 成功 → exports 行 succeeded+result_json（部分成功
  是合法态，200+逐项 status）；全部失败 → 行 failed 后抛 export_all_failed
  （消息带逐项失败码）。不写 kb_artifact_batches（67号 §4.3）；不发布不
  拨指针无 MCP 开放。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import anyio

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_artifacts import (
    ArtifactDB,
    ArtifactError,
    _encode_key_parts,
    _json as _artifact_json,
)
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.services.artifact_import_service import (
    _infer_column_type,
)
from knowledge_mining.mining.kb.workspace_executor import WorkspaceExecutor
from knowledge_mining.mining.kb.workspace_sources import (
    WorkspacePermissionError,
    _SingleConnPool,
    parse_excel,
    read_kb_dir,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspaceForbiddenError",
    "WorkspaceService",
    "WorkspaceSourceForbiddenError",
]

#: 2026-11-17 收紧后以下三档仅作调用点签名兼容保留——_assert_ws_role 只认
#: 管理员（admin/domain_admin），allowed 恒满足，不再有成员角色分档。
ROLE_READ = ("owner", "editor", "viewer")
ROLE_DESIGN = ("owner", "editor")
ROLE_OWNER_ONLY = ("owner",)

#: run scope 服务端词表（subgraph 与执行器同步拒绝——迁移 012 词表预留）。
_STARTABLE_SCOPES = ("workspace", "node")

_VALID_MEMBER_ROLES = ("owner", "editor", "viewer")


class WorkspaceForbiddenError(WorkspaceError):
    """403——建台/列表管理员门禁不过（2026-11-17 收紧；非管理员对既有
    工作区的访问走 404 反探测，见模块 docstring）。"""

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
    ArtifactDB（is_domain_admin/get_user_status——谓词唯一真相源不自造，
    口径同 kb/db.py；user_in_domain/can_create_in_domain 在 2026-11-17
    收紧后不再使用）。
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
        """返回 (workspace, role)——2026-11-17 收紧为仅管理员判定：site
        admin → "admin"；is_domain_admin(uid, ws.domain) → "domain_admin"；
        **其余一律 WorkspaceNotFoundError**（404 反探测；原 owner/成员行/
        viewer 路径全部退役）。allowed 保留签名但恒满足（管理员天然满足
        任意角色档）；workspace 行由调用方（_require）先行取好。"""
        if _is_site_admin(user):
            return workspace, "admin"
        if await self._adb.is_domain_admin(
                _uid(user), str(workspace["domain_id"])):
            return workspace, "domain_admin"
        raise WorkspaceNotFoundError(
            f"workspace not found: {workspace['id']}")

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
        """2026-11-17 收紧：建台管理员门禁——is_domain_admin（含 site
        admin 全通）不过 → 403 workspace_forbidden（创建无反探测对象，
        域本身可见）。原 can_create_in_domain（绑定 member 即放行）退役。"""
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise WorkspaceValidationError("domain_id must be non-empty")
        if not isinstance(name, str) or not name.strip():
            raise WorkspaceValidationError("name must be a non-empty string")
        if not await self._adb.is_domain_admin(_uid(user), domain_id):
            raise WorkspaceForbiddenError(
                f"admin only (site/domain): {domain_id}")
        return await self._db.create_workspace(
            domain_id=domain_id, name=name.strip(),
            description=description or "", owner_user_id=_uid(user),
            created_by=_uid(user))

    async def list_workspaces(
        self, *, user: dict[str, Any], domain_id: str,
    ) -> list[dict[str, Any]]:
        """2026-11-17 收紧：site admin 全通；否则对查询域做
        is_domain_admin（域解析归路由层——缺省域回落同 _resolve_domain），
        不过 → 403 workspace_forbidden（无「自己参与的」面）。过门后两类
        管理员都是本域全量（admin 两旗只是 db 层查询范围）。"""
        is_site = _is_site_admin(user)
        is_dom_admin = (
            not is_site
            and await self._adb.is_domain_admin(_uid(user), domain_id))
        if not is_site and not is_dom_admin:
            raise WorkspaceForbiddenError(
                f"admin only (site/domain): {domain_id}")
        return await self._db.list_workspaces(
            domain_id=domain_id, user_id=_uid(user),
            is_site_admin=is_site, is_domain_admin=is_dom_admin)

    async def get_workspace(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        """详情附 my_role——收紧后只剩合成角色 admin/domain_admin（原
        「admin 归一 owner」退役：前端 canWrite 改为对两角色放行）。"""
        ws, role = await self._require(user, workspace_id, *ROLE_READ)
        return {**ws, "my_role": role}

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

    async def archive_or_delete(
        self, *, user: dict[str, Any], workspace_id: str,
    ) -> dict[str, Any]:
        """DELETE 两态（用户反馈问题1——两步门禁，照制品「先停用再删除」）：
        active → 归档（幂等软删，返回行 status='archived'）；已 archived →
        purge 真删（单事务七表清理，返回 {"id", "status": "deleted"}——行已
        不存在，不再有整行可回）。2026-11-17 收紧后原 owner-only 限定退役
        ——_require 只放行两类管理员，天然有权；db 层 purge 锁内再验一次
        archived（并发改态的纵深防御）。"""
        ws, _ = await self._require(user, workspace_id, *ROLE_OWNER_ONLY)
        if str(ws.get("status")) == "archived":
            await self._db.purge_workspace(workspace_id=workspace_id)
            return {"id": workspace_id, "status": "deleted"}
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
        用户须在 kb_users 且 active（以库为准）；2026-11-17 收紧后治理权=
        过 _require 的两类管理员（原 owner-only 限定退役）。"""
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
        节点声明的输出端口（结果键校验，给 URL 端口段以语义）；**空
        outputs 边缘同拦**（Task 9 补口：无声明端口与端口不存在同面 →
        404 output_not_found，与导出门禁码族一致）。"""
        await self._require(user, workspace_id, *ROLE_DESIGN)
        result = await self._executor.trial_node(workspace_id, node_id)
        outputs = result.get("outputs")
        if not isinstance(outputs, dict) or port_id not in outputs:
            raise WorkspaceNotFoundError(
                f"port not found: {port_id}", code="output_not_found")
        return {"node_id": node_id, "port_id": port_id, **result}

    # ------------------------------------------------------------ 导出交付

    @staticmethod
    def _kb_dir_lineage(
        nodes: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
        node_id: str,
    ) -> list[str]:
        """图上溯 node_id（含自身）全部 kb_dir 来源的 config.kb_id——执行
        不重读 KB（Task 5 决策 3），撤权检查移至导出门禁的血缘输入。图
        已保证无环（put_graph 校验），BFS 自然终止。"""
        by_id = {str(n.get("id")): n for n in nodes}
        seen = {node_id}
        queue = [node_id]
        while queue:
            node = by_id.get(queue.pop())
            if node is None:
                continue
            for ref in node.get("inputs") or []:
                producer = str(ref).split(":", 1)[0]
                if producer not in seen:
                    seen.add(producer)
                    queue.append(producer)
        kb_ids: list[str] = []
        for nid in seen:
            row = sources.get(nid)
            if row is None or str(row.get("connector")) != "kb_dir":
                continue
            kb_id = str((row.get("config") or {}).get("kb_id") or "")
            if kb_id and kb_id not in kb_ids:
                kb_ids.append(kb_id)
        return kb_ids

    @staticmethod
    def _build_export_table(
        dataset: dict[str, Any], *, workspace_id: str, node_id: str,
        port_id: str, run_id: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """输出数据集 → 制品列定义 + rev0 行（导出专用口径）：

        - 列：cols 全量样本推断六列型（_infer_column_type——excel 冻结解析
          同源），column_id=c001 序（artifact_import create_specs 同构）；
        - 行：row_key=hex(_id)（与手工行增/追加导入的主键编码同构）；data
          只含 cols（_id 进 row_key、_source 进 provenance）；provenance=
          [{"_source", "workspace":{id,node,port,run_id}}]——数组口径
          （db_artifacts 既有不变量，UPSERT 拼接依赖数组）；ordinal 按序；
          data/provenance/conflicts 已 _json 编码（_insert_rows_in_conn 的
          flat 行契约）。
        """
        cols = [str(c) for c in (dataset.get("cols") or [])]
        rows = list(dataset.get("rows") or [])
        columns: list[dict[str, Any]] = []
        for position, col in enumerate(cols, start=1):
            ctype, enum = _infer_column_type([r.get(col) for r in rows])
            columns.append({
                "column_id": f"c{position:03d}", "column_name": col,
                "column_type": ctype, "enum_values": enum or None,
                "is_primary_key": False, "is_indexed": False,
                "alias": [], "ordinal": position,
            })
        lineage = {"id": workspace_id, "node": node_id, "port": port_id,
                   "run_id": run_id}
        flat: list[dict[str, Any]] = []
        for ordinal, row in enumerate(rows, start=1):
            flat.append({
                "row_key": _encode_key_parts([str(row["_id"])]),
                "data": _artifact_json({c: row.get(c) for c in cols}),
                "provenance": _artifact_json([
                    {"_source": str(row.get("_source") or ""),
                     "workspace": lineage}]),
                "conflicts": _artifact_json({}), "ordinal": ordinal,
            })
        return columns, flat

    async def _export_one(
        self, ws: dict[str, Any], user: dict[str, Any], item: dict[str, Any],
        outputs: dict[str, dict[str, Any]], graph_nodes: list[dict[str, Any]],
        sources: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """单 item 门禁+单事务落制品（口径见模块 docstring Task 9 节）。

        返回逐项结果 {node_id, port_id, artifact_id, name, status, error?}；
        门禁/事务失败 → status=failed + error=机器码，不抛（编排层聚账）。"""
        workspace_id = str(ws["id"])
        operator = _uid(user)
        node_id = str(item.get("node_id") or "")
        port_id = str(item.get("port_id") or "")
        name = str(item.get("artifact_name") or "").strip()
        result: dict[str, Any] = {"node_id": node_id, "port_id": port_id,
                                  "artifact_id": None, "name": name,
                                  "status": "failed"}
        try:
            if not node_id or not port_id or not name or len(name) > 200:
                raise WorkspaceValidationError("invalid export item")
            # 门禁 a：输出存在且 is_current 且非 stale 且 row_count>0
            out = outputs.get(f"{node_id}:{port_id}")
            if out is None:
                raise WorkspaceValidationError(
                    f"output not found: {node_id}:{port_id}",
                    code="output_not_found")
            if out.get("stale_since_run"):
                raise WorkspaceValidationError(
                    f"output stale since {out['stale_since_run']}",
                    code="stale_output")
            if not int(out.get("row_count") or 0):
                raise WorkspaceValidationError(
                    "output has no rows", code="empty_output")
            # owner：默认工作区 owner；指定时须同域有效用户。
            # 站点/域管理员旁路（is_domain_admin，内含 active+未软删校验）：
            # 与 _resolve_domain/_assert_ws_role 判定序同口径——admin 是系统
            # 管理员、不依赖 user_domains 绑定行（内网无绑定 admin 建工作台
            # 走默认域回落可成、导出此前被严格 user_in_domain 拦死）。
            owner_id = str(item.get("owner_user_id") or "") \
                or str(ws["owner_user_id"])
            if not (
                await self._adb.is_domain_admin(
                    owner_id, str(ws["domain_id"]))
                or (await self._adb.get_user_status(owner_id) == "active"
                    and await self._adb.user_in_domain(
                        owner_id, str(ws["domain_id"])))):
                raise WorkspaceValidationError(
                    f"invalid owner user: {owner_id}",
                    code="invalid_owner_user")
            # 门禁 b：kb_dir 血缘撤权重验（read_kb_dir 同款谓词）
            kb_ids = self._kb_dir_lineage(graph_nodes, sources, node_id)
            if kb_ids:
                async with self._pool.connection() as conn:
                    kbdb = KbDB(_SingleConnPool(conn))
                    for kb_id in kb_ids:
                        if not await kbdb.is_visible(
                                kb_id=kb_id, user_id=operator):
                            raise WorkspaceValidationError(
                                f"kb not readable: {kb_id}",
                                code="source_permission_revoked")
            # 门禁 c：绑定+人工改动（updated_at 晚于上次导出即阻断）
            binding = await self._rdb.get_binding(
                workspace_id=workspace_id, node_id=node_id, port_id=port_id)
            artifact: dict[str, Any] | None = None
            expected_ts: Any = None
            if binding is not None:
                artifact = await self._adb.get_artifact(
                    str(binding["artifact_id"]))
            if artifact is not None:
                expected_ts = artifact["updated_at"]
                last_exported = binding.get("last_exported_at")
                if isinstance(last_exported, str) and last_exported:
                    try:
                        last_ts = datetime.fromisoformat(last_exported)
                    except ValueError:
                        last_ts = None
                    if last_ts is None or artifact["updated_at"] > last_ts:
                        raise WorkspaceValidationError(
                            "artifact modified since last export",
                            code="artifact_modified_since_export")
            # 单 item 单事务：conn 级内核串装（手法铁律，见模块 docstring）
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    cur = await conn.execute("SELECT now() AS ts")
                    txn_now = (await cur.fetchone())["ts"]
                    if artifact is None:  # 首导出（或绑定指向的制品已删）
                        artifact = await ArtifactDB._create_artifact_in_conn(
                            conn, domain_id=str(ws["domain_id"]),
                            kind="table", title=name, description="",
                            owner_user_id=owner_id, visibility="private")
                    else:  # B.5 重写草稿：并发人工改动由锁内令牌校验兜底
                        await self._adb._assert_expected_updated_at(
                            conn, artifact["artifact_id"], expected_ts)
                    if operator != str(artifact["owner_user_id"]):
                        await ArtifactDB._upsert_member_in_conn(
                            conn, artifact["artifact_id"], operator, "editor")
                    dataset = out.get("content_inline")
                    if not isinstance(dataset, dict):
                        # 防御分支（审查 Low-4）：content_inline 由 jsonb
                        # 解析必为 dict（脏形态在 write_outputs 写库前已
                        # 拦）——独立码名 invalid_output 便于区分脏数据与
                        # 空输出（empty_output）。
                        raise WorkspaceValidationError(
                            "output dataset unreadable",
                            code="invalid_output")
                    columns, flat = self._build_export_table(
                        dataset, workspace_id=workspace_id, node_id=node_id,
                        port_id=port_id, run_id=out.get("run_id"))
                    await ArtifactDB._replace_columns_in_conn(
                        conn, artifact["artifact_id"], columns)
                    await ArtifactDB._delete_rows_in_conn(
                        conn, artifact["artifact_id"], 0)
                    await ArtifactDB._insert_rows_in_conn(
                        conn, artifact["artifact_id"], 0, flat, operator)
                    await self._rdb.upsert_binding(
                        conn, workspace_id=workspace_id, node_id=node_id,
                        port_id=port_id,
                        artifact_id=str(artifact["artifact_id"]),
                        artifact_kind="table",
                        last_run_id=out.get("run_id"), updated_by=operator,
                        exported_at=txn_now.isoformat())
            return {**result, "artifact_id": str(artifact["artifact_id"]),
                    "status": "succeeded"}
        except WorkspaceValidationError as exc:
            return {**result, "error": str(exc.code or "workspace_validation")}
        except ArtifactError as exc:
            code = ("artifact_modified_since_export"
                    if str(exc.code) == "stale_artifact" else "export_failed")
            logger.warning("workspace export item failed (%s): %s",
                           node_id, exc.code)
            return {**result, "error": code}
        except Exception as exc:  # noqa: BLE001 —— 单 item 失败不阻断其余
            logger.warning("workspace export item crashed: %s:%s %r",
                           node_id, port_id, exc)
            return {**result, "error": "export_failed"}

    async def export_drafts(
        self, workspace_id: str, user: dict[str, Any], *, request_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """导出交付入口（design 角色；机制见模块 docstring Task 9 节）。"""
        if not isinstance(request_id, str) or not request_id:
            raise WorkspaceValidationError("request_id must be non-empty")
        if not isinstance(items, list) or not items:
            raise WorkspaceValidationError(
                "items must be a non-empty list")
        ws, _role = await self._require(user, workspace_id, *ROLE_DESIGN)
        outputs = await self._rdb.get_current_outputs(workspace_id)
        graph_nodes = list((ws.get("graph_json") or {}).get("nodes") or [])
        sources = {str(r["node_id"]): r for r in await self._db.list_sources(
            workspace_id=workspace_id)}

        # request_id 幂等三态 + 行先建后执行（见模块 docstring）。
        # created=True（本请求刚建行）直接执行——pending 三态判定只对
        # 既有行生效，否则自建行会把自己判成在飞。
        export_row = await self._rdb.get_export_by_request(
            request_id=request_id)
        created = False
        if export_row is None:
            first = outputs.get(
                f"{items[0].get('node_id')}:{items[0].get('port_id')}")
            export_row, created = await self._rdb.insert_export(
                None, request_id=request_id, workspace_id=workspace_id,
                node_id=str(items[0].get("node_id") or ""),
                port_id=str(items[0].get("port_id") or ""),
                snapshot_run_id=(first or {}).get("run_id"),
                mapping_json={"items": [
                    {"node_id": str(i.get("node_id") or ""),
                     "port_id": str(i.get("port_id") or ""),
                     "artifact_name": str(i.get("artifact_name") or ""),
                     "owner_user_id": str(i.get("owner_user_id") or "")}
                    for i in items]},
                created_by=_uid(user))
        # 归属校验先于一切回放/重置（审查 Medium-1：request_id 全局唯一，
        # 异工作区重放不得拿到他区 result_json、也不得把他区 failed 行翻
        # pending 借行执行——404 反探测与族口径一致。覆盖入口查询与
        # insert_export 并发回读两条来路；created=True 的新行必属本区）。
        if not created and str(export_row["workspace_id"]) != workspace_id:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace_id}")
        if not created:
            status = str(export_row["status"])
            if status == "succeeded":
                payload = dict(export_row.get("result_json") or {})
                return {**payload, "duplicate": True}
            if status == "pending":
                raise WorkspaceConflictError(
                    f"export in progress: {request_id}",
                    code="export_in_progress")
            if not await self._rdb.reset_failed_export(
                    export_id=str(export_row["id"]),
                    workspace_id=workspace_id):
                raise WorkspaceConflictError(
                    f"export in progress: {request_id}",
                    code="export_in_progress")

        results = [
            await self._export_one(ws, user, item, outputs, graph_nodes,
                                   sources)
            for item in items
        ]
        succeeded = [r for r in results if r["status"] == "succeeded"]
        if not succeeded:
            codes = ",".join(sorted({
                str(r.get("error") or "export_failed") for r in results}))
            await self._rdb.mark_export_result(
                str(export_row["id"]), "failed",
                error=f"export_all_failed: {codes}")
            raise WorkspaceValidationError(
                f"export_all_failed: {codes}", code="export_all_failed")
        await self._rdb.mark_export_result(
            str(export_row["id"]), "succeeded",
            result_json={"exports": results},
            target_artifact_id=str(succeeded[0]["artifact_id"]))
        return {"exports": results}
