from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from knowledge_mining.mining.workflow.repositories.domain_run_repository import (
    AsyncDomainRunRepository,
)
from knowledge_mining.mining.workflow.repositories.global_workflow_repository import (
    GlobalWorkflowRepository,
)
from knowledge_mining.mining.workflow.run_binding import WorkflowRunBinder
from knowledge_mining.mining.workflow.service import (
    DraftRevisionConflict,
    WorkflowService,
)


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def table_exists(pool, table_name: str) -> bool:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT to_regclass(%s) AS name", (table_name,))
        row = await cursor.fetchone()
    return row["name"] is not None


async def scalar(pool, query: str, params=()):
    async with pool.connection() as conn:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
    return next(iter(row.values())) if row else None


async def test_global_definition_and_domain_runtime_never_cross_pools(
    control_pool, domain_pool
) -> None:
    control_service = WorkflowService(GlobalWorkflowRepository(control_pool))
    await control_service.ensure_system_workflows()
    binder = WorkflowRunBinder(
        control_service,
        ontology_lookup=lambda domain: asyncio.sleep(0, result=None),
        config_fingerprint=lambda: "postgres-acceptance-config",
    )
    binding = await binder.resolve(
        workflow_id=None,
        workflow_version=None,
        domain="plant-a",
        channel="prod",
        upload_batch_id="postgres-acceptance-batch",
    )
    run_id = uuid.uuid4().hex
    await AsyncDomainRunRepository(domain_pool).insert_queued_run(
        run_id=run_id,
        input_path="upload://postgres-acceptance-batch",
        domain="plant-a",
        channel="prod",
        execution_engine="workflow",
        binding=binding,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    assert await table_exists(control_pool, "mining_workflows")
    assert not await table_exists(domain_pool, "mining_workflows")
    assert await scalar(
        domain_pool,
        "SELECT workflow_version FROM mining_runs WHERE id = %s",
        (run_id,),
    ) == binding.workflow_version
    # The primary DB may retain compatibility runtime tables, but the Domain
    # insertion must never leak a row into that pool.
    assert await scalar(
        control_pool, "SELECT COUNT(*) FROM mining_runs WHERE id = %s", (run_id,)
    ) == 0


async def test_publish_is_atomic_and_historical_versions_remain_immutable(
    control_pool,
) -> None:
    service = WorkflowService(GlobalWorkflowRepository(control_pool))
    workflow = await service.create(
        workflow_id=f"acceptance-{uuid.uuid4().hex}",
        name=f"acceptance-{uuid.uuid4().hex}",
        template_key="minimal",
    )
    original_graph = deepcopy(workflow["draft_graph_json"])

    results = await asyncio.gather(
        service.publish(workflow["id"], expected_revision=0),
        service.publish(workflow["id"], expected_revision=0),
        return_exceptions=True,
    )

    published = [item for item in results if isinstance(item, dict)]
    conflicts = [item for item in results if isinstance(item, DraftRevisionConflict)]
    assert len(published) == 1
    assert len(conflicts) == 1
    stored = await service.get_version(workflow["id"], 1)
    assert stored["graph_json"] == original_graph

    changed = deepcopy(original_graph)
    changed["nodes"][0]["ui"]["x"] += 40
    await service.save_draft(
        workflow["id"], graph=changed, expected_revision=0, updated_by="acceptance"
    )
    assert (await service.get_version(workflow["id"], 1))["graph_json"] == original_graph
