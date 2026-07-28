"""KB management — async repository for kb_users / knowledge_bases / kb_members.

Used by the async FastAPI routes in mining/kb/routes/. Mirrors the codebase pattern:
raw parameterized SQL over the shared async pool (like knowledge.py routes), wrapped
in a thin repository for testability. Each method opens its own connection (one tx).

Style aligned with knowledge_mining/mining/infra/db.py (TEXT ids, ISO timestamps,
JSONB via ::jsonb cast, dict_row return).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    if obj is None:
        return "{}"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ── 文档状态派生 SQL 片段（alias d = asset_documents）────────────────────────
# CASE 在 SELECT；LATERAL 在 FROM。优先级与原 derive_document_status 一致：
# published > failed > mining(pending/processing) > withdrawn(removed) > uploaded。
# 折进列表/详情查询，避免对每个文档单独查（N+1，远程库下 2-3s 卡顿的根因）。
_STATUS_CASE_SQL = """CASE
    WHEN COALESCE(pub.published, FALSE) THEN 'published'
    WHEN rs.rd_status = 'failed' THEN 'failed'
    WHEN rs.rd_status IN ('pending', 'processing') THEN 'mining'
    WHEN COALESCE(rm.removed, FALSE) THEN 'withdrawn'
    ELSE 'uploaded'
END"""

_STATUS_JOIN_SQL = """
LEFT JOIN LATERAL (
    SELECT r.status AS rd_status
    FROM mining_run_documents r
    WHERE r.document_key = d.document_key
    ORDER BY r.finished_at DESC NULLS LAST, r.started_at DESC NULLS LAST, r.id DESC
    LIMIT 1
) rs ON TRUE
LEFT JOIN LATERAL (
    SELECT EXISTS (
        SELECT 1 FROM asset_publish_releases rel
        JOIN asset_build_document_snapshots bs ON bs.build_id = rel.build_id
        WHERE rel.domain = d.domain AND rel.status = 'active'
          AND bs.document_id = d.id AND bs.selection_status = 'active'
    ) AS published
) pub ON TRUE
LEFT JOIN LATERAL (
    SELECT EXISTS (
        SELECT 1 FROM asset_publish_releases rel
        JOIN asset_build_document_snapshots bs ON bs.build_id = rel.build_id
        WHERE rel.domain = d.domain AND rel.status = 'active'
          AND bs.document_id = d.id AND bs.selection_status = 'removed'
    ) AS removed
) rm ON TRUE"""


class KbDB:
    """Async repository over kb_users / knowledge_bases / kb_members.

    Constructed with a psycopg AsyncConnectionPool opened with row_factory=dict_row.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ---------------------------------------------------------------- users

    async def upsert_user_by_username(
        self, username: str, *, display_name: str | None = None
    ) -> dict[str, Any]:
        """Idempotent user upsert by username (Phase 1: header-injected identity)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_users (id, username, display_name, status, created_at)
                   VALUES (%(id)s, %(u)s, %(d)s, 'active', %(t)s)
                   ON CONFLICT (username) DO UPDATE
                     SET display_name = COALESCE(%(d)s, kb_users.display_name)
                   RETURNING id, username, display_name, status""",
                {"id": _new_id(), "u": username, "d": display_name, "t": _utcnow()},
            )
            row = await cur.fetchone()
            return dict(row)  # type: ignore[arg-type]

    # -------------------------------------------------------- knowledge bases

    async def create_kb(
        self,
        *,
        domain: str,
        name: str,
        owner_id: str,
        visibility: str = "private",
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO knowledge_bases
                     (id, domain, name, description, owner_id, visibility, status,
                      metadata_json, created_at, updated_at)
                   VALUES
                     (%(id)s, %(dom)s, %(n)s, %(desc)s, %(own)s, %(vis)s, 'active',
                      %(meta)s::jsonb, %(t)s, %(t)s)
                   RETURNING id, domain, name, description, owner_id, visibility,
                             status, created_at, updated_at""",
                {
                    "id": _new_id(), "dom": domain, "n": name, "desc": description,
                    "own": owner_id, "vis": visibility, "meta": _json(metadata), "t": _utcnow(),
                },
            )
            row = await cur.fetchone()
            return dict(row)  # type: ignore[arg-type]

    async def get_kb(self, kb_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        clause = "" if include_deleted else " AND status = 'active'"
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, domain, name, description, owner_id, visibility, status,
                          deleted_at, created_at, updated_at
                   FROM knowledge_bases WHERE id = %s""" + clause,
                [kb_id],
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_visible(self, *, user_id: str, domain: str) -> list[dict[str, Any]]:
        """KBs visible to user in domain: owned + member + public, status='active'.

        附带 my_role（owner/editor/viewer 有效访问级别）与 document_count（KB 内文档数），
        供列表页一次拿全、免 N+1。my_role 语义：owner 优先；否则 editor 成员；否则 viewer
        （含 viewer 成员与 public 的非成员读者——都是「只读有效角色」）。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT kb.id, kb.domain, kb.name, kb.description,
                          kb.owner_id, kb.visibility, kb.created_at,
                          CASE
                            WHEN kb.owner_id = %(uid)s THEN 'owner'
                            WHEN EXISTS (SELECT 1 FROM kb_members m
                                         WHERE m.kb_id = kb.id AND m.user_id = %(uid)s
                                           AND m.role = 'editor') THEN 'editor'
                            ELSE 'viewer'
                          END AS my_role,
                          (SELECT COUNT(*) FROM asset_documents d
                           WHERE d.kb_id = kb.id) AS document_count
                   FROM knowledge_bases kb
                   WHERE kb.domain = %(dom)s AND kb.status = 'active'
                     AND (kb.owner_id = %(uid)s
                          OR kb.visibility = 'public'
                          OR EXISTS (SELECT 1 FROM kb_members m
                                     WHERE m.kb_id = kb.id AND m.user_id = %(uid)s))
                   ORDER BY kb.created_at DESC""",
                {"uid": user_id, "dom": domain},
            )
            return [dict(r) for r in await cur.fetchall()]

    async def update_kb(
        self,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        params: dict[str, Any] = {"id": kb_id, "t": _utcnow()}
        if name is not None:
            fields.append("name = %(n)s")
            params["n"] = name
        if description is not None:
            fields.append("description = %(desc)s")
            params["desc"] = description
        if visibility is not None:
            fields.append("visibility = %(vis)s")
            params["vis"] = visibility
        if not fields:
            return await self.get_kb(kb_id)
        fields.append("updated_at = %(t)s")
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE knowledge_bases SET """ + ", ".join(fields) + """
                   WHERE id = %(id)s AND status = 'active'
                   RETURNING id, domain, name, description, owner_id, visibility, status""",
                params,
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def soft_delete(self, kb_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE knowledge_bases
                   SET status = 'deleted', deleted_at = %(t)s, updated_at = %(t)s
                   WHERE id = %(id)s AND status = 'active'
                   RETURNING id, status, deleted_at""",
                {"id": kb_id, "t": _utcnow()},
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    # ---------------------------------------------------------------- members

    async def add_member(self, *, kb_id: str, user_id: str, role: str = "viewer") -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_members (kb_id, user_id, role, added_at)
                   VALUES (%(kb)s, %(u)s, %(r)s, %(t)s)
                   ON CONFLICT (kb_id, user_id) DO UPDATE SET role = EXCLUDED.role
                   RETURNING kb_id, user_id, role, added_at""",
                {"kb": kb_id, "u": user_id, "r": role, "t": _utcnow()},
            )
            row = await cur.fetchone()
            return dict(row)  # type: ignore[arg-type]

    async def list_members(self, kb_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT m.kb_id, m.user_id, m.role, m.added_at, u.username, u.display_name
                   FROM kb_members m JOIN kb_users u ON u.id = m.user_id
                   WHERE m.kb_id = %s ORDER BY m.added_at""",
                [kb_id],
            )
            return [dict(r) for r in await cur.fetchall()]

    async def remove_member(self, *, kb_id: str, user_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM kb_members WHERE kb_id = %s AND user_id = %s",
                [kb_id, user_id],
            )

    # ------------------------------------------------------------- visibility

    async def is_visible(self, *, kb_id: str, user_id: str) -> bool:
        """True iff user can read this KB (owner / member / public) and KB is active."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM knowledge_bases kb
                   WHERE kb.id = %s AND kb.status = 'active'
                     AND (kb.owner_id = %s
                          OR kb.visibility = 'public'
                          OR EXISTS (SELECT 1 FROM kb_members m
                                     WHERE m.kb_id = kb.id AND m.user_id = %s))""",
                [kb_id, user_id, user_id],
            )
            return (await cur.fetchone()) is not None

    async def can_write(self, *, kb_id: str, user_id: str) -> bool:
        """True iff user can write this KB (owner, or editor member) and KB is active."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM knowledge_bases kb
                   WHERE kb.id = %s AND kb.status = 'active'
                     AND (kb.owner_id = %s
                          OR EXISTS (SELECT 1 FROM kb_members m
                                     WHERE m.kb_id = kb.id AND m.user_id = %s AND m.role = 'editor'))
                   """,
                [kb_id, user_id, user_id],
            )
            return (await cur.fetchone()) is not None

    # --------------------------------------------- documents (asset_documents identity)

    async def insert_document_identity(
        self, *, domain: str, kb_id: str, document_key: str, document_name: str,
        storage_path: str, directory_path: str | None = None,
        document_type: str | None = None, owner_id: str | None = None,
        file_size: int | None = None, modified_at: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """KB 上传：建文档身份行（不计算 hash、不建 snapshot——挖掘时才算）。

        写方归属：asset_documents 身份由 KB package 独占（设计铁律 1）。
        file_size / modified_at 由调用方 stat 落盘文件得到。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO asset_documents
                     (id, domain, document_key, document_name, document_type, metadata_json,
                      created_at, kb_id, storage_path, directory_path, owner_id,
                      file_size, modified_at)
                   VALUES
                     (%(id)s, %(dom)s, %(k)s, %(n)s, %(t)s, %(m)s::jsonb, %(now)s,
                      %(kb)s, %(sp)s, %(dp)s, %(own)s, %(fs)s, %(ma)s)
                   RETURNING id, domain, kb_id, document_key, document_name, document_type,
                             storage_path, directory_path, owner_id, created_at,
                             file_size, modified_at""",
                {
                    "id": _new_id(), "dom": domain, "k": document_key, "n": document_name,
                    "t": document_type, "m": _json(metadata), "now": _utcnow(),
                    "kb": kb_id, "sp": storage_path, "dp": directory_path, "own": owner_id,
                    "fs": file_size, "ma": modified_at,
                },
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def list_documents_in_kb(
        self, *, kb_id: str, directory: str | None = None,
        limit: int = 200, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列 KB 内文档，**状态内联派生**（一次 SQL，避免 N+1 远程查询）。

        状态优先级与原 derive_document_status 一致：published > failed > mining > withdrawn > uploaded。
        """
        clause = "d.kb_id = %s"
        params: list[Any] = [kb_id]
        if directory is not None:
            clause += " AND d.directory_path = %s"
            params.append(directory)
        params.extend([limit, offset])
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT d.id, d.domain, d.kb_id, d.document_key, d.document_name,
                           d.document_type, d.storage_path, d.directory_path, d.owner_id,
                           d.created_at, d.file_size, d.modified_at,
                           {_STATUS_CASE_SQL} AS status
                    FROM asset_documents d
                    {_STATUS_JOIN_SQL}
                    WHERE {clause}
                    ORDER BY d.created_at DESC LIMIT %s OFFSET %s""",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_document_identity(self, document_id: str) -> dict[str, Any] | None:
        """单文档身份 + 内联派生状态（一次 SQL）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT d.id, d.domain, d.kb_id, d.document_key, d.document_name,
                           d.document_type, d.storage_path, d.directory_path, d.owner_id,
                           d.metadata_json, d.created_at, d.file_size, d.modified_at,
                           {_STATUS_CASE_SQL} AS status
                    FROM asset_documents d
                    {_STATUS_JOIN_SQL}
                    WHERE d.id = %s""",
                [document_id],
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete_document_identity(self, document_id: str) -> None:
        """删文档身份行。FK CASCADE 自动清 asset_document_snapshot_links 与
        asset_build_document_snapshots；共享 snapshot 保留（别的文档可能引用）。"""
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM asset_documents WHERE id = %s", [document_id])

    async def update_document_identity(
        self, document_id: str, *,
        document_name: str | None = None, document_type: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        params: dict[str, Any] = {"id": document_id}
        if document_name is not None:
            fields.append("document_name = %(n)s")
            params["n"] = document_name
        if document_type is not None:
            fields.append("document_type = %(t)s")
            params["t"] = document_type
        if not fields:
            return await self.get_document_identity(document_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE asset_documents SET " + ", ".join(fields) + " WHERE id = %(id)s "
                "RETURNING id, document_key, document_name, document_type, storage_path, directory_path",
                params,
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def derive_document_status(self, document_id: str) -> str:
        """单文档状态派生（保留作单点查询；列表/详情已用内联 _STATUS_CASE_SQL 一次取齐）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT {_STATUS_CASE_SQL} AS status
                    FROM asset_documents d
                    {_STATUS_JOIN_SQL}
                    WHERE d.id = %s""",
                [document_id],
            )
            row = await cur.fetchone()
            return row["status"] if row else "unknown"

    # ------------------------------------------------------------- folders (kb_folders)

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, kb_id, parent_id, name, path, created_at, created_by
                   FROM kb_folders WHERE id = %s""",
                [folder_id],
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_folders(self, kb_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, kb_id, parent_id, name, path, created_at, created_by
                   FROM kb_folders WHERE kb_id = %s ORDER BY path""",
                [kb_id],
            )
            return [dict(r) for r in await cur.fetchall()]

    async def find_folder_by_parent(
        self, *, kb_id: str, parent_id: str | None, name: str
    ) -> dict[str, Any] | None:
        """同父同名查找（parent_id NULL 用 IS NOT DISTINCT FROM 视作相等）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, kb_id, parent_id, name, path FROM kb_folders
                   WHERE kb_id = %s AND parent_id IS NOT DISTINCT FROM %s AND name = %s""",
                [kb_id, parent_id, name],
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def insert_folder(
        self, *, folder_id: str, kb_id: str, parent_id: str | None, name: str,
        path: str, created_by: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_folders (id, kb_id, parent_id, name, path, created_at, created_by)
                   VALUES (%(id)s, %(kb)s, %(p)s, %(n)s, %(path)s, %(t)s, %(cb)s)
                   RETURNING id, kb_id, parent_id, name, path, created_at, created_by""",
                {"id": folder_id, "kb": kb_id, "p": parent_id, "n": name,
                 "path": path, "t": _utcnow(), "cb": created_by},
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def count_child_folders(self, *, kb_id: str, parent_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM kb_folders WHERE kb_id = %s AND parent_id = %s",
                [kb_id, parent_id],
            )
            return int((await cur.fetchone())["n"])

    async def count_docs_under_path(self, *, kb_id: str, path: str) -> int:
        """统计某文件夹下（含子文件夹）的文档数。path 非空（根删除由上层处理）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT COUNT(*) AS n FROM asset_documents
                   WHERE kb_id = %s
                     AND (directory_path = %s OR directory_path LIKE %s)""",
                [kb_id, path, path + "/%"],
            )
            return int((await cur.fetchone())["n"])

    async def delete_folder_row(self, folder_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM kb_folders WHERE id = %s", [folder_id])

    # -- folder move / rename (G3)：身份键不变，只改位置（path / directory_path / storage_path）--

    async def update_folder_name(self, folder_id: str, name: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE kb_folders SET name = %s WHERE id = %s", [name, folder_id],
            )

    async def set_folder_parent(self, folder_id: str, parent_id: str | None) -> None:
        """parent_id 为 None 表示移到根。path 由 rewrite_folder_subtree_paths 处理。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE kb_folders SET parent_id = %s WHERE id = %s", [parent_id, folder_id],
            )

    async def rewrite_folder_subtree_paths(
        self, *, kb_id: str, old_prefix: str, new_prefix: str,
    ) -> int:
        """把 path == old_prefix 或 LIKE 'old_prefix/%' 的文件夹 path 前缀替换为 new_prefix。

        返回受影响行数。path 是单列、纯前缀关系，SQL substr 重写安全。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_folders
                   SET path = %s || substr(path, %s)
                   WHERE kb_id = %s
                     AND (path = %s OR path LIKE %s)""",
                [new_prefix, len(old_prefix) + 1, kb_id, old_prefix, old_prefix + "/%"],
            )
            return cur.rowcount if hasattr(cur, "rowcount") else 0

    async def list_docs_under_prefix(self, *, kb_id: str, prefix: str) -> list[dict[str, Any]]:
        """列出身处某文件夹（含子文件夹）下的文档：directory_path = prefix 或 LIKE 'prefix/%'。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, document_name, directory_path, storage_path
                   FROM asset_documents
                   WHERE kb_id = %s
                     AND (directory_path = %s OR directory_path LIKE %s)""",
                [kb_id, prefix, prefix + "/%"],
            )
            return [dict(r) for r in await cur.fetchall()]

    async def update_doc_location(
        self, document_id: str, *, directory_path: str, storage_path: str,
    ) -> None:
        """移动/改名文件：更新位置两字段。document_key / id 不变。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE asset_documents SET directory_path = %s, storage_path = %s WHERE id = %s",
                [directory_path, storage_path, document_id],
            )
