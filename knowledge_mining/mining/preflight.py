from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from knowledge_mining.mining.workflow.run_binding import WorkflowRunBinding


@dataclass(frozen=True)
class TargetWorkflow:
    workflow_id: str
    workflow_version: int
    workflow_version_id: str
    workflow_graph_hash: str

    @classmethod
    def from_binding(cls, binding: WorkflowRunBinding) -> "TargetWorkflow":
        return cls(
            workflow_id=binding.workflow_id,
            workflow_version=binding.workflow_version,
            workflow_version_id=binding.workflow_version_id,
            workflow_graph_hash=binding.graph_hash,
        )


def _is_exact(snapshot: dict[str, Any], target: TargetWorkflow) -> bool:
    return (
        snapshot.get("workflow_id") == target.workflow_id
        and snapshot.get("workflow_version") == target.workflow_version
        and snapshot.get("workflow_graph_hash") == target.workflow_graph_hash
    )


def _public_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        key: snapshot.get(key)
        for key in (
            "snapshot_id", "document_id", "document_key", "workflow_id",
            "workflow_version", "workflow_version_id", "workflow_graph_hash",
            "is_active", "artifacts_complete",
        )
    }


def classify_preflight_matches(
    snapshots: Iterable[dict[str, Any]],
    in_progress: Iterable[dict[str, Any]],
    target: TargetWorkflow,
) -> dict[str, Any]:
    rows = list(snapshots)
    active = next((row for row in rows if row.get("is_active")), None)
    exact_active = active if active is not None and _is_exact(active, target) else None
    exact_history = next(
        (
            row for row in rows
            if not row.get("is_active")
            and row.get("artifacts_complete")
            and _is_exact(row, target)
        ),
        None,
    )
    running = next(
        (
            row for row in in_progress
            if row.get("workflow_id") == target.workflow_id
            and row.get("workflow_version") == target.workflow_version
            and row.get("workflow_graph_hash") == target.workflow_graph_hash
        ),
        None,
    )

    if exact_active and exact_active.get("artifacts_complete"):
        classification, default, allowed, selected = (
            "REUSED", "REUSED", ["REUSED"], exact_active,
        )
    elif running is not None:
        classification, default, allowed, selected = (
            "IN_PROGRESS", "JOINED_EXISTING", ["JOINED_EXISTING"], active,
        )
    elif exact_history is not None and active is None:
        classification, default, allowed, selected = (
            "RESTORABLE", "RESTORED", ["RESTORED", "REMINED"], exact_history,
        )
    elif exact_history is not None and active is not None:
        classification, default, allowed, selected = (
            "RESTORABLE_CONFLICT", "KEPT_CURRENT",
            ["KEPT_CURRENT", "RESTORED", "REMINED"], active,
        )
    elif active is not None:
        classification, default, allowed, selected = (
            "WORKFLOW_CONFLICT", "KEPT_CURRENT", ["KEPT_CURRENT", "REMINED"], active,
        )
    elif rows:
        classification, default, allowed, selected = (
            "HISTORY_UNAVAILABLE", "REMINED", ["REMINED"], rows[0],
        )
    else:
        classification, default, allowed, selected = (
            "NEW", "NEW", ["NEW"], None,
        )

    return {
        "classification": classification,
        "default_action": default,
        "allowed_actions": allowed,
        "current_snapshot": _public_snapshot(active),
        "matched_snapshot": _public_snapshot(selected),
        "existing_run_id": (running or {}).get("run_id"),
    }


def _raw_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_token(
    *, domain: str, relative_path: str, raw_content_hash: str,
    current_snapshot_id: str | None, graph_hash: str,
) -> str:
    value = "\n".join((
        domain, relative_path, raw_content_hash, current_snapshot_id or "", graph_hash,
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _snapshot_matches(conn: Any, *, domain: str, channel: str, raw_hash: str) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """SELECT snapshots.id AS snapshot_id,
                  links.document_id,
                  documents.document_key,
                  snapshots.workflow_id,
                  snapshots.workflow_version,
                  snapshots.workflow_version_id,
                  snapshots.workflow_graph_hash,
                  EXISTS (
                      SELECT 1 FROM asset_raw_segments AS segments
                      WHERE segments.document_snapshot_id = snapshots.id
                  ) AS artifacts_complete,
                  EXISTS (
                      SELECT 1
                      FROM asset_publish_releases AS releases
                      JOIN asset_build_document_snapshots AS selections
                        ON selections.build_id = releases.build_id
                       AND selections.document_id = links.document_id
                       AND selections.document_snapshot_id = snapshots.id
                       AND selections.selection_status = 'active'
                      WHERE releases.domain = %s
                        AND releases.channel = %s
                        AND releases.status = 'active'
                  ) AS is_active
           FROM asset_document_snapshots AS snapshots
           JOIN asset_document_snapshot_links AS links
             ON links.document_snapshot_id = snapshots.id
           JOIN asset_documents AS documents
             ON documents.id = links.document_id AND documents.domain = snapshots.domain
           WHERE snapshots.domain = %s AND snapshots.raw_content_hash = %s
           ORDER BY is_active DESC, snapshots.created_at DESC, snapshots.id DESC""",
        (domain, channel, domain, raw_hash),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _in_progress_matches(conn: Any, *, domain: str, raw_hash: str) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """SELECT runs.id AS run_id, runs.workflow_id, runs.workflow_version,
                  runs.workflow_graph_hash
           FROM mining_run_documents AS documents
           JOIN mining_runs AS runs ON runs.id = documents.run_id
           WHERE runs.domain = %s
             AND documents.raw_content_hash = %s
             AND runs.status IN ('queued', 'running', 'awaiting_review')
           ORDER BY runs.started_at DESC""",
        (domain, raw_hash),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def build_run_preflight(
    *, pool: Any, batch_path: Path, domain: str, channel: str,
    binding: WorkflowRunBinding,
) -> dict[str, Any]:
    target = TargetWorkflow.from_binding(binding)
    files = sorted(path for path in batch_path.rglob("*") if path.is_file())
    items: list[dict[str, Any]] = []
    async with pool.connection() as conn:
        for path in files:
            relative_path = path.relative_to(batch_path).as_posix()
            raw_hash = _raw_hash(path)
            snapshots = await _snapshot_matches(
                conn, domain=domain, channel=channel, raw_hash=raw_hash,
            )
            in_progress = await _in_progress_matches(
                conn, domain=domain, raw_hash=raw_hash,
            )
            classification = classify_preflight_matches(snapshots, in_progress, target)
            current = classification.get("current_snapshot") or {}
            items.append({
                "relative_path": relative_path,
                "file_name": path.name,
                "file_size": path.stat().st_size,
                "raw_content_hash": raw_hash,
                **classification,
                "selected_action": classification["default_action"],
                "state_token": _state_token(
                    domain=domain,
                    relative_path=relative_path,
                    raw_content_hash=raw_hash,
                    current_snapshot_id=current.get("snapshot_id"),
                    graph_hash=target.workflow_graph_hash,
                ),
            })

    summary = dict(Counter(item["classification"] for item in items))
    return {
        "preflight_id": uuid.uuid4().hex,
        "domain": domain,
        "workflow": {
            "id": target.workflow_id,
            "version": target.workflow_version,
            "version_id": target.workflow_version_id,
            "graph_hash": target.workflow_graph_hash,
        },
        "summary": summary,
        "items": items,
    }
