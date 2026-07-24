from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from knowledge_mining.mining.api.models.workflows import (
    ArchiveWorkflowRequest,
    CloneWorkflowRequest,
    CreateWorkflowRequest,
    PublishWorkflowRequest,
    RestoreDraftRequest,
    SaveDraftRequest,
    ValidateWorkflowRequest,
)
from knowledge_mining.mining.workflow.compiler import WorkflowCompileException
from knowledge_mining.mining.workflow.operators.catalog import builtin_catalog
from knowledge_mining.mining.workflow.service import (
    DraftRevisionConflict,
    WorkflowArchived,
    WorkflowNameConflict,
    WorkflowNotFound,
)


router = APIRouter(tags=["mining-workflows"])


def _error(status_code: int, code: str, message: str, **details: Any) -> None:
    raise HTTPException(
        status_code,
        detail={"code": code, "message": message, "details": details},
    )


async def _service_call(awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    except WorkflowNotFound as exc:
        _error(404, "workflow_not_found", str(exc) or "Workflow not found")
    except DraftRevisionConflict as exc:
        _error(
            409,
            "draft_revision_conflict",
            "Workflow draft was changed by another request",
            workflow_id=str(exc),
        )
    except WorkflowNameConflict as exc:
        _error(
            409,
            "workflow_name_conflict",
            "Workflow name already exists",
            name=str(exc),
        )
    except WorkflowArchived as exc:
        _error(409, "workflow_archived", str(exc) or "Workflow is archived")
    except WorkflowCompileException as exc:
        _error(
            422,
            "workflow_compile_failed",
            "Workflow compilation failed",
            errors=[error.to_dict() for error in exc.errors],
        )
    except ValueError as exc:
        _error(422, "invalid_workflow", str(exc))


def _service(request: Request) -> Any:
    return request.app.state.workflow_service


@router.get("/api/mining-operators/catalog")
async def get_operator_catalog() -> dict[str, Any]:
    catalog = builtin_catalog()
    return {
        "catalog_version": "1",
        "items": [catalog[key].to_dict() for key in sorted(catalog)],
    }


@router.get("/api/mining-workflows")
async def list_workflows(
    request: Request,
    include_archived: bool = Query(False),
) -> dict[str, Any]:
    items = await _service_call(
        _service(request).list(include_archived=include_archived)
    )
    return {"items": items}


@router.post("/api/mining-workflows", status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).create(**body.model_dump())
    )


# Static routes must be registered before ``/{workflow_id}``.
@router.get("/api/mining-workflows/options")
async def list_workflow_options(request: Request) -> dict[str, Any]:
    return {"items": await _service_call(_service(request).published_options())}


@router.get("/api/mining-workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    return await _service_call(_service(request).get(workflow_id))


@router.put("/api/mining-workflows/{workflow_id}/draft")
async def save_workflow_draft(
    workflow_id: str, body: SaveDraftRequest, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).save_draft(
            workflow_id,
            graph=body.graph,
            expected_revision=body.expected_revision,
            updated_by=body.updated_by,
        )
    )


@router.post("/api/mining-workflows/{workflow_id}/validate")
async def validate_workflow(
    workflow_id: str, body: ValidateWorkflowRequest, request: Request
) -> dict[str, Any]:
    service = _service(request)
    graph = body.graph
    if graph is None:
        workflow = await _service_call(service.get(workflow_id))
        graph = workflow["draft_graph_json"]
    return await _service_call(service.validate_graph(graph, mode="publish"))


@router.post("/api/mining-workflows/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: str, body: PublishWorkflowRequest, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).publish(
            workflow_id,
            expected_revision=body.expected_revision,
            release_notes=body.release_notes,
            created_by=body.created_by,
        )
    )


@router.get("/api/mining-workflows/{workflow_id}/versions")
async def list_workflow_versions(
    workflow_id: str, request: Request
) -> dict[str, Any]:
    return {
        "items": await _service_call(
            _service(request).list_versions(workflow_id)
        )
    }


@router.get("/api/mining-workflows/{workflow_id}/versions/{version}")
async def get_workflow_version(
    workflow_id: str, version: int, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).get_version(workflow_id, version)
    )


@router.post(
    "/api/mining-workflows/{workflow_id}/versions/{version}/restore-draft"
)
async def restore_workflow_draft(
    workflow_id: str,
    version: int,
    body: RestoreDraftRequest,
    request: Request,
) -> dict[str, Any]:
    return await _service_call(
        _service(request).restore_draft(
            workflow_id,
            version=version,
            expected_revision=body.expected_revision,
            updated_by=body.updated_by,
        )
    )


@router.post("/api/mining-workflows/{workflow_id}/clone", status_code=201)
async def clone_workflow(
    workflow_id: str, body: CloneWorkflowRequest, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).clone(
            workflow_id,
            name=body.name,
            description=body.description,
            source_version=body.source_version,
            created_by=body.created_by,
        )
    )


@router.post("/api/mining-workflows/{workflow_id}/archive")
async def archive_workflow(
    workflow_id: str, body: ArchiveWorkflowRequest, request: Request
) -> dict[str, Any]:
    return await _service_call(
        _service(request).archive(workflow_id, updated_by=body.updated_by)
    )
