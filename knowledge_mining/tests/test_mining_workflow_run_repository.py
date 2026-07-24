from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from knowledge_mining.mining.workflow.repositories.domain_run_repository import (
    DomainRunRepository,
)


class FakeCursor:
    def __init__(self, *, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, pool: "FakeSyncPool") -> None:
        self.pool = pool

    @contextmanager
    def transaction(self):
        self.pool.transactions += 1
        yield

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if "pg_advisory_xact_lock" in normalized:
            self.pool.advisory_locks.append(tuple(params))
            return FakeCursor(row={"locked": True})
        if "COALESCE(MAX(attempt_no), 0) + 1" in normalized:
            run_id, node_id, run_document_id = params
            attempts = [
                row["attempt_no"]
                for row in self.pool.rows.values()
                if row["run_id"] == run_id
                and row["node_id"] == node_id
                and row["run_document_id"] == run_document_id
            ]
            return FakeCursor(row={"attempt_no": max(attempts, default=0) + 1})
        if normalized.startswith("INSERT INTO mining_workflow_node_events"):
            (
                event_id,
                run_id,
                run_document_id,
                node_id,
                operator_type,
                operator_version,
                attempt_no,
                started_at,
                input_summary,
            ) = params
            self.pool.rows[event_id] = {
                "id": event_id,
                "run_id": run_id,
                "run_document_id": run_document_id,
                "node_id": node_id,
                "operator_type": operator_type,
                "operator_version": operator_version,
                "status": "started",
                "attempt_no": attempt_no,
                "started_at": started_at,
                "input_summary_json": input_summary.obj,
            }
            return FakeCursor()
        if normalized.startswith("UPDATE mining_workflow_node_events"):
            (
                status,
                finished_at,
                duration_ms,
                output_summary,
                error_code,
                error_message,
                metadata,
                event_id,
            ) = params
            self.pool.rows[event_id].update({
                "status": status,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "output_summary_json": output_summary.obj,
                "error_code": error_code,
                "error_message": error_message,
                "metadata_json": metadata.obj,
            })
            return FakeCursor()
        if normalized.startswith("UPDATE mining_runs SET active_node_id"):
            if "pause_step = NULL" in normalized:
                node_id, operator_type, run_id = params
                pause_step = None
            else:
                node_id, operator_type, pause_step, run_id = params
            self.pool.runs[run_id].update({
                "active_node_id": node_id,
                "active_operator_type": operator_type,
                "pause_step": pause_step,
            })
            return FakeCursor()
        if normalized.startswith("SELECT * FROM mining_runs"):
            return FakeCursor(row=self.pool.runs.get(params[0]))
        if normalized.startswith("SELECT workflow_manifest_json FROM mining_runs"):
            run = self.pool.runs.get(params[0])
            return FakeCursor(row=None if run is None else {
                "workflow_manifest_json": run.get("workflow_manifest_json")
            })
        if normalized.startswith("SELECT 1 FROM mining_workflow_node_events"):
            run_id, node_id, run_document_id = params
            completed = (run_id, run_document_id, node_id) in self.pool.completed
            completed = completed or any(
                row["run_id"] == run_id
                and row["node_id"] == node_id
                and row["run_document_id"] == run_document_id
                and row["status"] == "completed"
                for row in self.pool.rows.values()
            )
            return FakeCursor(row={"exists": 1} if completed else None)
        if normalized.startswith("SELECT * FROM mining_workflow_node_events"):
            rows = sorted(
                (
                    dict(row)
                    for row in self.pool.rows.values()
                    if row["run_id"] == params[0]
                ),
                key=lambda row: (row["started_at"], row["node_id"]),
            )
            return FakeCursor(rows=rows)
        raise AssertionError(f"Unexpected SQL: {normalized}")


class FakeSyncPool:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.completed: set[tuple[str, str | None, str]] = set()
        self.transactions = 0
        self.advisory_locks: list[tuple] = []
        self.runs = {
            "run-1": {
                "id": "run-1",
                "workflow_manifest_json": {"workflowId": "wf", "workflowVersion": 2},
                "active_node_id": None,
                "active_operator_type": None,
                "pause_step": None,
            }
        }

    @contextmanager
    def connection(self):
        yield FakeConnection(self)


@pytest.fixture
def fake_sync_pool() -> FakeSyncPool:
    return FakeSyncPool()


def test_start_event_allocates_next_attempt_and_finish_updates_same_row(
    fake_sync_pool: FakeSyncPool,
) -> None:
    repo = DomainRunRepository(fake_sync_pool)
    first = repo.start_node(
        run_id="run-1",
        run_document_id="rd-1",
        node_id="embedding",
        operator_type="embedding",
        operator_version="1",
        input_summary={"units": 2},
    )
    repo.finish_node(first, status="completed", output_summary={"vectors": 2})
    second = repo.start_node(
        run_id="run-1",
        run_document_id="rd-1",
        node_id="embedding",
        operator_type="embedding",
        operator_version="1",
        input_summary={"units": 2},
    )

    assert first.attempt_no == 1
    assert second.attempt_no == 2
    assert fake_sync_pool.rows[first.id]["status"] == "completed"
    assert fake_sync_pool.rows[first.id]["output_summary_json"] == {"vectors": 2}
    assert fake_sync_pool.rows[first.id]["duration_ms"] >= 0
    assert fake_sync_pool.transactions == 2
    assert len(fake_sync_pool.advisory_locks) == 2


def test_completed_document_node_is_resume_safe(
    fake_sync_pool: FakeSyncPool,
) -> None:
    repo = DomainRunRepository(fake_sync_pool)
    fake_sync_pool.completed.add(("run-1", "rd-1", "asset_persist"))

    assert repo.is_node_completed("run-1", "asset_persist", "rd-1") is True
    assert repo.is_node_completed("run-1", "asset_persist", "rd-2") is False
    assert repo.is_node_completed("run-1", "asset_persist", None) is False


def test_manifest_active_node_and_event_listing_are_domain_local(
    fake_sync_pool: FakeSyncPool,
) -> None:
    repo = DomainRunRepository(fake_sync_pool)
    attempt = repo.start_node(
        run_id="run-1",
        run_document_id=None,
        node_id="entity-review",
        operator_type="entity_review_gate",
        operator_version="1",
        input_summary={},
    )
    repo.set_active_node(
        "run-1", "entity-review", "entity_review_gate", "entity_review"
    )

    assert repo.load_run("run-1")["id"] == "run-1"
    assert repo.load_manifest("run-1") == {
        "workflowId": "wf", "workflowVersion": 2
    }
    assert fake_sync_pool.runs["run-1"]["pause_step"] == "entity_review"
    assert [row["id"] for row in repo.list_node_events("run-1")] == [attempt.id]


def test_finish_rejects_non_terminal_status(fake_sync_pool: FakeSyncPool) -> None:
    repo = DomainRunRepository(fake_sync_pool)
    attempt = repo.start_node(
        run_id="run-1",
        run_document_id=None,
        node_id="finalize",
        operator_type="mining_finalize",
        operator_version="1",
        input_summary={},
    )

    with pytest.raises(ValueError, match="terminal node status"):
        repo.finish_node(attempt, status="started")


def test_node_attempt_uses_utc_datetime(fake_sync_pool: FakeSyncPool) -> None:
    attempt = DomainRunRepository(fake_sync_pool).start_node(
        run_id="run-1",
        run_document_id=None,
        node_id="finalize",
        operator_type="mining_finalize",
        operator_version="1",
        input_summary={},
    )

    assert isinstance(attempt.started_at, datetime)
    assert attempt.started_at.tzinfo == timezone.utc
