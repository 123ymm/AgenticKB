"""知识制品 REST 路由 —— /api/artifacts（61号 §十）。

薄 handler + _map_error 错误翻译（NotFound→404 反探测 / Forbidden→403 /
UsageNoteRequired→422 / PrimaryKeyConflict·StaleArtifact·NoPendingChanges
→409（带 code）/ InvalidOperation→400（unknown_column·formula 等）/
ArtifactImportError→413·410·429·422）。业务与权限全部
在 ArtifactService（见 services/artifact_service.py 权限矩阵注释）。

Excel 三步向导（§六）：upload（multipart→202 预览 token，登记绑定
artifact_id+uploader）→ preview/{token}（TTL 失效 410 / 绑定失配 404）→
import（plan 支持两种形态：build_mapping 产物，或
{sheet, header_row, column_ops} 原始 ops——后者由本层补跑 build_mapping，
返回前含 missing_columns 校验）。
"""
from __future__ import annotations

import uuid
from typing import Any

import anyio
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
from knowledge_mining.mining.kb.deps import (
    get_artifact_db,
    get_artifact_import_service,
    get_artifact_service,
)
from knowledge_mining.mining.kb.services.artifact_import_service import (
    MAX_UPLOAD_BYTES,
    ArtifactImportError,
    ArtifactImportService,
)
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactError,
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
    InvalidOperation,
    NoPendingChanges,
    PrimaryKeyConflict,
    StaleArtifact,
    UsageNoteRequired,
)
from knowledge_mining.mining.infra.domain_pack import (
    get_default_domain,
    resolve_domain,
)

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


class TableExtractRequest(BaseModel):
    kb_id: str = Field(min_length=1, max_length=128)
    document_ids: list[str] = Field(min_length=1, max_length=2000)


class PipelineStepRequest(BaseModel):
    parent_run_id: str
    operator: str
    parameters: dict[str, Any] = Field(default_factory=dict)


@router.get("/{artifact_id}/pipeline/operators")
async def pipeline_operators(artifact_id: str, user: dict = Depends(current_user)):
    # Registry metadata is public; every data operation below rechecks authorization.
    from knowledge_mining.mining.workflow.artifact_table_ops import CATALOG
    return {"items": [{"id": k, "name": v, "version": "1"} for k, v in CATALOG]}


async def get_pipeline_service(
    artifact_id: str, request: Request,
    user: dict = Depends(current_user),
    artifacts: ArtifactService = Depends(get_artifact_service),
):
    from knowledge_mining.mining.kb.services.artifact_pipeline_service import service_for
    try:
        artifact, _ = await artifacts._assert_role(user, artifact_id, "owner", "editor")
        return await service_for(request.app, artifact["domain_id"])
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/pipeline/extract", status_code=202)
async def extract_tables(
    artifact_id: str, body: TableExtractRequest, request: Request,
    user: dict = Depends(current_user), service=Depends(get_pipeline_service),
):
    try:
        run_id = await service.submit(user, artifact_id, body.kb_id, body.document_ids)
        artifact = await service.authorize(user, artifact_id)
        request.app.state.domain_run_dispatcher.kick(artifact["domain_id"])
        return {"run_id": run_id}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/pipeline/runs")
async def table_pipeline_runs(
    artifact_id: str, user: dict = Depends(current_user), service=Depends(get_pipeline_service),
):
    try:
        return {"items": await service.list_runs(user, artifact_id)}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/pipeline/steps", status_code=202)
async def pipeline_step(artifact_id: str, body: PipelineStepRequest, request: Request,
                        user: dict = Depends(current_user), service=Depends(get_pipeline_service)):
    try:
        run_id = await service.submit_step(user, artifact_id, body.parent_run_id, body.operator, body.parameters)
        artifact = await service.authorize(user, artifact_id)
        request.app.state.domain_run_dispatcher.kick(artifact["domain_id"])
        return {"run_id": run_id}
    except ArtifactError as exc: raise _map_error(exc) from None


@router.post("/{artifact_id}/pipeline/runs/{run_id}/cancel")
async def cancel_pipeline(artifact_id: str, run_id: str, user: dict = Depends(current_user), service=Depends(get_pipeline_service)):
    try:
        await service.read_run(user, artifact_id, run_id)
        await service.repo.cancel(run_id)
        return {"status": "cancelled"}
    except ArtifactError as exc: raise _map_error(exc) from None


@router.post("/{artifact_id}/pipeline/lookup")
async def upload_pipeline_lookup(artifact_id: str, file: UploadFile = File(...),
                                 user: dict = Depends(current_user), service=Depends(get_pipeline_service)):
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES: raise InvalidOperation("文件过大")
        return await service.upload_lookup(user, artifact_id, file.filename or "", content)
    except ArtifactError as exc: raise _map_error(exc) from None
    finally: await file.close()


@router.post("/{artifact_id}/pipeline/runs/{run_id}/publish")
async def publish_pipeline(artifact_id: str, run_id: str, body: dict[str, Any],
                           confirm: bool = Query(False), user: dict = Depends(current_user), service=Depends(get_pipeline_service)):
    from knowledge_mining.mining.kb.services.artifact_pipeline_publish import publish_snapshot
    try: return await publish_snapshot(service, user, artifact_id, run_id, body, confirm)
    except ArtifactError as exc: raise _map_error(exc) from None


@router.get("/{artifact_id}/pipeline/targets")
async def pipeline_targets(artifact_id: str, user: dict = Depends(current_user), service=Depends(get_pipeline_service)):
    try:
        artifact = await service.authorize(user, artifact_id)
        values = await service.artifacts.list_artifacts(user=user, domain_id=artifact["domain_id"])
        items = []
        for target in values:
            if target["kind"] != "table": continue
            try: await service.authorize(user, target["artifact_id"])
            except ArtifactError: continue
            columns = await service.artifacts.list_columns(user=user, artifact_id=target["artifact_id"])
            items.append({"artifact_id": target["artifact_id"], "title": target["title"], "columns": columns})
        return {"items": items}
    except ArtifactError as exc: raise _map_error(exc) from None


@router.get("/{artifact_id}/pipeline/runs/{run_id}")
async def table_pipeline_snapshot(
    artifact_id: str, run_id: str, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=50),
    filters: str | None = Query(None, max_length=10000),
    user: dict = Depends(current_user), service=Depends(get_pipeline_service),
):
    try:
        import json
        try: conditions = json.loads(filters) if filters else None
        except ValueError: raise InvalidOperation("筛选条件格式错误") from None
        if conditions is not None and (not isinstance(conditions, list) or any(not isinstance(f, dict) for f in conditions)):
            raise InvalidOperation("筛选条件格式错误")
        return await service.page(user, artifact_id, run_id, page, size, conditions)
    except ArtifactError as exc:
        raise _map_error(exc) from None

#: multipart 开销预算（对齐 auth_guard 用户导入预检口径）。
_UPLOAD_BODY_BUDGET = MAX_UPLOAD_BYTES + 256 * 1024


# ----------------------------------------------------------------- models

class ArtifactCreate(BaseModel):
    title: str
    description: str | None = None
    domain_id: str | None = Field(default=None, max_length=128)
    visibility: str = Field(default="private", pattern="^(private|public)$")


class ArtifactMetaPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    usage_note: str | None = None
    visibility: str | None = Field(default=None, pattern="^(private|public)$")
    expected_updated_at: str | None = None


class PublishReq(BaseModel):
    change_summary: str = ""
    expected_updated_at: str | None = None


class RevisionActionReq(BaseModel):
    expected_updated_at: str | None = None


class MemberAdd(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: str = Field(default="viewer", pattern="^(owner|editor|viewer)$")


class MemberPatch(BaseModel):
    role: str = Field(pattern="^(owner|editor|viewer)$")


class ImportReq(BaseModel):
    preview_token: str = Field(min_length=8, max_length=64)
    plan: dict[str, Any]
    conflict_policy: dict[str, Any] | str | None = None


class ColumnAdd(BaseModel):
    column_name: str = Field(min_length=1, max_length=200)
    column_type: str = "text"
    enum_values: list[str] | None = None
    is_indexed: bool = False
    expected_updated_at: str | None = None


class ColumnPatch(BaseModel):
    column_name: str | None = Field(default=None, min_length=1, max_length=200)
    column_type: str | None = None
    enum_values: list[str] | None = None
    is_indexed: bool | None = None
    status: str | None = Field(default=None, pattern="^dropped$")
    expected_updated_at: str | None = None


class PrimaryKeyReq(BaseModel):
    column_ids: list[str] = Field(min_length=0)
    expected_updated_at: str | None = None


class CellPatch(BaseModel):
    column: str = Field(min_length=1)
    value: Any = None
    expected_updated_at: str | None = None


class BatchAssignReq(BaseModel):
    column: str = Field(min_length=1)
    value: Any = None
    where: list[dict[str, Any]] = Field(default_factory=list)
    expected_updated_at: str | None = None


class RowInsertReq(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    expected_updated_at: str | None = None


class RowDeleteReq(BaseModel):
    expected_updated_at: str | None = None


# ---------------------------------------------------------------- helpers

def _map_error(exc: ArtifactError) -> HTTPException:
    if isinstance(exc, ArtifactImportError):
        if exc.code == "file_too_large":
            return HTTPException(413, "file_too_large")
        if exc.code == "preview_expired":
            return HTTPException(410, "preview_expired")
        if exc.code == "preview_limit":
            return HTTPException(
                429, "preview_limit: 预览登记过多（每用户 ≤5），已清最旧，请重试",
            )
        return HTTPException(422, exc.code)
    if isinstance(exc, ArtifactNotFound):
        return HTTPException(404, str(exc) or "not_found")
    if isinstance(exc, ArtifactForbidden):
        return HTTPException(403, str(exc) or "forbidden")
    if isinstance(exc, UsageNoteRequired):
        return HTTPException(422, "usage_note_required: 发布前必须填写使用说明")
    if isinstance(exc, NoPendingChanges):
        return HTTPException(409, {"code": "no_pending_changes",
                                   "message": "无待发布变更（草稿已关闭，请先编辑检出草稿）"})
    if isinstance(exc, PrimaryKeyConflict):
        return HTTPException(409, {"code": "primary_key_conflict",
                                   "message": str(exc)})
    if isinstance(exc, StaleArtifact):
        return HTTPException(409, {"code": "stale_artifact",
                                   "message": "制品已被他人修改，请刷新后重试"})
    if isinstance(exc, InvalidOperation):
        if str(exc) == "artifact_not_deprecated":
            return HTTPException(409, {
                "code": "artifact_not_deprecated",
                "message": "必须先停用（deprecate）制品才能删除——删除是级联硬删且不可恢复",
            })
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


def _check_upload_size(request: Request, file: UploadFile) -> None:
    """content-length 预检（对齐 api/auth_guard.py 用户导入口径）+ 实读兜底。"""
    raw_length = request.headers.get("content-length", "").strip()
    if raw_length:
        try:
            if int(raw_length) > _UPLOAD_BODY_BUDGET:
                raise HTTPException(413, "file_too_large")
        except ValueError:
            raise HTTPException(400, "invalid_content_length") from None
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file_too_large")


async def _require_editor(
    svc: ArtifactService, user: dict[str, Any], artifact_id: str,
) -> None:
    """向导三端点共用的设计态门禁（owner/editor；非成员 404 反探测）。"""
    try:
        await svc.list_columns(user=user, artifact_id=artifact_id)
    except ArtifactError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------ 制品 CRUD

@router.post("", status_code=201)
async def create_artifact(
    body: ArtifactCreate,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    adb: ArtifactDB = Depends(get_artifact_db),
):
    """domain_id 缺省回落用户首个绑定域；site admin 再回落 registry 默认域。
    域门禁（批次2a 疑虑2）：site admin / 域 admin / 该域 user_domains 绑定。"""
    user_id = str(user["id"])
    domain_id = (body.domain_id or "").strip()
    if not domain_id:
        domain_id = await adb.first_domain_for_user(user_id)
        if not domain_id and user.get("site_role") == "admin":
            domain_id = get_default_domain()
    if not domain_id:
        raise HTTPException(422, "domain_id_required: 未提供 domain_id 且用户无绑定域")
    try:
        resolve_domain(domain_id)
    except Exception as exc:
        raise HTTPException(400, f"invalid_domain:{domain_id}") from exc
    if not await adb.can_create_in_domain(user_id, domain_id):
        raise HTTPException(403, f"domain_not_bound:{domain_id}")
    try:
        return await svc.create_artifact(
            user=user, domain_id=domain_id, kind="table",
            title=body.title, description=body.description,
            visibility=body.visibility,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("")
async def list_artifacts(
    domain_id: str | None = Query(default=None),
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return {"artifacts": await svc.list_artifacts(
            user=user, domain_id=domain_id)}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """详情 = 元信息 + schema（设计态优先，viewer 回落发布快照）+ 采样行 5。"""
    try:
        artifact = await svc.get_artifact(user=user, artifact_id=artifact_id)
        try:
            schema = await svc.list_columns(user=user, artifact_id=artifact_id)
            rows = await svc.list_rows(
                user=user, artifact_id=artifact_id, draft=True, page=1, size=5)
        except ArtifactForbidden:  # viewer：运行态视图
            schema = await svc.list_columns(
                user=user, artifact_id=artifact_id, runtime=True)
            rows = await svc.list_rows(
                user=user, artifact_id=artifact_id, page=1, size=5)
        return {**artifact, "schema": schema, "sample_rows": rows["rows"]}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.patch("/{artifact_id}")
async def update_artifact_meta(
    artifact_id: str,
    body: ArtifactMetaPatch,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    fields = body.model_dump(exclude_unset=True)
    expected = fields.pop("expected_updated_at", None)
    try:
        return await svc.update_meta(
            user=user, artifact_id=artifact_id, fields=fields,
            expected_updated_at=expected,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.delete("/{artifact_id}")
async def delete_artifact(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        await svc.delete_artifact(user=user, artifact_id=artifact_id)
        return {"deleted": True}
    except ArtifactError as exc:
        raise _map_error(exc) from None


# -------------------------------------------------------------- 生命周期

@router.post("/{artifact_id}/publish")
async def publish_artifact(
    artifact_id: str,
    body: PublishReq,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.publish(
            user=user, artifact_id=artifact_id,
            change_summary=body.change_summary,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/rollback/{revision}")
async def rollback_artifact(
    artifact_id: str,
    revision: int = Path(..., ge=1),
    body: RevisionActionReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.rollback_to(
            user=user, artifact_id=artifact_id, revision=revision,
            expected_updated_at=(body or RevisionActionReq()).expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/deprecate")
async def deprecate_artifact(
    artifact_id: str,
    body: RevisionActionReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.deprecate(
            user=user, artifact_id=artifact_id,
            expected_updated_at=(body or RevisionActionReq()).expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/republish")
async def republish_artifact(
    artifact_id: str,
    body: RevisionActionReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.republish(
            user=user, artifact_id=artifact_id,
            expected_updated_at=(body or RevisionActionReq()).expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/discard")
async def discard_draft(
    artifact_id: str,
    body: RevisionActionReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.discard_draft(
            user=user, artifact_id=artifact_id,
            expected_updated_at=(body or RevisionActionReq()).expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/diff")
async def diff_draft(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.diff_draft_vs_current(user=user, artifact_id=artifact_id)
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/revisions")
async def list_revisions(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    adb: ArtifactDB = Depends(get_artifact_db),
):
    """版本历史（成员任一角色可读——运行态信息，对齐 batches 端点口径）。"""
    try:
        await svc.get_artifact(user=user, artifact_id=artifact_id)
        rows = await adb.list_revisions(artifact_id)
        return {"revisions": [{**r, "artifact_id": artifact_id} for r in rows]}
    except ArtifactError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------------ 成员

@router.get("/{artifact_id}/members")
async def list_members(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return {"members": await svc.list_members(
            user=user, artifact_id=artifact_id)}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/members", status_code=201)
async def add_member(
    artifact_id: str,
    body: MemberAdd,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.upsert_member(
            user=user, artifact_id=artifact_id,
            member_user_id=body.user_id, role=body.role,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.patch("/{artifact_id}/members/{user_id}")
async def update_member(
    artifact_id: str,
    user_id: str,
    body: MemberPatch,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.upsert_member(
            user=user, artifact_id=artifact_id,
            member_user_id=user_id, role=body.role,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.delete("/{artifact_id}/members/{user_id}")
async def remove_member(
    artifact_id: str,
    user_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        await svc.remove_member(
            user=user, artifact_id=artifact_id, member_user_id=user_id)
        return {"removed": True}
    except ArtifactError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------ 导入向导

@router.post("/{artifact_id}/table/upload", status_code=202)
async def upload_table(
    artifact_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    imp: ArtifactImportService = Depends(get_artifact_import_service),
):
    """上传解析登记绑定 artifact_id+uploader（修复⑬）；openpyxl 全量扫描经
    anyio.to_thread 移出事件循环（修复⑮）。"""
    await _require_editor(svc, user, artifact_id)
    _check_upload_size(request, file)
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "unsupported_file_type: 仅支持 .xlsx")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return await anyio.to_thread.run_sync(
            lambda: imp.parse_upload(
                content, filename,
                artifact_id=artifact_id, uploader=str(user["id"]),
            ),
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/table/preview/{preview_token}")
async def get_table_preview(
    artifact_id: str,
    preview_token: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    imp: ArtifactImportService = Depends(get_artifact_import_service),
):
    await _require_editor(svc, user, artifact_id)
    try:
        return imp.get_preview(
            preview_token,
            artifact_id=artifact_id, uploader=str(user["id"]),
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/table/import")
async def import_table(
    artifact_id: str,
    body: ImportReq,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    imp: ArtifactImportService = Depends(get_artifact_import_service),
):
    """plan 两种形态：build_mapping 产物（含 mode/columns），或原始
    {sheet, header_row, column_ops}——后者本层补跑 build_mapping（含
    missing_columns 与映射校验，openpyxl 表头重读经 anyio.to_thread 移出
    事件循环），再进 execute_import（token 绑定在服务内校验，失配 404）。"""
    try:
        plan = dict(body.plan)
        if "column_ops" in plan:
            target_columns = await svc.list_columns(
                user=user, artifact_id=artifact_id)
            plan = await anyio.to_thread.run_sync(
                lambda: imp.build_mapping(
                    body.preview_token,
                    sheet=plan.get("sheet"),
                    header_row=int(plan.get("header_row") or 1),
                    column_ops=plan["column_ops"],
                    target_columns=target_columns,
                    artifact_id=artifact_id, uploader=str(user["id"]),
                ),
            )
        return await imp.execute_import(
            svc, user=user, artifact_id=artifact_id,
            preview_token=body.preview_token, plan=plan,
            conflict_policy=body.conflict_policy,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/batches")
async def list_import_batches(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
    adb: ArtifactDB = Depends(get_artifact_db),
):
    """导入批次历史（成员任一角色可读——导入账属制品元信息）。"""
    try:
        await svc.get_artifact(user=user, artifact_id=artifact_id)
        return {"batches": await adb.list_batches(artifact_id)}
    except ArtifactError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------ 列定义

@router.get("/{artifact_id}/table/columns")
async def list_columns(
    artifact_id: str,
    include_dropped: bool = Query(default=False),
    runtime: bool = Query(default=False),
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """runtime=true → 发布定格快照列（revisions.schema_json，viewer+ 可读，
    codex二审功能项②：运行态表头与设计态草稿解耦）。"""
    try:
        return {"columns": await svc.list_columns(
            user=user, artifact_id=artifact_id,
            include_dropped=include_dropped, runtime=runtime)}
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/table/columns", status_code=201)
async def add_column(
    artifact_id: str,
    body: ColumnAdd,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    fields: dict[str, Any] = {
        "column_name": body.column_name,
        "column_type": body.column_type,
        "is_indexed": body.is_indexed,
    }
    if body.enum_values is not None:
        fields["enum_values"] = body.enum_values
    try:
        return await svc.upsert_column(
            user=user, artifact_id=artifact_id,
            column_id=f"c{uuid.uuid4().hex[:8]}",
            fields=fields,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.patch("/{artifact_id}/table/columns/{column_id}")
async def patch_column(
    artifact_id: str,
    column_id: str,
    body: ColumnPatch,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """改列名=service 层写 alias+同事务迁 rev0 data 键（§7.1 + codex二审
    批号②）；status=dropped=软删。"""
    fields = body.model_dump(exclude_unset=True)
    expected = fields.pop("expected_updated_at", None)
    status = fields.pop("status", None)
    try:
        if status == "dropped":
            await svc.drop_column(
                user=user, artifact_id=artifact_id, column_id=column_id,
                expected_updated_at=expected)
            return {"column_id": column_id, "status": "dropped"}
        columns = await svc.list_columns(
            user=user, artifact_id=artifact_id, include_dropped=True)
        if not any(c.get("column_id") == column_id for c in columns):
            raise HTTPException(404, f"column_not_found:{column_id}")
        return await svc.upsert_column(
            user=user, artifact_id=artifact_id, column_id=column_id,
            fields=fields, expected_updated_at=expected,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/table/primary-key")
async def set_primary_key(
    artifact_id: str,
    body: PrimaryKeyReq,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.set_primary_key(
            user=user, artifact_id=artifact_id, column_ids=body.column_ids,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


# ------------------------------------------------------------------ 行

@router.get("/{artifact_id}/table/rows")
async def list_rows(
    artifact_id: str,
    draft: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.list_rows(
            user=user, artifact_id=artifact_id,
            draft=draft, page=page, size=size,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/table/rows", status_code=201)
async def insert_row(
    artifact_id: str,
    body: RowInsertReq,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """手工行增（§7.2 行增）：无主键→row_key=uuid；有主键→拼主键，rev0 内
    冲突→409、主键值缺失→400。前端 dialog 引导走导入；此端点补齐后端能力面。"""
    try:
        return await svc.insert_single_row(
            user=user, artifact_id=artifact_id, data=body.data,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


# row_key 用 :path converter——hex 编码（codex三审 P1-1）后键恒为 [0-9a-f.]，
# 不会含 '/'，converter 保留无害；前端已 encodeURIComponent（hex 不被转义）。
@router.patch("/{artifact_id}/table/rows/{row_key:path}")
async def update_row_cell(
    artifact_id: str,
    row_key: str,
    body: CellPatch,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.update_cell(
            user=user, artifact_id=artifact_id, row_key=row_key,
            column=body.column, value=body.value,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/table/rows/batch-assign")
async def batch_assign(
    artifact_id: str,
    body: BatchAssignReq,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        return await svc.batch_assign(
            user=user, artifact_id=artifact_id, column=body.column,
            value=body.value, where_clauses=body.where,
            expected_updated_at=body.expected_updated_at,
        )
    except ArtifactError as exc:
        raise _map_error(exc) from None


@router.delete("/{artifact_id}/table/rows/{row_key:path}")
async def delete_row(
    artifact_id: str,
    row_key: str,
    body: RowDeleteReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: ArtifactService = Depends(get_artifact_service),
):
    try:
        await svc.delete_row(
            user=user, artifact_id=artifact_id, row_key=row_key,
            expected_updated_at=(body or RowDeleteReq()).expected_updated_at,
        )
        return {"deleted": True}
    except ArtifactError as exc:
        raise _map_error(exc) from None
