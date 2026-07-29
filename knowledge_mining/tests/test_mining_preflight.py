from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from knowledge_mining.mining.api.routes import runs
from knowledge_mining.mining.preflight import TargetWorkflow, classify_preflight_matches
from knowledge_mining.mining.workflow.run_binding import WorkflowRunBinding


TARGET = TargetWorkflow(
    workflow_id="wf-new",
    workflow_version=2,
    workflow_version_id="wf-new-v2",
    workflow_graph_hash="graph-new",
)


def _snapshot(*, snapshot_id="snap-1", workflow_id="wf-new", version=2, graph="graph-new", active=False, complete=True):
    return {
        "snapshot_id": snapshot_id,
        "document_id": "doc-1",
        "document_key": "doc:/a.md",
        "workflow_id": workflow_id,
        "workflow_version": version,
        "workflow_version_id": f"{workflow_id}-v{version}",
        "workflow_graph_hash": graph,
        "is_active": active,
        "artifacts_complete": complete,
    }


def test_preflight_classifies_new_and_exact_reuse() -> None:
    assert classify_preflight_matches([], [], TARGET)["classification"] == "NEW"
    reused = classify_preflight_matches([_snapshot(active=True)], [], TARGET)
    assert reused["classification"] == "REUSED"
    assert reused["default_action"] == "REUSED"


def test_preflight_conflict_defaults_to_keep_current() -> None:
    result = classify_preflight_matches([
        _snapshot(active=True, workflow_id="wf-old", version=1, graph="graph-old"),
    ], [], TARGET)

    assert result["classification"] == "WORKFLOW_CONFLICT"
    assert result["default_action"] == "KEPT_CURRENT"
    assert result["allowed_actions"] == ["KEPT_CURRENT", "REMINED"]


def test_preflight_restores_exact_history_only_when_no_other_snapshot_is_active() -> None:
    result = classify_preflight_matches([_snapshot(active=False)], [], TARGET)
    assert result["classification"] == "RESTORABLE"
    assert result["default_action"] == "RESTORED"


def test_explicit_workflow_upload_requires_confirmed_preflight_decisions() -> None:
    with pytest.raises(ValidationError, match="preflight"):
        runs.CreateRunRequest(
            domain="plant-a", upload_batch_id="abcdef123456",
            workflow_id="wf-new", workflow_version=2,
        )

    body = runs.CreateRunRequest(
        domain="plant-a", upload_batch_id="abcdef123456",
        workflow_id="wf-new", workflow_version=2, preflight_id="pf-1",
        document_decisions=[{
            "relative_path": "a.md", "raw_content_hash": "raw-a",
            "selected_action": "KEPT_CURRENT", "state_token": "state-a",
        }],
    )
    assert body.document_decisions[0].selected_action == "KEPT_CURRENT"


@pytest.mark.asyncio
async def test_preflight_endpoint_resolves_exact_workflow_and_returns_result(monkeypatch, tmp_path) -> None:
    binding = WorkflowRunBinding(
        workflow_id="wf-new", workflow_version=2, workflow_version_id="wf-new-v2",
        graph_hash="graph-new", manifest={"graphHash": "graph-new"},
    )

    class Binder:
        async def resolve(self, **kwargs):
            assert kwargs["workflow_id"] == "wf-new"
            assert kwargs["workflow_version"] == 2
            return binding

    async def fake_build(**kwargs):
        assert kwargs["binding"] is binding
        return {"preflight_id": "pf-1", "summary": {"NEW": 1}, "items": []}

    async def fake_pool(domain):
        assert domain == "plant-a"
        return object()

    monkeypatch.setattr(runs, "resolve_upload_batch_path", lambda domain, batch: tmp_path)
    monkeypatch.setattr(runs, "resolve_domain", lambda domain: {"default_channel": "prod"})
    monkeypatch.setattr(runs, "build_run_preflight", fake_build)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        workflow_run_binder=Binder(),
        domain_pools=SimpleNamespace(async_pool=fake_pool),
    )))

    response = await runs.preflight_run(
        runs.RunPreflightRequest(
            domain="plant-a", upload_batch_id="abcdef123456",
            workflow_id="wf-new", workflow_version=2,
        ),
        request,
    )
    assert response["preflight_id"] == "pf-1"
