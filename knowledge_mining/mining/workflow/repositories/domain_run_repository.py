from __future__ import annotations

from typing import Any, Literal

from psycopg.types.json import Jsonb

from ..run_binding import WorkflowRunBinding


class AsyncDomainRunRepository:
    """Insert Runtime rows into one already-selected Domain pool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def insert_queued_run(
        self,
        *,
        run_id: str,
        input_path: str,
        domain: str,
        channel: str,
        execution_engine: Literal["legacy", "workflow"],
        binding: WorkflowRunBinding | None,
        started_at: str,
    ) -> str:
        if execution_engine == "workflow":
            if binding is None or not all((
                binding.workflow_id,
                binding.workflow_version,
                binding.workflow_version_id,
                binding.graph_hash,
                binding.manifest,
            )):
                raise ValueError("Workflow runs require a complete immutable binding")
        elif binding is not None:
            raise ValueError("Legacy runs cannot carry a Workflow binding")

        async with self._pool.connection() as conn:
            await conn.execute(
                """INSERT INTO mining_runs (
                       id, input_path, domain, status, current_stage, started_at,
                       channel, execution_engine, workflow_id, workflow_version,
                       workflow_version_id, workflow_graph_hash,
                       workflow_manifest_json
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                   )""",
                (
                    run_id,
                    input_path,
                    domain,
                    "queued",
                    "queued",
                    started_at,
                    channel,
                    execution_engine,
                    binding.workflow_id if binding else None,
                    binding.workflow_version if binding else None,
                    binding.workflow_version_id if binding else None,
                    binding.graph_hash if binding else None,
                    Jsonb(binding.manifest) if binding else None,
                ),
            )
        return run_id
