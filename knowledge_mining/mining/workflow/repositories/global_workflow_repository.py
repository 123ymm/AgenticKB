from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb


class GlobalWorkflowRepository:
    """Persistence for global Workflow definitions in the primary Control store."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def list_workflows(self, *, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE status = 'active'"
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM mining_workflows {where} "
                "ORDER BY is_system_default DESC, updated_at DESC, name ASC"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_workflow(self, workflow_id: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM mining_workflows WHERE id = %s", (workflow_id,)
        )

    async def get_by_name(self, name: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM mining_workflows WHERE name = %s", (name,)
        )

    async def insert_workflow(self, record: dict) -> dict:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """INSERT INTO mining_workflows (
                       id, name, description, status, draft_graph_json,
                       draft_revision, current_version, is_system,
                       is_system_default, created_by, updated_by, metadata_json
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                   ) RETURNING *""",
                (
                    record["id"],
                    record["name"],
                    record.get("description"),
                    record.get("status", "active"),
                    Jsonb(record["draft_graph_json"]),
                    record.get("draft_revision", 0),
                    record.get("current_version"),
                    record.get("is_system", False),
                    record.get("is_system_default", False),
                    record.get("created_by"),
                    record.get("updated_by"),
                    Jsonb(record.get("metadata_json") or {}),
                ),
            )
            return dict(await cursor.fetchone())

    async def update_draft(
        self,
        workflow_id: str,
        *,
        graph: dict,
        expected_revision: int,
        updated_by: str | None,
    ) -> dict | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """UPDATE mining_workflows
                   SET draft_graph_json = %s,
                       draft_revision = draft_revision + 1,
                       updated_by = %s,
                       updated_at = NOW()
                   WHERE id = %s
                     AND draft_revision = %s
                     AND status = 'active'
                   RETURNING *""",
                (Jsonb(graph), updated_by, workflow_id, expected_revision),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def insert_version_and_advance(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        version_record: dict,
    ) -> dict | None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    "SELECT * FROM mining_workflows WHERE id = %s FOR UPDATE",
                    (workflow_id,),
                )
                workflow = await cursor.fetchone()
                if (
                    workflow is None
                    or workflow["status"] != "active"
                    or workflow["draft_revision"] != expected_revision
                ):
                    return None
                next_version = (workflow["current_version"] or 0) + 1
                if version_record["version"] != next_version:
                    return None
                cursor = await conn.execute(
                    """INSERT INTO mining_workflow_versions (
                           id, workflow_id, version, graph_json,
                           compiled_manifest_json, graph_hash, schema_version,
                           operator_catalog_version, release_notes, created_by,
                           metadata_json
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                       ) RETURNING *""",
                    (
                        version_record["id"],
                        workflow_id,
                        next_version,
                        Jsonb(version_record["graph_json"]),
                        Jsonb(version_record["compiled_manifest_json"]),
                        version_record["graph_hash"],
                        version_record["schema_version"],
                        version_record["operator_catalog_version"],
                        version_record.get("release_notes"),
                        version_record.get("created_by"),
                        Jsonb(version_record.get("metadata_json") or {}),
                    ),
                )
                inserted = dict(await cursor.fetchone())
                await conn.execute(
                    """UPDATE mining_workflows
                       SET current_version = %s, updated_at = NOW()
                       WHERE id = %s""",
                    (next_version, workflow_id),
                )
                return inserted

    async def list_versions(self, workflow_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """SELECT * FROM mining_workflow_versions
                   WHERE workflow_id = %s ORDER BY version DESC""",
                (workflow_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_version(self, workflow_id: str, version: int) -> dict | None:
        return await self._fetch_one(
            """SELECT * FROM mining_workflow_versions
               WHERE workflow_id = %s AND version = %s""",
            (workflow_id, version),
        )

    async def archive(
        self, workflow_id: str, *, updated_by: str | None
    ) -> dict | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """UPDATE mining_workflows
                   SET status = 'archived', updated_by = %s, updated_at = NOW()
                   WHERE id = %s AND status = 'active'
                   RETURNING *""",
                (updated_by, workflow_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _fetch_one(self, query: str, params: tuple[Any, ...]) -> dict | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            return dict(row) if row else None
