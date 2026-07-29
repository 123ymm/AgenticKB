from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_mining.mining.contracts.models import BatchParams, RawFileData
from knowledge_mining.mining.jobs import run as run_job
from knowledge_mining.mining.pipeline import PipelineConfig


def _doc() -> RawFileData:
    return RawFileData(
        file_path="C:/uploads/a.md", relative_path="a.md", file_name="a.md",
        file_type="md", content="# A", raw_content_hash="raw-a",
        normalized_content_hash="normalized-a", file_size=3,
    )


class _RuntimeDb:
    def __init__(self, manifest):
        self.manifest = manifest
        self.executed = []

    def get_run(self, run_id):
        return {"id": run_id, "source_batch_id": "batch-1", "preflight_manifest_json": self.manifest}

    def get_run_documents(self, run_id):
        return []

    def _execute(self, *args):
        self.executed.append(args)

    def commit(self):
        pass


class _Tracker:
    def __init__(self):
        self.registered = []
        self.committed = []

    def set_run_phase(self, *args):
        return True

    def register_document(self, row):
        self.registered.append(row)

    def commit_document(self, run_document_id, document_id, snapshot_id):
        self.committed.append((run_document_id, document_id, snapshot_id))

    def start_document(self, run_document_id):
        pass

    def finish_ingest(self, *args):
        pass


class _AssetDb:
    def __init__(self):
        self.links = []

    def upsert_source_batch(self, **kwargs):
        return kwargs["batch_id"]

    def insert_snapshot_link(self, **kwargs):
        self.links.append(kwargs)

    def get_document_lifecycle_state(self, **kwargs):
        raise AssertionError("frozen preflight decisions must replace runtime reclassification")


def _services(monkeypatch, selected_action: str):
    snapshot = {
        "snapshot_id": "snapshot-old", "document_id": "document-old",
        "document_key": "doc:/old.md", "workflow_id": "wf-old",
        "workflow_version": 1, "workflow_graph_hash": "graph-old",
    }
    manifest = {
        "preflight_id": "pf-1",
        "items": [{
            "relative_path": "a.md", "raw_content_hash": "raw-a",
            "selected_action": selected_action,
            "current_snapshot": snapshot,
            "matched_snapshot": snapshot,
        }],
    }
    services = object.__new__(run_job._WorkflowJobServices)
    services.action = "execute"
    services.run_id = "run-1"
    services.asset_db = _AssetDb()
    services.runtime_db = _RuntimeDb(manifest)
    services.tracker = _Tracker()
    services.profile = SimpleNamespace(domain_id="plant-a")
    services.channel = "prod"
    services.input_path = "C:/uploads"
    services.batch_params = BatchParams()
    services.manifest = {"runtimeBinding": {"uploadBatchId": "batch-1"}}
    services.pipeline_config = PipelineConfig(domain="plant-a")
    monkeypatch.setattr(run_job, "ingest_directory", lambda *args: ([_doc()], {"accepted": 1}))
    return services


@pytest.mark.parametrize("action", ["REUSED", "RESTORED", "KEPT_CURRENT"])
def test_non_computing_preflight_actions_are_audited_without_running_document_nodes(monkeypatch, action) -> None:
    services = _services(monkeypatch, action)

    states = services._prepare_document_states()

    assert states == ()
    assert services.tracker.registered[0].metadata_json["preflight_action"] == action
    assert services.tracker.committed[0][1:] == ("document-old", "snapshot-old")
    assert services.asset_db.links[0]["source_batch_id"] == "batch-1"


def test_remine_uses_the_matched_logical_document_and_enters_the_pipeline(monkeypatch) -> None:
    services = _services(monkeypatch, "REMINED")

    states = services._prepare_document_states()

    assert len(states) == 1
    assert states[0].context.action == "UPDATE"
    assert states[0].context.existing_doc["id"] == "document-old"
    assert services.tracker.committed == []
