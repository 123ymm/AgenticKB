from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace

import pytest

from knowledge_mining.mining.workflow.execution_plan import ExecutionPlan, PlannedNode
from knowledge_mining.mining.workflow.executors.document_executor import WorkflowRunFailed
from knowledge_mining.mining.workflow.executors.global_executor import GlobalExecutor
from knowledge_mining.mining.workflow.graph import EdgeDef, OutputDef, WorkflowGraph
from knowledge_mining.mining.workflow.handler_registry import HandlerRegistry
from knowledge_mining.mining.workflow.handlers.finalize import mining_finalize_handler
from knowledge_mining.mining.workflow.handlers.global_nodes import (
    entity_review_gate_handler,
    graph_write_handler,
    ontology_induction_handler,
    ontology_review_gate_handler,
)


GLOBAL_TYPES = (
    "entity_review_gate",
    "ontology_induction",
    "ontology_review_gate",
    "graph_write",
    "mining_finalize",
)


def global_node(operator_type: str) -> PlannedNode:
    policies = {
        "entity_review_gate": "PAUSE_FOR_REVIEW",
        "ontology_induction": "FALLBACK",
        "ontology_review_gate": "PAUSE_FOR_REVIEW",
        "graph_write": "FAIL_FAST",
        "mining_finalize": "FAIL_FAST",
    }
    provides = {
        "entity_review_gate": {"entity_review_approved"},
        "ontology_induction": {"ontology_candidates"},
        "ontology_review_gate": {"ontology_review_approved"},
        "graph_write": {"graph_written"},
        "mining_finalize": {"finalized"},
    }
    guarded = operator_type != "mining_finalize"
    return PlannedNode(
        node_id=operator_type,
        operator_type=operator_type,
        operator_version="1",
        params={},
        requires=frozenset(),
        provides=frozenset(provides[operator_type]),
        error_policy=policies[operator_type],
        guard="ontology_applicable" if guarded else None,
    )


def global_plan(types=GLOBAL_TYPES) -> ExecutionPlan:
    nodes = tuple(global_node(item) for item in types)
    edges = tuple(
        EdgeDef(source, "finalizeInput", target, "finalizeInput")
        for source, target in zip(types, types[1:])
    )
    return ExecutionPlan(
        graph=WorkflowGraph(
            nodes=(), edges=edges, output=OutputDef(types[-1], "result")
        ),
        nodes=nodes,
        edges=edges,
        input_order=(),
        document_order=(),
        global_order=tuple(types),
        required_completion=frozenset({"finalized"}),
    )


class FakeGlobalRepository:
    def __init__(self) -> None:
        self.events = []
        self.attempts = defaultdict(int)
        self.active = []

    def start_node(self, **kwargs):
        key = kwargs["node_id"]
        self.attempts[key] += 1
        attempt = SimpleNamespace(id=f"{key}:{self.attempts[key]}")
        self.events.append({**kwargs, "id": attempt.id, "status": "started"})
        return attempt

    def finish_node(self, attempt, **kwargs):
        event = next(item for item in self.events if item["id"] == attempt.id)
        assert event["status"] == "started"
        event.update(kwargs)

    def is_node_completed(self, run_id, node_id, run_document_id):
        return any(
            item["run_id"] == run_id
            and item["node_id"] == node_id
            and item["run_document_id"] is None
            and item["status"] == "completed"
            for item in self.events
        )

    def reusable_node_result(self, run_id, node_id, run_document_id):
        reusable = {"completed", "skipped", "fallback", "not_applicable"}
        matching = [
            item
            for item in self.events
            if item["run_id"] == run_id
            and item["node_id"] == node_id
            and item["run_document_id"] is run_document_id
            and item["status"] in reusable
        ]
        if not matching:
            return None
        event = matching[-1]
        return {
            "status": event["status"],
            "capabilities": event.get("output_summary", {}).get(
                "capabilities", ()
            ),
        }

    def set_active_node(self, run_id, node_id, operator_type, pause_step=None):
        self.active.append((run_id, node_id, operator_type, pause_step))


class FakeGlobalServices:
    def __init__(self) -> None:
        self.calls = []
        self.pending_entities = 0
        self.pending_candidates = 0
        self.execution_mode = "publish"
        self.fail_graph = None
        self.release_id = "release-1"

    def count_pending_entity_mentions(self, run_id):
        self.calls.append(("entity_review_gate", run_id))
        return self.pending_entities

    def run_ontology_induction(self, run_id, node_id):
        self.calls.append(("ontology_induction", run_id, node_id))
        return {"candidates": 1}

    def count_pending_ontology_candidates(self, domain):
        self.calls.append(("ontology_review_gate", domain))
        return self.pending_candidates

    def write_graph_strict(self, run_id):
        self.calls.append(("graph_write", run_id))
        if self.fail_graph:
            raise self.fail_graph
        return {"edges": 2}

    def finalize_mining(self, run_id, **kwargs):
        self.calls.append(("mining_finalize", run_id, kwargs))
        if kwargs["execution_mode"] == "assets_only":
            return {"build_id": None, "release_id": None}
        return {"build_id": "build-1", "release_id": self.release_id}


def builtin_global_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    for operator_type, handler in {
        "entity_review_gate": entity_review_gate_handler,
        "ontology_induction": ontology_induction_handler,
        "ontology_review_gate": ontology_review_gate_handler,
        "graph_write": graph_write_handler,
        "mining_finalize": mining_finalize_handler,
    }.items():
        registry.register(operator_type, "1", handler)
    return registry


def runtime(*, ontology="ontology-v1"):
    services = FakeGlobalServices()
    services.handler_registry = builtin_global_registry()
    repository = FakeGlobalRepository()
    return SimpleNamespace(
        domain="odn",
        channel="prod",
        ontology_version_id=ontology,
        runtime_repository=repository,
        services=services,
        cancellation_check=lambda: False,
        manifest={
            "runId": "run-1",
            "runtimeBinding": {"ontologyApplicable": ontology is not None},
        },
    )


def test_full_global_order_is_review_then_induction_then_graph_then_finalize() -> None:
    context = runtime()

    result = GlobalExecutor(context).execute(global_plan())

    assert [item[0] for item in context.services.calls] == list(GLOBAL_TYPES)
    assert result.capabilities >= {
        "entity_review_approved",
        "ontology_review_approved",
        "graph_written",
        "release_published",
        "finalized",
    }


def test_graph_failure_prevents_build_and_release() -> None:
    context = runtime()
    context.services.fail_graph = RuntimeError("edge transaction failed")

    with pytest.raises(WorkflowRunFailed, match="edge transaction failed"):
        GlobalExecutor(context).execute(global_plan())

    assert "mining_finalize" not in [item[0] for item in context.services.calls]
    graph_event = next(
        item for item in context.runtime_repository.events
        if item["node_id"] == "graph_write"
    )
    assert graph_event["status"] == "failed"


def test_no_active_ontology_marks_guarded_line_not_applicable_but_finalizes() -> None:
    context = runtime(ontology=None)

    result = GlobalExecutor(context).execute(global_plan())

    assert result.status_for("ontology_induction") == "not_applicable"
    assert "ontology_not_applicable" in result.capabilities
    assert "release_published" in result.capabilities
    assert [item[0] for item in context.services.calls] == ["mining_finalize"]


def test_pause_persists_step_and_resume_retries_only_unfinished_nodes() -> None:
    context = runtime()
    context.services.pending_entities = 2
    executor = GlobalExecutor(context)

    paused = executor.execute(global_plan())

    assert paused.paused is True
    assert paused.pause_step == "entity_review_gate"
    assert context.runtime_repository.active[-1] == (
        "run-1",
        "entity_review_gate",
        "entity_review_gate",
        "entity_review_gate",
    )
    assert [item[0] for item in context.services.calls] == ["entity_review_gate"]

    context.services.pending_entities = 0
    resumed = executor.resume(global_plan())

    assert resumed.paused is False
    assert context.runtime_repository.attempts["entity_review_gate"] == 2
    assert [item[0] for item in context.services.calls].count("ontology_induction") == 1


def test_review_scopes_are_run_for_entities_and_domain_for_ontology() -> None:
    context = runtime()
    context.services.pending_candidates = 1

    result = GlobalExecutor(context).execute(global_plan())

    assert result.pause_step == "ontology_review_gate"
    assert ("entity_review_gate", "run-1") in context.services.calls
    assert ("ontology_review_gate", "odn") in context.services.calls


def test_assets_only_finalizes_without_build_or_release() -> None:
    context = runtime(ontology=None)
    context.services.execution_mode = "assets_only"

    result = GlobalExecutor(context).execute(global_plan())

    assert "finalized" in result.capabilities
    assert "release_published" not in result.capabilities
    finalize_call = context.services.calls[-1]
    assert finalize_call[2]["execution_mode"] == "assets_only"


def test_completed_finalize_is_not_called_twice_on_resume() -> None:
    context = runtime(ontology=None)
    executor = GlobalExecutor(context)
    executor.execute(global_plan())
    first_calls = len(context.services.calls)

    result = executor.resume(global_plan())

    assert len(context.services.calls) == first_calls
    assert "finalized" in result.capabilities


def test_explicit_publish_replays_only_completed_finalize() -> None:
    context = runtime(ontology=None)
    context.services.execution_mode = "assets_only"
    executor = GlobalExecutor(context)
    executor.execute(global_plan())

    context.services.execution_mode = "publish"
    result = executor.execute(
        global_plan(), replay_nodes=frozenset({"mining_finalize"})
    )

    assert "release_published" in result.capabilities
    assert context.runtime_repository.attempts["mining_finalize"] == 2


def test_finalize_requires_all_manifest_applicable_capabilities() -> None:
    context = runtime()
    context.manifest["executionPlan"] = {
        "requiredCompletion": ["assets_persisted", "graph_written", "finalized"]
    }
    context.services.initial_global_capabilities = frozenset({"assets_persisted"})

    with pytest.raises(WorkflowRunFailed, match="graph_written"):
        GlobalExecutor(context).execute(global_plan(("mining_finalize",)))

    assert context.services.calls == []


def test_workflow_services_forward_frozen_ontology_version(monkeypatch) -> None:
    from knowledge_mining.mining.jobs import run as run_job

    calls = []
    monkeypatch.setattr(
        run_job,
        "workflow_run_induction_strict",
        lambda *args, **kwargs: calls.append(("induction", kwargs)) or {},
    )
    monkeypatch.setattr(
        run_job,
        "workflow_write_graph_strict",
        lambda *args, **kwargs: calls.append(("graph", kwargs)) or {},
    )
    services = object.__new__(run_job._WorkflowJobServices)
    services.asset_db = object()
    services.profile = SimpleNamespace(domain_id="odn")
    services.llm_base_url = "http://llm"
    services.ontology_version_id = "ontology-frozen"

    services.run_ontology_induction("run-1", "ontology_induction")
    services.write_graph_strict("run-1")

    assert [item[1]["ontology_version_id"] for item in calls] == [
        "ontology-frozen",
        "ontology-frozen",
    ]


def test_workflow_service_claims_assets_only_run_before_manual_publish(
    monkeypatch,
) -> None:
    from knowledge_mining.mining.jobs import run as run_job

    calls = []

    class RuntimeDB:
        def get_run(self, run_id):
            return {"status": "completed"}

        def commit(self):
            calls.append("commit")

    class Tracker:
        def begin_manual_publish(self, run_id, *, domain):
            calls.append(("claim", run_id, domain))
            return True

    monkeypatch.setattr(
        run_job,
        "workflow_finalize_mining_strict",
        lambda *args, **kwargs: calls.append(("finalize", kwargs)) or {},
    )
    services = object.__new__(run_job._WorkflowJobServices)
    services.action = "publish"
    services.asset_db = object()
    services.runtime_db = RuntimeDB()
    services.tracker = Tracker()
    services.profile = SimpleNamespace(domain_id="odn")
    services.channel = "prod"

    services.finalize_mining(
        "run-1", execution_mode="publish", publish_on_partial_failure=False
    )

    assert calls[:2] == [("claim", "run-1", "odn"), "commit"]
    assert calls[2][0] == "finalize"
