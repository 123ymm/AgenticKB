"""Workbench persistence using existing runtime tables; no artifact row writes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from knowledge_mining.mining.workflow.artifact_tables import PROFILE, OPERATOR
from knowledge_mining.mining.workflow.manifest import value_hash
from knowledge_mining.mining.workflow.run_binding import WorkflowRunBinding
from knowledge_mining.mining.workflow.repositories.domain_run_repository import AsyncDomainRunRepository
from knowledge_mining.mining.workflow.repositories.global_workflow_repository import GlobalWorkflowRepository


class ArtifactPipelineRepository:
    def __init__(self, pool, runtime_pool):
        self.pool = pool
        self.runtime_pool = runtime_pool

    async def freeze_sources(self, kb_id: str, document_ids: list[str]) -> list[dict]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """SELECT d.id, d.document_name, d.directory_path, d.content_revision,
                          s.id AS snapshot_id, s.parse_ir_storage_object_id
                   FROM asset_documents d
                   LEFT JOIN LATERAL (
                       SELECT s.id, s.parse_ir_storage_object_id
                       FROM asset_document_snapshot_links l
                       JOIN asset_document_snapshots s ON s.id=l.document_snapshot_id
                       WHERE l.document_id=d.id AND s.domain=d.domain
                         AND s.lifecycle_status='READY'
                         AND (d.source_raw_hash IS NULL OR s.raw_content_hash=d.source_raw_hash)
                       ORDER BY l.linked_at DESC, s.id DESC LIMIT 1
                   ) s ON TRUE
                   WHERE d.kb_id=%s AND d.id=ANY(%s) AND d.deleted_at IS NULL
                   ORDER BY d.directory_path, d.document_name, d.id""",
                (kb_id, document_ids),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def table_data(self, snapshot_id: str) -> tuple[list[dict], list[dict]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM asset_structured_assets WHERE snapshot_id=%s "
                "AND asset_type='table' ORDER BY table_ref", (snapshot_id,),
            )
            tables = [dict(row) for row in await cursor.fetchall()]
            cursor = await conn.execute(
                "SELECT * FROM asset_table_cells WHERE snapshot_id=%s "
                "ORDER BY table_ref,row_index,column_index", (snapshot_id,),
            )
            return tables, [dict(row) for row in await cursor.fetchall()]

    async def enqueue(self, artifact: dict, user_id: str, kb_id: str, manifest: dict) -> str:
        run_id, workflow_id, version_id = (uuid.uuid4().hex for _ in range(3))
        metadata = {"profile": PROFILE, "artifact_id": artifact["artifact_id"],
                    "user_id": user_id, "kb_id": kb_id, "domain": artifact["domain_id"],
                    "parent_run_id": manifest.get("parent_run_id"),
                    "step_index": manifest.get("step_index", 1),
                    "operator": manifest["nodes"][0]["operatorType"]}
        workflows = GlobalWorkflowRepository(self.pool)
        await workflows.insert_workflow({
            "id": workflow_id, "name": f"artifact-table-{workflow_id}",
            "draft_graph_json": manifest, "created_by": user_id, "metadata_json": metadata,
        })
        graph_hash = value_hash(manifest)
        version = await workflows.insert_version_and_advance(
            workflow_id, expected_revision=0, version_record={
                "id": version_id, "version": 1, "graph_json": manifest,
                "compiled_manifest_json": manifest, "graph_hash": graph_hash,
                "schema_version": "1.0", "operator_catalog_version": "artifact-table@1",
                "created_by": user_id, "metadata_json": metadata,
            },
        )
        if not version:
            raise RuntimeError("无法冻结加工配置")
        await AsyncDomainRunRepository(self.runtime_pool).insert_queued_run(
            run_id=run_id, input_path=f"artifact://{artifact['artifact_id']}",
            domain=artifact["domain_id"], channel="artifact_table", execution_engine="workflow",
            binding=WorkflowRunBinding(workflow_id, 1, version_id, graph_hash, manifest),
            started_at=datetime.now(timezone.utc).isoformat(), metadata_json=metadata,
        )
        return run_id

    async def list_runs(self, artifact_id: str) -> list[dict]:
        async with self.runtime_pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT id,status,started_at,finished_at,error_summary,metadata_json "
                "FROM mining_runs WHERE metadata_json->>'profile'=%s "
                "AND metadata_json->>'artifact_id'=%s ORDER BY started_at DESC,id DESC",
                (PROFILE, artifact_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_run(self, artifact_id: str, run_id: str) -> dict | None:
        async with self.runtime_pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM mining_runs WHERE id=%s AND metadata_json->>'profile'=%s "
                "AND metadata_json->>'artifact_id'=%s", (run_id, PROFILE, artifact_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def publications(self, workflow_id):
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT metadata_json->'publications' AS publications FROM mining_workflows WHERE id=%s", (workflow_id,))
            row = await cursor.fetchone()
            return list((row["publications"] or {}).values()) if row else []

    async def claim(self, run_id: str, worker_id: str, operator: str = OPERATOR) -> str | None:
        event_id = uuid.uuid4().hex
        async with self.runtime_pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """UPDATE mining_runs SET status='running', current_stage='mining',
                           finished_at=NULL, worker_id=%s, lease_until=NOW()+interval '5 minutes',
                           active_node_id='step1', active_operator_type=%s
                       WHERE id=%s AND status IN ('queued','interrupted')
                         AND (lease_until IS NULL OR lease_until<NOW()) RETURNING id""",
                    (worker_id, operator, run_id),
                )
                if await cursor.fetchone() is None:
                    return None
                # An interrupted attempt is retained as failed; a new attempt has its own event.
                await conn.execute(
                    "UPDATE mining_workflow_node_events SET status='failed',finished_at=NOW(),"
                    "error_code='interrupted' WHERE run_id=%s AND status='started'", (run_id,),
                )
                await conn.execute(
                    """INSERT INTO mining_workflow_node_events
                       (id,run_id,node_id,operator_type,operator_version,status,attempt_no,
                        started_at,input_summary_json)
                       SELECT %s,%s,'step1',%s,'1','started',
                              COALESCE(MAX(attempt_no),0)+1,NOW(),%s
                       FROM mining_workflow_node_events WHERE run_id=%s AND node_id='step1'""",
                    (event_id, run_id, operator, Jsonb({"manifest_run_id": run_id}), run_id),
                )
        return event_id

    async def progress(self, run_id, worker_id, progress):
        async with self.runtime_pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE mining_runs SET metadata_json=metadata_json || %s "
                "WHERE id=%s AND worker_id=%s AND status='running' RETURNING id",
                (Jsonb({"progress": progress}), run_id, worker_id),
            )
            return await cursor.fetchone() is not None

    async def cancel(self, run_id):
        async with self.runtime_pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE mining_runs SET status='cancelled',finished_at=NOW(),lease_until=NULL,worker_id=NULL "
                    "WHERE id=%s AND status IN ('queued','running','interrupted')", (run_id,),
                )
                await conn.execute(
                    "UPDATE mining_workflow_node_events SET status='failed',finished_at=NOW(),error_code='cancelled' "
                    "WHERE run_id=%s AND status='started'", (run_id,),
                )

    async def heartbeat(self, run_id: str, worker_id: str) -> bool:
        async with self.runtime_pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE mining_runs SET lease_until=NOW()+interval '5 minutes' "
                "WHERE id=%s AND worker_id=%s AND status='running' RETURNING id", (run_id, worker_id),
            )
            return await cursor.fetchone() is not None

    async def finish(self, run_id: str, event_id: str, worker_id: str, *, output: dict, error: str = ""):
        async with self.runtime_pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """UPDATE mining_runs SET status=%s,current_stage='done',finished_at=NOW(),
                           error_summary=%s,metadata_json=metadata_json || %s,
                           worker_id=NULL,lease_until=NULL
                       WHERE id=%s AND worker_id=%s AND status='running' RETURNING id""",
                    ("failed" if error else "completed", error or None,
                     Jsonb({"output": output}), run_id, worker_id),
                )
                if await cursor.fetchone() is None:
                    return
                await conn.execute(
                    """UPDATE mining_workflow_node_events SET status=%s,finished_at=NOW(),
                           duration_ms=GREATEST(0,EXTRACT(EPOCH FROM (NOW()-started_at))*1000)::integer,
                           output_summary_json=%s,error_message=%s WHERE id=%s""",
                    ("failed" if error else "completed", Jsonb(output), error or None, event_id),
                )
