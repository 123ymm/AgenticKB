from __future__ import annotations

from copy import deepcopy

import pytest

from knowledge_mining.mining.workflow.core import OperatorResult, OperatorStatus
from knowledge_mining.mining.workflow.executors.document_executor import (
    DocumentExecutor,
    WorkflowRunFailed,
)
from knowledge_mining.mining.workflow.executors.global_executor import GlobalExecutor
from knowledge_mining.mining.workflow.handler_registry import HandlerRegistry
from knowledge_mining.tests.test_mining_document_executor import (
    FakeEventRepository,
    document_state,
    node,
    plan,
    runtime as document_runtime,
)
from knowledge_mining.tests.test_mining_global_executor import (
    global_plan,
    runtime as global_runtime,
)


def recovery_registry(repository: FakeEventRepository, calls: list[tuple]) -> HandlerRegistry:
    registry = HandlerRegistry()

    def parse(state, params, runtime):
        calls.append((state.run_document_id, "parse"))
        return OperatorResult(state, frozenset({"parsed"}), OperatorStatus.SUCCESS)

    def persist(state, params, runtime):
        calls.append((state.run_document_id, "asset_persist"))
        repository.markers[state.run_document_id] = (
            f"document-{state.run_document_id}",
            f"snapshot-{state.run_document_id}",
        )
        return OperatorResult(
            state, frozenset({"assets_persisted"}), OperatorStatus.SUCCESS
        )

    registry.register("parse", "1", parse)
    registry.register("asset_persist", "1", persist)
    return registry


def test_restart_skips_committed_documents_and_restarts_uncommitted_at_parse() -> None:
    repository = FakeEventRepository()
    calls: list[tuple] = []
    registry = recovery_registry(repository, calls)
    workflow = plan([node("parse"), node("asset_persist")])

    repository.markers["committed"] = ("document-committed", "snapshot-committed")
    repository.seed("committed", "asset_persist", "completed")
    repository.seed("interrupted", "parse", "started")

    result = DocumentExecutor(
        document_runtime(registry, repository)
    ).resume(
        workflow,
        [document_state("committed"), document_state("interrupted")],
        max_workers=2,
    )

    assert ("committed", "parse") not in calls
    assert ("committed", "asset_persist") not in calls
    assert calls.count(("interrupted", "parse")) == 1
    assert calls.count(("interrupted", "asset_persist")) == 1
    assert repository._attempts[("interrupted", "parse")] == 2
    assert result.outcomes[0].state.context.document_id == "document-committed"


def test_review_resume_keeps_manifest_and_retries_only_the_paused_gate() -> None:
    context = global_runtime()
    context.services.pending_entities = 1
    frozen_manifest = deepcopy(context.manifest)
    executor = GlobalExecutor(context)

    paused = executor.execute(global_plan())
    context.services.pending_entities = 0
    resumed = executor.resume(global_plan())

    assert paused.pause_step == "entity_review_gate"
    assert resumed.paused is False
    assert context.manifest == frozen_manifest
    assert context.runtime_repository.attempts["entity_review_gate"] == 2
    assert context.runtime_repository.attempts["ontology_induction"] == 1
    assert context.runtime_repository.attempts["mining_finalize"] == 1


def test_graph_failure_never_reaches_build_or_release_and_can_retry() -> None:
    context = global_runtime()
    context.services.fail_graph = RuntimeError("graph transaction rolled back")
    executor = GlobalExecutor(context)

    with pytest.raises(WorkflowRunFailed, match="graph transaction rolled back"):
        executor.execute(global_plan())

    assert "mining_finalize" not in [item[0] for item in context.services.calls]

    context.services.fail_graph = None
    result = executor.resume(global_plan())

    assert result.paused is False
    assert context.runtime_repository.attempts["graph_write"] == 2
    assert context.runtime_repository.attempts["mining_finalize"] == 1
    assert [item[0] for item in context.services.calls].count("mining_finalize") == 1


def test_resume_after_finalize_converges_without_a_second_release() -> None:
    context = global_runtime(ontology=None)
    executor = GlobalExecutor(context)

    first = executor.execute(global_plan())
    calls_after_first = list(context.services.calls)
    second = executor.resume(global_plan())

    assert first.capabilities >= {"finalized", "release_published"}
    assert second.capabilities >= {"finalized", "release_published"}
    assert context.services.calls == calls_after_first
    assert [item[0] for item in context.services.calls].count("mining_finalize") == 1
    assert set(context.runtime_repository.attempts.values()) == {1}
