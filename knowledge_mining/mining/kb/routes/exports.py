"""整库导出（打包下载）路由 — /api/kb.

异步任务语义：POST 秒回 202，进度轮询 GET …/export-tasks，完成后经
download-url 现签 presign 直连 MinIO 下载（大包绕开代理两跳）。

路径全部为两段以上（/{kb_id}/export、/export-tasks/{task_id}…），不与
kbs.py 的单段 GET /{kb_id} 冲突；但 /api/kb/export-tasks/{task_id} 首段
是字面量，注册顺序须先于 kb_router（app.py 静态先于动态铁律）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.deps import get_export_service
from knowledge_mining.mining.kb.services.export_service import (
    ExportDisabled, ExportDiskFull, ExportService, ExportTaskConflict,
    ExportTooLarge, SourceEmptyExport,
)
from knowledge_mining.mining.kb.services.kb_service import NotFound

router = APIRouter(prefix="/api/kb", tags=["kb-export"])


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(404, str(exc) or "not found")
    if isinstance(exc, ExportTaskConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, (SourceEmptyExport, ExportTooLarge, ExportDiskFull)):
        return HTTPException(422, str(exc))
    if isinstance(exc, ExportDisabled):
        return HTTPException(503, str(exc))
    return HTTPException(500, str(exc))


@router.post("/{kb_id}/export", status_code=202)
async def create_export(
    kb_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ExportService = Depends(get_export_service),
):
    """创建整库导出任务（打包下载通道；copy 超 5000 文档走这里）.

    同库已有排队/运行中任务时幂等返回（already_started=true）。
    """
    try:
        return await svc.create_export(kb_id=kb_id, user_id=user["id"])
    except (NotFound, SourceEmptyExport, ExportTooLarge, ExportDiskFull,
            ExportDisabled) as exc:
        raise _map_error(exc) from None


@router.get("/{kb_id}/export-tasks")
async def list_export_tasks(
    kb_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ExportService = Depends(get_export_service),
):
    """本库导出任务列表（域管理员看全部；普通用户只看自己发起的）."""
    try:
        return {"tasks": await svc.list_tasks(kb_id=kb_id, user_id=user["id"])}
    except NotFound as exc:
        raise _map_error(exc) from None


@router.get("/export-tasks/{task_id}")
async def get_export_task(
    task_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ExportService = Depends(get_export_service),
):
    """任务详情（进度/产物元数据；不含下载 URL——另走 download-url 现签）."""
    try:
        return await svc.get_task(task_id=task_id, user_id=user["id"])
    except NotFound as exc:
        raise _map_error(exc) from None


@router.get("/export-tasks/{task_id}/download-url")
async def get_export_download_url(
    task_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ExportService = Depends(get_export_service),
):
    """现签 presign 下载 URL（每次调用新签；仅 succeeded 且未过期）."""
    try:
        return await svc.download_url(task_id=task_id, user_id=user["id"])
    except (NotFound, ExportTaskConflict) as exc:
        raise _map_error(exc) from None


@router.delete("/export-tasks/{task_id}", status_code=200)
async def delete_export_task(
    task_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: ExportService = Depends(get_export_service),
):
    """运行中 → 请求取消（202）；终态 → 删产物对象 + 删任务记录（200）."""
    from fastapi.responses import JSONResponse

    try:
        result = await svc.delete_task(task_id=task_id, user_id=user["id"])
    except NotFound as exc:
        raise _map_error(exc) from None
    if result.get("cancelled"):
        return JSONResponse(status_code=202, content={
            "cancelled": True, "task": result.get("task")})
    return result
