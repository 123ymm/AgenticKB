"""ExportDB — 整库导出的读路径仓储（独立于 db.py，照 db_artifacts.py 先例）.

只做四件事：keyset 分批读 active 文档、目录树、字节量预估、跨库引用清单。
写路径（任务行）在 services/export_tasks.py 的 ExportTaskRepo。
"""
from __future__ import annotations

from typing import Any


class ExportDB:
    """导出读路径（与 KbDB 同池）."""

    def __init__(self, pool: Any):
        self._pool = pool

    async def iter_active_documents(
        self, kb_id: str, *, after_key: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        """keyset 分批读 active 文档（(kb_id, document_key) 唯一索引天然支持）.

        列与 copy_kb 的 SELECT 同源；多带 storage_path（legacy 本地回落）。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, document_key, document_name, document_type,
                          directory_path, folder_id, file_size, modified_at,
                          storage_object_id, source_raw_hash, content_revision,
                          storage_path, owner_id,
                          created_at::text AS created_at
                   FROM asset_documents
                   WHERE kb_id = %s AND deleted_at IS NULL
                     AND document_key > %s
                   ORDER BY document_key
                   LIMIT %s""",
                [kb_id, after_key, limit],
            )
            return [dict(r) for r in await cur.fetchall()]

    async def list_folders(self, kb_id: str) -> list[dict[str, Any]]:
        """目录树（manifest 还原目录结构用）."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, parent_id, name, path FROM kb_folders
                   WHERE kb_id = %s ORDER BY path""",
                [kb_id],
            )
            return [dict(r) for r in await cur.fetchall()]

    async def estimate_bytes(self, kb_id: str) -> int:
        """active 文档字节量预估（磁盘预检闸门用；旧文档 file_size 可能为 NULL）."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT COALESCE(SUM(COALESCE(file_size, 0)), 0)::bigint AS n
                   FROM asset_documents
                   WHERE kb_id = %s AND deleted_at IS NULL""",
                [kb_id],
            )
            return int((await cur.fetchone())["n"])

    async def list_referenced_documents(self, kb_id: str) -> list[dict[str, Any]]:
        """跨库引用文档清单（一张网公共库引用）：manifest 列出，不打包字节."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT r.document_id, r.created_by, r.created_at,
                          d.document_key, d.document_name, d.directory_path,
                          d.file_size
                   FROM kb_document_refs r
                   JOIN asset_documents d ON d.id = r.document_id
                   WHERE r.kb_id = %s AND d.deleted_at IS NULL
                   ORDER BY d.document_key""",
                [kb_id],
            )
            return [dict(r) for r in await cur.fetchall()]


__all__ = ["ExportDB"]
