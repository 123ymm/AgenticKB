from __future__ import annotations

from knowledge_mining.mining.contracts.models import DocumentProfile, RawFileData
from knowledge_mining.mining.infra import pg_schema
from knowledge_mining.mining.snapshot import select_or_create_snapshot


class _SnapshotDb:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.snapshots: dict[tuple[str, str, str | None, int | None, str | None], dict] = {}
        self.links: list[dict] = []

    def get_document_by_key(self, *, domain, document_key):
        return self.documents.get(f"{domain}:{document_key}")

    def get_document(self, *, domain, document_id):
        return next(
            (row for row in self.documents.values() if row["domain"] == domain and row["id"] == document_id),
            None,
        )

    def upsert_document(self, *, domain, document_id, document_key, **kwargs):
        key = f"{domain}:{document_key}"
        self.documents.setdefault(key, {"id": document_id, "domain": domain, "document_key": document_key})
        return self.documents[key]["id"]

    def get_snapshot_by_hash(
        self, *, domain, normalized_content_hash, workflow_id=None,
        workflow_version=None, workflow_graph_hash=None,
    ):
        return self.snapshots.get((
            domain, normalized_content_hash, workflow_id, workflow_version, workflow_graph_hash,
        ))

    def upsert_snapshot(
        self, *, domain, snapshot_id, normalized_content_hash,
        workflow_id=None, workflow_version=None, workflow_graph_hash=None, **kwargs,
    ):
        key = (domain, normalized_content_hash, workflow_id, workflow_version, workflow_graph_hash)
        self.snapshots.setdefault(key, {"id": snapshot_id})
        return self.snapshots[key]["id"]

    def insert_snapshot_link(self, **kwargs):
        self.links.append(kwargs)


def _doc() -> RawFileData:
    return RawFileData(
        file_path="a.md", relative_path="a.md", file_name="a.md",
        file_type="md", content="# A", raw_content_hash="raw-a",
        normalized_content_hash="normalized-a", file_size=3,
    )


def _profile() -> DocumentProfile:
    return DocumentProfile(document_key="doc:/a.md")


def test_same_content_reuses_only_the_same_exact_workflow_snapshot() -> None:
    db = _SnapshotDb()
    workflow_a = {
        "workflow_id": "wf-a", "workflow_version": 1,
        "workflow_version_id": "wf-a-v1", "workflow_graph_hash": "graph-a",
    }
    workflow_b = {
        "workflow_id": "wf-b", "workflow_version": 1,
        "workflow_version_id": "wf-b-v1", "workflow_graph_hash": "graph-b",
    }

    _, first, _ = select_or_create_snapshot(
        db, _doc(), _profile(), domain="plant-a", workflow_binding=workflow_a,
    )
    _, repeated, _ = select_or_create_snapshot(
        db, _doc(), _profile(), domain="plant-a", workflow_binding=workflow_a,
    )
    _, other_workflow, _ = select_or_create_snapshot(
        db, _doc(), _profile(), domain="plant-a", workflow_binding=workflow_b,
    )

    assert first == repeated
    assert other_workflow != first


def test_existing_logical_document_can_receive_a_new_workflow_snapshot() -> None:
    db = _SnapshotDb()
    db.documents["plant-a:doc:/old.md"] = {
        "id": "document-existing", "domain": "plant-a", "document_key": "doc:/old.md",
    }

    document_id, _, _ = select_or_create_snapshot(
        db,
        _doc(),
        _profile(),
        domain="plant-a",
        existing_document_id="document-existing",
        workflow_binding={
            "workflow_id": "wf-b", "workflow_version": 2,
            "workflow_version_id": "wf-b-v2", "workflow_graph_hash": "graph-b2",
        },
    )

    assert document_id == "document-existing"
    assert db.links[-1]["document_id"] == "document-existing"


def test_domain_schema_applies_snapshot_workflow_binding_migration() -> None:
    names = [path.name for path in pg_schema.domain_schema_paths()]
    assert "004_asset_snapshot_workflow_binding.sql" in names
    ddl_path = next(path for path in pg_schema.domain_schema_paths() if path.name == "004_asset_snapshot_workflow_binding.sql")
    ddl = ddl_path.read_text(encoding="utf-8").lower()
    assert "workflow_graph_hash" in ddl
    assert "where workflow_graph_hash is not null" in ddl
    assert "where workflow_graph_hash is null" in ddl
