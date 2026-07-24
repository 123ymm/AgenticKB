from __future__ import annotations

from copy import deepcopy

import pytest

from knowledge_mining.mining.workflow.service import (
    DraftRevisionConflict,
    WorkflowArchived,
    WorkflowNotFound,
    WorkflowService,
)


class MemoryWorkflowRepository:
    def __init__(self) -> None:
        self.workflows: dict[str, dict] = {}
        self.versions: dict[tuple[str, int], dict] = {}

    async def list_workflows(self, *, include_archived: bool = False) -> list[dict]:
        items = self.workflows.values()
        if not include_archived:
            items = (item for item in items if item["status"] == "active")
        return deepcopy(sorted(items, key=lambda item: item["name"]))

    async def get_workflow(self, workflow_id: str) -> dict | None:
        return deepcopy(self.workflows.get(workflow_id))

    async def get_by_name(self, name: str) -> dict | None:
        return deepcopy(next(
            (item for item in self.workflows.values() if item["name"] == name),
            None,
        ))

    async def insert_workflow(self, record: dict) -> dict:
        if record["id"] in self.workflows or await self.get_by_name(record["name"]):
            raise RuntimeError("duplicate workflow")
        self.workflows[record["id"]] = deepcopy(record)
        return deepcopy(record)

    async def update_draft(
        self,
        workflow_id: str,
        *,
        graph: dict,
        expected_revision: int,
        updated_by: str | None,
    ) -> dict | None:
        item = self.workflows.get(workflow_id)
        if (
            item is None
            or item["status"] != "active"
            or item["draft_revision"] != expected_revision
        ):
            return None
        item["draft_graph_json"] = deepcopy(graph)
        item["draft_revision"] += 1
        item["updated_by"] = updated_by
        return deepcopy(item)

    async def insert_version_and_advance(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        version_record: dict,
    ) -> dict | None:
        item = self.workflows.get(workflow_id)
        if (
            item is None
            or item["status"] != "active"
            or item["draft_revision"] != expected_revision
        ):
            return None
        next_version = (item["current_version"] or 0) + 1
        if version_record["version"] != next_version:
            raise AssertionError("service did not allocate the next version")
        stored = deepcopy(version_record)
        stored["workflow_id"] = workflow_id
        self.versions[(workflow_id, next_version)] = stored
        item["current_version"] = next_version
        return deepcopy(stored)

    async def list_versions(self, workflow_id: str) -> list[dict]:
        return deepcopy([
            value
            for (owner, _), value in sorted(
                self.versions.items(), key=lambda pair: pair[0][1], reverse=True
            )
            if owner == workflow_id
        ])

    async def get_version(self, workflow_id: str, version: int) -> dict | None:
        return deepcopy(self.versions.get((workflow_id, version)))

    async def archive(
        self, workflow_id: str, *, updated_by: str | None
    ) -> dict | None:
        item = self.workflows.get(workflow_id)
        if item is None or item["status"] != "active":
            return None
        item["status"] = "archived"
        item["updated_by"] = updated_by
        return deepcopy(item)


@pytest.fixture
def memory_workflow_repo() -> MemoryWorkflowRepository:
    return MemoryWorkflowRepository()


@pytest.mark.asyncio
async def test_publish_is_immutable_and_restore_creates_a_new_draft(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)
    created = await service.create(
        name="custom", description="demo", template_key="minimal", created_by="tester"
    )
    v1 = await service.publish(
        created["id"],
        expected_revision=created["draft_revision"],
        release_notes="first",
        created_by="tester",
    )
    published_graph = deepcopy(v1["graph_json"])

    restored = await service.restore_draft(
        created["id"],
        version=1,
        expected_revision=created["draft_revision"],
        updated_by="tester",
    )
    restored["draft_graph_json"]["nodes"][0]["params"]["clientMutation"] = True

    assert restored["draft_revision"] == created["draft_revision"] + 1
    assert (await service.get_version(created["id"], 1))["graph_json"] == published_graph
    assert (await service.get(created["id"]))["current_version"] == 1


@pytest.mark.asyncio
async def test_stale_draft_revision_is_rejected(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)
    created = await service.create(name="custom", template_key="minimal")
    await service.save_draft(
        created["id"],
        graph=created["draft_graph_json"],
        expected_revision=0,
        updated_by="a",
    )
    with pytest.raises(DraftRevisionConflict):
        await service.save_draft(
            created["id"],
            graph=created["draft_graph_json"],
            expected_revision=0,
            updated_by="b",
        )


@pytest.mark.asyncio
async def test_seed_creates_one_global_full_default_idempotently(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)

    await service.ensure_system_workflows()
    await service.ensure_system_workflows()

    items = await service.list(include_archived=True)
    defaults = [item for item in items if item["is_system_default"]]
    assert [(item["id"], item["current_version"]) for item in defaults] == [
        ("system-full-baseline", 1)
    ]
    assert len(
        (await service.get_version("system-full-baseline", 1))["graph_json"]["nodes"]
    ) == 16


@pytest.mark.asyncio
async def test_clone_can_start_from_an_exact_historical_version(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)
    source = await service.create(name="source", template_key="minimal")
    await service.publish(source["id"], expected_revision=0)

    clone = await service.clone(
        source["id"], name="clone", source_version=1, created_by="tester"
    )

    assert clone["name"] == "clone"
    assert clone["current_version"] is None
    assert clone["draft_graph_json"] == (
        await service.get_version(source["id"], 1)
    )["graph_json"]


@pytest.mark.asyncio
async def test_system_default_cannot_be_archived(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)
    await service.ensure_system_workflows()

    with pytest.raises(WorkflowArchived):
        await service.archive("system-full-baseline", updated_by="tester")


@pytest.mark.asyncio
async def test_published_options_and_exact_version_resolution(
    memory_workflow_repo: MemoryWorkflowRepository,
) -> None:
    service = WorkflowService(memory_workflow_repo)
    await service.ensure_system_workflows()
    custom = await service.create(name="custom", template_key="minimal")
    await service.publish(custom["id"], expected_revision=0)
    draft_only = await service.create(name="draft-only", template_key="minimal")

    options = await service.published_options()
    exact = await service.resolve_published_version(
        workflow_id=custom["id"], workflow_version=1
    )
    default = await service.resolve_published_version(
        workflow_id=None,
        workflow_version=None,
        default_workflow_id="system-full-baseline",
    )

    assert {item["id"] for item in options} == {
        "system-full-baseline",
        custom["id"],
    }
    assert exact["workflow_id"] == custom["id"]
    assert exact["version"] == 1
    assert default["workflow_id"] == "system-full-baseline"
    with pytest.raises(WorkflowNotFound):
        await service.resolve_published_version(
            workflow_id=draft_only["id"], workflow_version=None
        )
