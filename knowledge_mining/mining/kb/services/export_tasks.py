# -*- coding: utf-8 -*-
"""KB 整库导出任务执行器（打包下载通道）.

POST /api/kb/{id}/export 秒回 202 后 spawn 本执行器：keyset 分批遍历 active
文档 → MinIO/legacy 流式读取 → STORED ZIP64 本地暂存 → put_stream 直传
staging 桶（SDK 大文件自动分片）。进度实时落 ``kb_export_tasks``，前端轮询
渲染；产物 presign 现签现给（download-url 端点），URL 不落库。

重启恢复：``recover_export_tasks``（lifespan 调用）把 queued/running 的任务
删残留 .part 后重新入队——object_key 任务内固定，重跑覆盖即幂等。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 后台任务强引用（事件循环只留弱引用——同 purge_tasks 模式）.
_bg_tasks: set[asyncio.Task] = set()

#: task_id → 运行中的 Task（request_cancel 双保险：DB 置 cancelling + Task.cancel）.
_run_registry: dict[str, asyncio.Task] = {}

#: 全局打包并发闸门（磁盘/内存峰值与并发任务数成正比，默认 1 串行）.
_pack_semaphore: asyncio.Semaphore | None = None
_pack_semaphore_size = 0

#: 孤儿 .part 的兜底清理阈值（崩溃残留；正常路径 worker 自清）.
_ORPHAN_PART_MAX_AGE_SECONDS = 24 * 3600


class ExportCancelled(Exception):
    """任务被用户取消（DB cancelling 或 Task.cancel）."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_progress(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def download_filename(kb_name: str, created_at: str | None = None) -> str:
    """浏览器保存名：``<库名>-导出-YYYYMMDD-HHMM.zip``（去掉文件名非法字符）."""
    ts = created_at or _now()
    try:
        stamp = datetime.fromisoformat(ts).strftime("%Y%m%d-%H%M")
    except ValueError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    # 注意不能用 '\x00-\x1f' 字符串成员判断——'-' 是字面成员，会把库名里的
    # 连字符也换掉；控制字符按 ord 判。
    illegal = '<>:"/\\|?*'
    safe = "".join(
        c if c not in illegal and ord(c) >= 32 else "_" for c in kb_name)
    safe = safe.strip().rstrip(".") or "knowledge-base"
    return f"{safe}-导出-{stamp}.zip"


def artifact_object_key(kb_id: str, task_id: str) -> str:
    """系统对象键（无业务语义，任务内固定 → 重跑覆盖幂等）."""
    return f"export/{kb_id}/{task_id}.zip"


class ExportTaskRepo:
    """kb_export_tasks 仓储（与 KbDB 同池）."""

    def __init__(self, pool: Any):
        self._pool = pool

    async def insert_task(self, *, kb_id: str, kb_name: str, domain: str,
                          requested_by: str) -> dict[str, Any]:
        row = {"id": uuid.uuid4().hex, "status": "queued", "phase": "queued",
               "progress_json": {}, "error": None,
               "created_at": _now(), "updated_at": _now()}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_export_tasks
                     (id, kb_id, kb_name, domain, status, phase, progress_json,
                      requested_by, error, created_at, updated_at)
                   VALUES (%(id)s, %(kb_id)s, %(kb_name)s, %(domain)s,
                           %(status)s, %(phase)s, %(progress_json)s::jsonb,
                           %(requested_by)s, %(error)s,
                           %(created_at)s, %(updated_at)s)
                   RETURNING *""",
                {**row, "kb_id": kb_id, "kb_name": kb_name, "domain": domain,
                 "requested_by": requested_by, "progress_json": json.dumps({})},
            )
            return dict(await cur.fetchone())

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM kb_export_tasks WHERE id = %s", [task_id])
            r = await cur.fetchone()
            return dict(r) if r else None

    async def get_active_task(self, kb_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT * FROM kb_export_tasks
                   WHERE kb_id = %s
                     AND status IN ('queued', 'running', 'cancelling')
                   ORDER BY created_at DESC LIMIT 1""",
                [kb_id])
            r = await cur.fetchone()
            return dict(r) if r else None

    async def list_tasks(self, *, kb_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT * FROM kb_export_tasks WHERE kb_id = %s
                   ORDER BY created_at DESC LIMIT %s""", [kb_id, limit])
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["progress"] = _load_progress(r.pop("progress_json", None))
        return rows

    async def list_recoverable(self) -> list[dict[str, Any]]:
        # created_at 是 TEXT ISO（kb 族惯例）——下限在 Python 侧算同构字符串，
        # 避免与 PG timestamptz 直接比较报 UndefinedFunction。
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=7)).isoformat()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT * FROM kb_export_tasks
                   WHERE status IN ('queued', 'running', 'cancelling')
                     AND created_at > %s
                   LIMIT 100""", [cutoff])
            return [dict(r) for r in await cur.fetchall()]

    async def list_expired(self, now_iso: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT * FROM kb_export_tasks
                   WHERE status = 'succeeded' AND expires_at < %s
                   ORDER BY created_at LIMIT 200""", [now_iso])
            return [dict(r) for r in await cur.fetchall()]

    async def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        params = {**fields, "updated_at": _now(), "id": task_id}
        sets: list[str] = []
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                params[k] = json.dumps(v, ensure_ascii=False)
                sets.append(f"{k} = %({k})s::jsonb")
            else:
                sets.append(f"{k} = %({k})s")
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE kb_export_tasks SET {', '.join(sets)}, "
                "updated_at = %(updated_at)s WHERE id = %(id)s", params)

    async def delete_task(self, task_id: str) -> None:
        """物理删任务行（用户删除终态记录；产物对象由调用方先删）."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM kb_export_tasks WHERE id = %s", [task_id])


async def _get_pack_semaphore(config: Any) -> asyncio.Semaphore:
    global _pack_semaphore, _pack_semaphore_size
    size = max(1, int(config.export_max_concurrent_tasks))
    if _pack_semaphore is None or _pack_semaphore_size != size:
        _pack_semaphore = asyncio.Semaphore(size)
        _pack_semaphore_size = size
    return _pack_semaphore


def _legacy_path(upload_root: Path, kb_id: str, doc: dict[str, Any]) -> Path:
    """legacy 本地文档路径（无 storage_object_id 的旧文档）.

    storage_path 为上传时落盘的绝对路径（含 <kb_id> 前缀）；缺失时按目录
    结构构造。防穿越：解析结果必须仍在 ``upload_root/{kb_id}`` 下。
    """
    base = (upload_root / kb_id).resolve()
    raw = doc.get("storage_path")
    if raw:
        p = Path(raw)
        resolved = p.resolve() if p.is_absolute() else (base / p).resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            raise FileNotFoundError(
                f"legacy path escapes upload root: {raw}") from None
        return resolved
    segs = [s for s in (doc.get("directory_path") or "").split("/")
            if s and s not in (".", "..")]
    return base.joinpath(*segs, doc["document_name"])


async def _file_stream(path: Path):
    """本地文件 → async 字节流（put_stream 输入；1MB 块走 to_thread）."""
    with open(path, "rb") as fh:
        while True:
            chunk = await asyncio.to_thread(fh.read, 1024 * 1024)
            if not chunk:
                break
            yield chunk


def _file_sha256(path: Path) -> str:
    """整包 sha 以最终落盘的 zip 字节为准（packer 的增量 sha 只盖内容块，
    不含 zip 头/中央目录，不能当产物校验值）."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _assert_not_cancelling(repo: ExportTaskRepo, task_id: str) -> None:
    row = await repo.get_task(task_id)
    if row is not None and row["status"] == "cancelling":
        raise ExportCancelled(task_id)


async def _mark_terminal(repo: ExportTaskRepo, task_id: str, status: str,
                         error: str | None = None) -> None:
    try:
        await repo.update_task(task_id, status=status, error=error)
    except Exception:  # noqa: BLE001 - 终态回写失败只记日志
        logger.exception("[export-task] 终态回写失败: %s -> %s", task_id, status)


async def _run_export(*, task_row: dict[str, Any], pool: Any,
                      object_store: Any, object_store_config: Any,
                      config: Any) -> None:
    """打包管线主体：prepare → packaging（分批流式写 zip）→ uploading → finalize."""
    from knowledge_mining.mining.contracts.storage.types import (
        ObjectLocation, PutOptions,
    )
    from knowledge_mining.mining.file_management.repositories_pg import (
        PgStorageObjectRepository,
    )
    from knowledge_mining.mining.infra.upload_config import UploadConfig
    from knowledge_mining.mining.kb.db import KbDB
    from knowledge_mining.mining.kb.db_export import ExportDB
    from knowledge_mining.mining.kb.services.export_packager import ExportPackager

    task_id = task_row["id"]
    kb_id = task_row["kb_id"]
    repo = ExportTaskRepo(pool)
    object_key = artifact_object_key(kb_id, task_id)
    local = config.tmp_root_path / f"{task_id}.zip.part"
    progress: dict[str, Any] = {"total_docs": 0, "packed_docs": 0,
                                "packed_bytes": 0, "warnings_count": 0}
    try:
        await repo.update_task(task_id, status="running", phase="prepare",
                               progress_json=progress)
        async with await _get_pack_semaphore(config):
            export_db = ExportDB(pool)
            kbdb = KbDB(pool)
            storage_objects = PgStorageObjectRepository(pool)
            upload_root = UploadConfig().upload_root_path
            bucket = config.bucket_for(object_store_config)

            total = await kbdb.count_active_documents_in_kb(kb_id)
            if total == 0:
                raise RuntimeError(
                    "源知识库没有可用文档（空库或全部已删除），导出中止")
            folders = await export_db.list_folders(kb_id)
            refs = await export_db.list_referenced_documents(kb_id)
            progress["total_docs"] = total

            packer = ExportPackager(local)
            packer.open()
            try:
                entries: list[dict[str, Any]] = []
                warnings: list[dict[str, Any]] = []
                packed_bytes = 0
                last_key = ""
                last_flush = time.monotonic()
                last_flush_docs = 0
                # 读是严格串行的（v1 语义）：zip 条目按 document_key 序写入，
                # 单文档 MinIO 取流（先落临时文件再流回）已是流水线。
                while True:
                    await _assert_not_cancelling(repo, task_id)
                    batch = await export_db.iter_active_documents(
                        kb_id, after_key=last_key,
                        limit=int(config.export_batch_size),
                    )
                    if not batch:
                        break
                    for doc in batch:
                        last_key = doc["document_key"]
                        arc = packer.reserve_arcname(
                            doc.get("directory_path"), doc["document_name"])
                        try:
                            if doc.get("storage_object_id"):
                                rec = await storage_objects.get(
                                    doc["storage_object_id"])
                                if rec is None or rec.state != "AVAILABLE":
                                    raise FileNotFoundError(
                                        "storage object not available: "
                                        f"{doc['storage_object_id']} "
                                        f"(state={rec.state if rec else 'missing'})")
                                stream = object_store.get_stream(
                                    ObjectLocation(
                                        bucket=rec.bucket,
                                        object_key=rec.object_key,
                                        version_id=rec.object_version_id,
                                    ))
                                n = await packer.add_stream(arc, stream)
                                obj_sha: str | None = rec.sha256
                            else:
                                n = packer.add_file(
                                    arc, _legacy_path(upload_root, kb_id, doc))
                                obj_sha = doc.get("source_raw_hash")
                            entries.append({
                                "document_key": doc["document_key"],
                                "document_name": doc["document_name"],
                                "arcname": arc,
                                "size": n,
                                "sha256": obj_sha,
                                "content_revision": doc.get("content_revision"),
                                "modified_at": doc.get("modified_at"),
                            })
                            packed_bytes += n
                            progress["packed_docs"] = len(entries)
                            progress["packed_bytes"] = packed_bytes
                        except Exception as exc:  # noqa: BLE001 - 单文档降级
                            warnings.append({
                                "document_key": doc["document_key"],
                                "document_name": doc.get("document_name"),
                                "error": str(exc)[:200],
                            })
                            progress["warnings_count"] = len(warnings)
                        if (len(entries) - last_flush_docs
                                >= int(config.export_progress_interval_docs)
                                or time.monotonic() - last_flush
                                >= float(config.export_progress_interval_seconds)):
                            await repo.update_task(
                                task_id, phase="packaging",
                                progress_json=progress)
                            last_flush = time.monotonic()
                            last_flush_docs = len(entries)

                packer.write_manifest({
                    "format": "coremasterkb-kb-export",
                    "version": 1,
                    "kb": {"id": kb_id, "name": task_row["kb_name"],
                           "domain": task_row["domain"]},
                    "exported_at": _now(),
                    "generator": "coremasterkb",
                    "folders": folders,
                    "documents": entries,
                    "referenced_documents": refs,
                    "warnings": warnings,
                    "stats": {
                        "total_documents": total,
                        "packed_documents": len(entries),
                        "skipped_documents": len(warnings),
                        "packed_bytes": packed_bytes,
                    },
                })
            finally:
                packer.close()

            # 整包 sha 重读 .part 计算（zip 头/中央目录不在 packer 增量 sha 里）。
            zip_sha = await asyncio.to_thread(_file_sha256, local)
            await repo.update_task(task_id, phase="uploading",
                                   progress_json=progress)
            # 元数据 artifact_class 用 temporary（合法枚举；产物是临时导出物），
            # 物理桶由 bucket_for 决定（staging，容量复用）。
            result = await object_store.put_stream(
                ObjectLocation(bucket=bucket, object_key=object_key),
                _file_stream(local),
                PutOptions(
                    artifact_class="temporary", mime="application/zip",
                    expected_sha256=zip_sha,
                    metadata={"kb_id": kb_id, "task_id": task_id,
                              "kind": "kb-export"},
                ),
            )

        expires_at = (datetime.now(timezone.utc)
                      + timedelta(hours=int(config.export_retention_hours))).isoformat()
        final_progress = {**progress, "zip_size": result.size,
                          "zip_sha256": result.sha256}
        await repo.update_task(
            task_id, status="succeeded", phase="finalize", error=None,
            artifact_bucket=bucket, artifact_object_key=object_key,
            artifact_size=result.size, artifact_sha256=result.sha256,
            doc_count=len(entries), expires_at=expires_at,
            progress_json=final_progress)
        logger.info("[export-task] done: %s kb=%s docs=%s size=%s",
                    task_id, kb_id, len(entries), result.size)
    except (ExportCancelled, asyncio.CancelledError):
        await _mark_terminal(repo, task_id, "cancelled")
        logger.info("[export-task] cancelled: %s kb=%s", task_id, kb_id)
    except Exception as exc:  # noqa: BLE001 - 真实原因入 error 列
        logger.exception("[export-task] failed: %s kb=%s", task_id, kb_id)
        await _mark_terminal(repo, task_id, "failed", str(exc)[:2000])
    finally:
        try:
            local.unlink(missing_ok=True)
        except OSError:
            pass
        _run_registry.pop(task_id, None)


async def start_export_task(
    *, pool: Any, object_store: Any, object_store_config: Any, config: Any,
    kb: dict[str, Any], requested_by: str,
) -> dict[str, Any]:
    """创建任务并后台执行（调用方须已完成权限/闸门校验）."""
    repo = ExportTaskRepo(pool)
    task_row = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
        requested_by=requested_by)
    t = asyncio.create_task(_run_export(
        task_row=task_row, pool=pool, object_store=object_store,
        object_store_config=object_store_config, config=config))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    _run_registry[task_row["id"]] = t
    return task_row


async def recover_export_tasks(
    *, pool: Any, object_store: Any, object_store_config: Any, config: Any,
) -> int:
    """startup 恢复：cancelling 收口为 cancelled；queued/running 删残留后重跑."""
    repo = ExportTaskRepo(pool)
    rows = await repo.list_recoverable()
    recovered = 0
    for row in rows:
        if row["status"] == "cancelling":
            await repo.update_task(row["id"], status="cancelled")
            continue
        logger.info("[export-task] recover: %s kb=%s (state=%s)",
                    row["id"], row["kb_id"], row["status"])
        try:
            (config.tmp_root_path / f"{row['id']}.zip.part").unlink(
                missing_ok=True)
        except OSError:
            pass
        await repo.update_task(row["id"], status="queued", phase="queued",
                               progress_json={})
        t = asyncio.create_task(_run_export(
            task_row=row, pool=pool, object_store=object_store,
            object_store_config=object_store_config, config=config))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
        _run_registry[row["id"]] = t
        recovered += 1
    return recovered


async def sweep_expired_exports(
    *, pool: Any, object_store: Any, config: Any,
) -> dict[str, int]:
    """过期产物清理：删对象 + 置 expired（行保留供列表回看）.

    顺带清 tmp_root 下超龄孤儿 .part（崩溃残留兜底；正常路径 worker 自清）。
    """
    from knowledge_mining.mining.contracts.storage.types import ObjectLocation

    repo = ExportTaskRepo(pool)
    expired = 0
    for row in await repo.list_expired(_now()):
        if row.get("artifact_bucket") and row.get("artifact_object_key"):
            try:
                await object_store.delete(ObjectLocation(
                    bucket=row["artifact_bucket"],
                    object_key=row["artifact_object_key"]))
            except Exception:  # noqa: BLE001 - 对象已不存在等，行照常过期
                logger.warning("[export-sweep] object delete failed: %s",
                               row["artifact_object_key"], exc_info=True)
        await repo.update_task(row["id"], status="expired")
        expired += 1

    parts_cleaned = 0
    try:
        root = config.tmp_root_path
        root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _ORPHAN_PART_MAX_AGE_SECONDS
        for p in root.glob("*.part"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    parts_cleaned += 1
            except OSError:
                pass
    except OSError:
        logger.warning("[export-sweep] tmp root sweep failed", exc_info=True)

    if expired or parts_cleaned:
        logger.info("[export-sweep] expired=%s parts_cleaned=%s",
                    expired, parts_cleaned)
    return {"expired": expired, "parts_cleaned": parts_cleaned}


async def request_cancel(pool: Any, task_id: str) -> dict[str, Any] | None:
    """请求取消：DB 置 cancelling + 内存 Task.cancel（worker 在 await 点协作退出）.

    随后短等 worker 收口：若任务排队未启动即被取消（CancelledError 在协程体
    执行前送达，``_run_export`` 的 except 兜底不会运行），DB 会停在
    cancelling——这里直接收口为 cancelled，避免悬挂到下次重启恢复。
    """
    repo = ExportTaskRepo(pool)
    row = await repo.get_task(task_id)
    if row is None or row["status"] not in ("queued", "running", "cancelling"):
        return row
    if row["status"] != "cancelling":
        await repo.update_task(task_id, status="cancelling")
    t = _run_registry.get(task_id)
    if t is not None:
        t.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=2.0)
        except BaseException:  # noqa: BLE001 - Cancelled/Timeout/业务异常均为预期路径
            pass
    row = await repo.get_task(task_id)
    if row is not None and row["status"] == "cancelling":
        await repo.update_task(task_id, status="cancelled")
    return await repo.get_task(task_id)


__all__ = [
    "ExportCancelled", "ExportTaskRepo", "artifact_object_key",
    "download_filename", "recover_export_tasks", "request_cancel",
    "start_export_task", "sweep_expired_exports",
]
