"""ExportService — 整库导出业务编排（权限 / 闸门 / 任务生命周期）.

语义：**异步打包下载通道**——copy_kb 超 MAX_COPY_DOCUMENTS 时的互补路径。
产物 = 原始文档 STORED zip + manifest.json 清单，落 staging 桶，presign
直连下载。执行在 services/export_tasks.py 的后台 worker。

错误族复用 kb_service（NotFound/Forbidden），新增 ExportDisabled(503)、
SourceEmptyExport/ExportTooLarge/ExportDiskFull(422)、ExportTaskConflict(409)
——route 层 _map_error 统一译 HTTP。

权限：创建/列表 = is_visible（导出是读操作）；详情/下载/删除 = 发起者或
域管理员（他人一律 404，不泄露存在性——项目铁律）。
"""
from __future__ import annotations

import shutil
from typing import Any

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_export import ExportDB
from knowledge_mining.mining.kb.services.export_tasks import (
    ExportTaskRepo, download_filename, request_cancel, start_export_task,
)
from knowledge_mining.mining.kb.services.kb_service import (
    KbError, NotFound,
)

#: 磁盘预检余量：put_stream 会在暂存文件旁再落一份副本（峰值≈2×包大小），
#: 在 2× 预估字节之外再留 1GB 给系统/并发上传。
_DISK_HEADROOM_BYTES = 1024 * 1024 * 1024


class ExportDisabled(KbError):
    """功能未启用（mining.yaml export.enabled=false）."""


class SourceEmptyExport(KbError):
    """源库无 active 文档——导出没有意义."""


class ExportTooLarge(KbError):
    """源库字节量超过配置上限（max_export_bytes，0=不限）."""


class ExportDiskFull(KbError):
    """暂存卷可用空间不足（峰值≈2×包大小 + 余量）."""


class ExportTaskConflict(KbError):
    """任务状态不满足当前操作（如未完成就取下载 URL）."""


class ExportService:
    def __init__(
        self, db: KbDB, export_db: ExportDB, *, object_store: Any,
        object_store_config: Any, config: Any, pool: Any,
    ) -> None:
        self._db = db
        self._export_db = export_db
        self._object_store = object_store
        self._object_store_config = object_store_config
        self._config = config
        self._pool = pool
        self._repo = ExportTaskRepo(pool)

    # ---------------------------------------------------------------- create

    async def create_export(self, *, kb_id: str, user_id: str) -> dict[str, Any]:
        """创建导出任务（202 秒回）。同库活跃任务幂等返回（already_started）."""
        if not getattr(self._config, "export_enabled", True):
            raise ExportDisabled("整库导出功能未启用（export.enabled=false）")

        kb = await self._db.get_kb(kb_id)
        if kb is None:
            raise NotFound(kb_id)
        # 读权限：看不到（他人 private / 软删）统一 404，不泄露存在性。
        if not await self._db.is_visible(kb_id=kb_id, user_id=user_id):
            raise NotFound(kb_id)

        # 幂等：同库已有排队/运行中任务 → 直接返回（防双击/多端重复打包）。
        active = await self._repo.get_active_task(kb_id)
        if active is not None:
            row = await self._repo.get_task(active["id"])
            if row is not None:
                row["already_started"] = True
                row["progress"] = _progress_of(row)
                return row

        n_docs = await self._db.count_active_documents_in_kb(kb_id)
        if n_docs == 0:
            raise SourceEmptyExport(
                "导出失败：源知识库没有可用文档（空库或全部已删除）")

        est_bytes = await self._export_db.estimate_bytes(kb_id)
        max_bytes = int(self._config.export_max_export_bytes)
        if max_bytes > 0 and est_bytes > max_bytes:
            raise ExportTooLarge(
                f"导出失败：源知识库预估 {est_bytes} 字节，超过上限 {max_bytes}")

        # 磁盘预检：峰值 ≈ 2×包大小（.part + put_stream 副本）+ 余量。
        try:
            self._config.tmp_root_path.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self._config.tmp_root_path).free
        except OSError as exc:
            raise ExportDiskFull(
                f"导出失败：暂存目录不可用（{self._config.tmp_root_path}）"
            ) from exc
        if free < est_bytes * 2 + _DISK_HEADROOM_BYTES:
            raise ExportDiskFull(
                f"导出失败：暂存卷可用空间不足（需 ≈2×{est_bytes} 字节 + "
                f"1GB 余量，当前可用 {free} 字节），请清理磁盘或联系管理员")

        task = await start_export_task(
            pool=self._pool, object_store=self._object_store,
            object_store_config=self._object_store_config,
            config=self._config, kb=kb, requested_by=str(user_id))
        task["already_started"] = False
        task["progress"] = {}
        return task

    # ------------------------------------------------------------------ read

    async def list_tasks(self, *, kb_id: str, user_id: str) -> list[dict[str, Any]]:
        """本库任务列表。域管理员看全部；普通用户只看自己发起的（对齐 purge-tasks）."""
        kb = await self._db.get_kb(kb_id)
        if kb is None or not await self._db.is_visible(
                kb_id=kb_id, user_id=user_id):
            raise NotFound(kb_id)
        rows = await self._repo.list_tasks(kb_id=kb_id)
        if await self._db.can_manage_domain(user_id=user_id, domain=kb["domain"]):
            return rows
        return [r for r in rows if r.get("requested_by") == str(user_id)]

    async def get_task(self, *, task_id: str, user_id: str) -> dict[str, Any]:
        row = await self._visible_task(task_id, user_id)
        row["progress"] = _progress_of(row)
        return row

    async def download_url(self, *, task_id: str, user_id: str) -> dict[str, Any]:
        """现签 presign 下载 URL（不落库，每次调用新签）.

        大包直连 MinIO：绕开 nginx/网关两跳的 300s 读超时，天然支持 Range
        断点续传。filename 经 response-content-disposition 给浏览器中文名。
        """
        from knowledge_mining.mining.contracts.storage.types import ObjectLocation

        row = await self._visible_task(task_id, user_id)
        if row["status"] != "succeeded":
            raise ExportTaskConflict(
                f"导出任务未完成（当前状态 {row['status']}），无法下载")
        if row.get("expires_at") is None or row["expires_at"] < _now_iso():
            raise ExportTaskConflict(
                "导出产物已过期，请重新发起导出")

        filename = download_filename(row.get("kb_name") or "knowledge-base",
                                     row.get("created_at"))
        access = await self._object_store.presign_get(
            ObjectLocation(
                bucket=row["artifact_bucket"],
                object_key=row["artifact_object_key"],
            ),
            expires_in_seconds=int(self._config.export_presign_ttl_seconds),
            filename=filename,
        )
        return {
            "url": access.url,
            "expires_in": access.expires_in_seconds,
            "filename": filename,
            "size": row.get("artifact_size"),
            "sha256": row.get("artifact_sha256"),
            "doc_count": row.get("doc_count"),
        }

    # ---------------------------------------------------------------- delete

    async def delete_task(self, *, task_id: str, user_id: str) -> dict[str, Any]:
        """运行中 → 请求取消（202）；终态 → 删产物对象 + 删任务行."""
        from knowledge_mining.mining.contracts.storage.types import ObjectLocation

        row = await self._visible_task(task_id, user_id)
        if row["status"] in ("queued", "running", "cancelling"):
            updated = await request_cancel(self._pool, task_id)
            return {"cancelled": True,
                    "task": _with_progress(updated or row)}
        if row.get("artifact_bucket") and row.get("artifact_object_key"):
            try:
                await self._object_store.delete(ObjectLocation(
                    bucket=row["artifact_bucket"],
                    object_key=row["artifact_object_key"]))
            except Exception:  # noqa: BLE001 - 对象缺失不阻塞删行
                pass
        await self._repo.delete_task(task_id)
        return {"deleted": True, "task_id": task_id}

    # ----------------------------------------------------------------- helpers

    async def _visible_task(self, task_id: str, user_id: str) -> dict[str, Any]:
        """详情/下载/删除的权限门：发起者或域管理员；他人统一 404."""
        row = await self._repo.get_task(task_id)
        if row is None:
            raise NotFound(task_id)
        if row.get("requested_by") == str(user_id):
            return row
        if await self._db.can_manage_domain(
                user_id=user_id, domain=row["domain"]):
            return row
        raise NotFound(task_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _progress_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("progress_json") if "progress_json" in row else row.get("progress")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def _with_progress(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["progress"] = _progress_of(row)
    row.pop("progress_json", None)
    return row


__all__ = [
    "ExportDisabled", "ExportDiskFull", "ExportService", "ExportTaskConflict",
    "ExportTooLarge", "SourceEmptyExport",
]
