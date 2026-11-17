# -*- coding: utf-8 -*-
"""KB 知识制品工作台 REST 路由 —— /api/workspaces（67号 §5.5 / Task 8/9）。

十九端点：工作区 CRUD（DELETE 两态：active→归档 / 已归档→真删）/ 成员四端点（owner-only+admin 旁路）/
图读·写（乐观锁 409 graph_conflict / 校验环 422 graph_invalid）/ 来源三端点
（kb_dir 即读即冻 + excel multipart + 删）/ 运行四端点（start 202·cancel 幂等）/
当前输出读 / 单口试跑（空 outputs→404 output_not_found，Task 9 补口）/
导出交付（POST /exports，Task 9：幂等 request_id + 单事务私有草稿）。

薄 handler + _map_error 错误翻译（照 routes/artifacts._map_error 风格）：
业务与权限全部在 WorkspaceService（见 workspace_service.py 模块 docstring 的
判定序与反探测拍板）。auth=current_user；服务经 deps.get_workspace_service。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
from knowledge_mining.mining.kb.db_workspaces import WorkspaceError
from knowledge_mining.mining.kb.deps import (
    get_artifact_db,
    get_workspace_service,
)
from knowledge_mining.mining.kb.services.artifact_import_service import (
    MAX_UPLOAD_BYTES,
)
from knowledge_mining.mining.infra.domain_pack import (
    get_default_domain,
    resolve_domain,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

#: multipart 开销预算（对齐 routes/artifacts._UPLOAD_BODY_BUDGET 口径）。
_UPLOAD_BODY_BUDGET = MAX_UPLOAD_BYTES + 256 * 1024


# ----------------------------------------------------------------- models

class WorkspaceCreate(BaseModel):
    """创建：domain_id 缺省回落用户首个绑定域（site admin 再回落 registry
    默认域）——解析在路由层（同 artifacts 创建端点）。"""
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    domain_id: str | None = Field(default=None, max_length=128)


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    expected_updated_at: str | None = None


class GraphPut(BaseModel):
    """整图替换：expected_revision = 当前 graph_revision（整数令牌，失配
    409 graph_conflict）。"""
    graph: dict[str, Any]
    expected_revision: int = Field(ge=0)


class SourcePut(BaseModel):
    """kb_dir 接入（即读即冻）。connector 词表限 kb_dir——excel 走
    /sources/excel multipart（JSON 通道传 excel → pydantic 422）。"""
    connector: Literal["kb_dir"]
    kb_id: str = Field(min_length=1, max_length=200)
    node_id: str = Field(min_length=1, max_length=200)
    directories: list[str] = Field(default_factory=list)


class RunStartReq(BaseModel):
    """scope 词表含 subgraph（到 service 翻 422 invalid_scope——与执行器
    同步；pydantic 层不吞，保持错误信封统一）。"""
    scope: str = Field(pattern="^(workspace|node|subgraph)$")
    target_node_id: str | None = Field(default=None, max_length=200)


class MemberAdd(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: str = Field(default="viewer", pattern="^(owner|editor|viewer)$")


class MemberPatch(BaseModel):
    role: str = Field(pattern="^(owner|editor|viewer)$")


class ExportItem(BaseModel):
    """单导出项：artifact_name 1..200（制品标题）；owner_user_id 缺省=
    工作区 owner（须同域有效用户——服务层逐项校验，不过该项不阻断其余）。"""
    node_id: str = Field(min_length=1, max_length=200)
    port_id: str = Field(min_length=1, max_length=200)
    artifact_name: str = Field(min_length=1, max_length=200)
    owner_user_id: str | None = Field(default=None,
                                      min_length=1, max_length=128)


class ExportCreate(BaseModel):
    """request_id = 幂等键（重放回放既有结果标 duplicate；在飞 409；
    failed 复用行重跑——机制见 workspace_service.Task 9 节）。"""
    request_id: str = Field(min_length=1, max_length=200)
    items: list[ExportItem] = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------- helpers

#: WorkspaceError.code → HTTP 状态（服务层打码后的码表；未收录回落 400，
#: 与 routes/wiki._WIKI_HTTP_STATUS 的兜底口径一致）。
_WS_HTTP_STATUS: dict[str, int] = {
    # 404 反探测族：不存在与无权同响应（不暴露存在性）
    "workspace_not_found": 404,
    "member_not_found": 404,
    "source_not_found": 404,
    "run_not_found": 404,
    "output_not_found": 404,  # 试跑端口不存在（Task 9 补口：空 outputs 同面）
    # 403：工作区有权但角色不足 / 来源 KB 无权（来源拍板见服务层 docstring）
    "workspace_forbidden": 403,
    "workspace_source_forbidden": 403,
    "workspace_permission": 403,  # 防御：适配器族异常直达路由时的兜底码
    # 409：乐观锁与单飞冲突族
    "graph_conflict": 409,
    "workspace_conflict": 409,
    "run_in_progress": 409,
    "export_in_progress": 409,
    # 422：形态/词表/护栏族（消息带定位，走裸 detail 透传）
    "graph_invalid": 422,
    "invalid_scope": 422,
    "invalid_member_user": 422,
    "export_all_failed": 422,  # 消息透传逐项失败码（服务层拼接）
    "workspace_validation": 422,
}

#: 404/403/409 信封文案（对齐 routes/artifacts 的 {code, message} 形态）。
_WS_MESSAGES: dict[str, str] = {
    "workspace_not_found": "工作区不存在或无权限访问",
    "member_not_found": "成员不存在",
    "source_not_found": "来源不存在",
    "run_not_found": "运行不存在或无权限访问",
    "output_not_found": "输出端口不存在或尚未执行",
    "workspace_forbidden": "角色权限不足",
    "workspace_source_forbidden": "无权读取来源知识库",
    "graph_conflict": "图已被他人修改（版本失配），请刷新后重试",
    "workspace_conflict": "工作区已被他人修改，请刷新后重试",
    "run_in_progress": "该工作区已有运行在飞，请等待完成或先取消",
    "export_in_progress": "该导出请求正在处理中，请稍后重试",
}


def _map_error(exc: Exception) -> HTTPException:
    """WorkspaceError 族按 code 表翻译，统一 {code, message} 信封——消息
    优先 _WS_MESSAGES 固定文案，无固定文案的码（422 校验族）透传异常原文
    （含定位，如 graph_invalid 的 "graph contains a cycle"）。"""
    if isinstance(exc, WorkspaceError):
        code = str(exc.code)
        status = _WS_HTTP_STATUS.get(code, 400)
        message = _WS_MESSAGES.get(code) or str(exc) or code
        return HTTPException(status, {"code": code, "message": message})
    return HTTPException(500, str(exc))


def _check_upload_size(request: Request, file: UploadFile) -> None:
    """content-length 预检 + 实读兜底（对齐 routes/artifacts 同名函数）。"""
    raw_length = request.headers.get("content-length", "").strip()
    if raw_length:
        try:
            if int(raw_length) > _UPLOAD_BODY_BUDGET:
                raise HTTPException(413, "file_too_large")
        except ValueError:
            raise HTTPException(400, "invalid_content_length") from None
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file_too_large")


async def _resolve_domain(
    adb: ArtifactDB, user: dict[str, Any], domain_id: str | None,
) -> str:
    """域解析（create/list 共用，照 artifacts 创建端点）：显式传入优先；
    缺省回落用户首个绑定域，site admin 再回落 registry 默认域；不可解析 →
    422 domain_id_required；非法域 → 400 invalid_domain。"""
    uid = str(user["id"])
    dom = (domain_id or "").strip()
    if not dom:
        dom = await adb.first_domain_for_user(uid)
        if not dom and user.get("site_role") == "admin":
            dom = get_default_domain()
    if not dom:
        raise HTTPException(422, "domain_id_required: 未提供 domain_id 且用户无绑定域")
    try:
        resolve_domain(dom)
    except Exception as exc:
        raise HTTPException(400, f"invalid_domain:{dom}") from exc
    return dom


# ------------------------------------------------------------ 工作区 CRUD

@router.post("", status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
    adb: ArtifactDB = Depends(get_artifact_db),
):
    dom = await _resolve_domain(adb, user, body.domain_id)
    try:
        return await svc.create_workspace(
            user=user, domain_id=dom, name=body.name,
            description=body.description or "")
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.get("")
async def list_workspaces(
    domain_id: str | None = Query(default=None, max_length=128),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
    adb: ArtifactDB = Depends(get_artifact_db),
):
    dom = await _resolve_domain(adb, user, domain_id)
    try:
        return {"workspaces": await svc.list_workspaces(
            user=user, domain_id=dom)}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.get_workspace(
            user=user, workspace_id=workspace_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.patch("/{workspace_id}")
async def patch_workspace(
    workspace_id: str,
    body: WorkspacePatch,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.patch_workspace(
            user=user, workspace_id=workspace_id, name=body.name,
            description=body.description,
            expected_updated_at=body.expected_updated_at)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.delete("/{workspace_id}")
async def archive_or_delete_workspace(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """DELETE 两态（用户反馈问题1——两步门禁）：active → 归档（幂等软删，
    status=archived，详情仍可读）；已归档 → 真删（status=deleted，七表清理；
    已导出的制品不受影响——绑定只是工作区侧的导出去处账目）。"""
    try:
        return await svc.archive_or_delete(
            user=user, workspace_id=workspace_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ----------------------------------------------------------------- 成员

@router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return {"members": await svc.list_members(
            user=user, workspace_id=workspace_id)}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.post("/{workspace_id}/members", status_code=201)
async def add_member(
    workspace_id: str,
    body: MemberAdd,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.upsert_member(
            user=user, workspace_id=workspace_id,
            member_user_id=body.user_id, role=body.role)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member(
    workspace_id: str,
    user_id: str,
    body: MemberPatch,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.update_member_role(
            user=user, workspace_id=workspace_id,
            member_user_id=user_id, role=body.role)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: str,
    user_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        await svc.remove_member(
            user=user, workspace_id=workspace_id, member_user_id=user_id)
        return {"removed": True}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ----------------------------------------------------------------- 图

@router.get("/{workspace_id}/graph")
async def get_graph(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.get_graph(
            user=user, workspace_id=workspace_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.put("/{workspace_id}/graph")
async def put_graph(
    workspace_id: str,
    body: GraphPut,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.put_graph(
            user=user, workspace_id=workspace_id, graph=body.graph,
            expected_revision=body.expected_revision)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ----------------------------------------------------------------- 来源

@router.get("/{workspace_id}/sources")
async def list_sources(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return {"sources": await svc.list_sources(
            user=user, workspace_id=workspace_id)}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.post("/{workspace_id}/sources", status_code=201)
async def upsert_source(
    workspace_id: str,
    body: SourcePut,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """kb_dir 即读即冻：读取-冻结-batch_no+1 同链落库（WB-07）。"""
    try:
        return await svc.upsert_source_kb_dir(
            user=user, workspace_id=workspace_id, node_id=body.node_id,
            kb_id=body.kb_id, directories=body.directories)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.post("/{workspace_id}/sources/excel", status_code=201)
async def upload_source_excel(
    workspace_id: str,
    request: Request,
    file: UploadFile = File(...),
    node_id: str = Form(min_length=1, max_length=200),
    sheet_name: str | None = Form(default=None),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """excel 上传冻结（multipart：file/node_id/sheet_name?）——整份数据集
    冻结进 manifest（重跑不重读文件；重传=更新来源并推进批次）。"""
    _check_upload_size(request, file)
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "unsupported_file_type: 仅支持 .xlsx")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file_too_large")
    try:
        return await svc.upsert_source_excel(
            user=user, workspace_id=workspace_id, node_id=node_id,
            content=content, file_name=filename, sheet_name=sheet_name)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.delete("/{workspace_id}/sources/{node_id}")
async def delete_source(
    workspace_id: str,
    node_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        await svc.delete_source(
            user=user, workspace_id=workspace_id, node_id=node_id)
        return {"deleted": True}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ----------------------------------------------------------------- 运行面

@router.post("/{workspace_id}/runs", status_code=202)
async def start_run(
    workspace_id: str,
    body: RunStartReq,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """启动 run（异步执行，202 只回 run_id；进度走 GET /runs/{rid}）。"""
    try:
        return await svc.start_run(
            user=user, workspace_id=workspace_id, scope=body.scope,
            target_node_id=body.target_node_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.get("/{workspace_id}/runs")
async def list_runs(
    workspace_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return {"runs": await svc.list_runs(
            user=user, workspace_id=workspace_id, limit=limit)}
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.get("/{workspace_id}/runs/{run_id}")
async def get_run_status(
    workspace_id: str,
    run_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.get_run_status(
            user=user, workspace_id=workspace_id, run_id=run_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.post("/{workspace_id}/runs/{run_id}/cancel")
async def cancel_run(
    workspace_id: str,
    run_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.cancel_run(
            user=user, workspace_id=workspace_id, run_id=run_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------- 输出与试跑

@router.get("/{workspace_id}/outputs/current")
async def get_current_outputs(
    workspace_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    try:
        return await svc.get_current_outputs(
            user=user, workspace_id=workspace_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


@router.post("/{workspace_id}/outputs/{node_id}/{port_id}/trial")
async def trial_node(
    workspace_id: str,
    node_id: str,
    port_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """单口试跑（不建 run 不写库；前 3 行切片口径见执行器）。"""
    try:
        return await svc.trial_node(
            user=user, workspace_id=workspace_id, node_id=node_id,
            port_id=port_id)
    except WorkspaceError as exc:
        raise _map_error(exc) from None


# ----------------------------------------------------------------- 导出

@router.post("/{workspace_id}/exports")
async def create_export(
    workspace_id: str,
    body: ExportCreate,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_workspace_service),
):
    """导出为私有表格制品草稿（幂等 request_id；机制见 workspace_service
    Task 9 节）：首发/部分成功 201+逐项 status；幂等重放 200+duplicate；
    在飞 409 export_in_progress；全部失败 422 export_all_failed（消息带
    逐项失败码）。"""
    try:
        result = await svc.export_drafts(
            workspace_id, user, request_id=body.request_id,
            items=[item.model_dump() for item in body.items])
    except WorkspaceError as exc:
        raise _map_error(exc) from None
    status = 200 if result.get("duplicate") else 201
    return JSONResponse(status_code=status, content=result)
