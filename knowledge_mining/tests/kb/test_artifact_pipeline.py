import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from knowledge_mining.mining.infra.object_store.fake import FakeObjectStore
from knowledge_mining.mining.kb.services.artifact_pipeline_service import ArtifactPipelineService
from knowledge_mining.mining.kb.services.artifact_service import ArtifactNotFound, InvalidOperation
from knowledge_mining.mining.kb.services.kb_service import NotFound
from knowledge_mining.mining.workflow.artifact_tables import extract_rows, extraction_manifest, table_handler_registry
from knowledge_mining.mining.workflow.manifest import value_hash
from knowledge_mining.mining.workflow.artifact_tables import COLUMNS

pytestmark = pytest.mark.asyncio
SOURCE = {"id": "doc", "snapshot_id": "snap", "document_name": "产品.md", "directory_path": "资料"}
TABLE = {"table_ref": "t1", "columns_json": [{"name": "参数"}], "row_count": 2, "sheet_name": "sheet", "readiness": "ready"}
CELLS = [{"table_ref": "t1", "row_index": 0, "column_index": 0, "column_name": "参数", "value": "名称", "is_header": True},
         {"table_ref": "t1", "row_index": 1, "column_index": 0, "column_name": "参数", "value": "命令", "is_header": False}]
IR = {"elements": [{"element_id": "caption", "order_index": 1, "text": "参数说明"}],
      "structured_assets": {"t1": {"table_id": "t1", "caption_element_id": "caption"}}}


class Objects:
    def __init__(self):
        self.rows = {}

    async def register(self, row):
        self.rows[row.id] = row
        return row

    async def get(self, key):
        return self.rows.get(key)


@pytest.fixture
def service(tmp_path):
    artifact = {"artifact_id": "a", "domain_id": "domain"}
    return ArtifactPipelineService(
        repository=SimpleNamespace(freeze_sources=AsyncMock(return_value=[SOURCE]), enqueue=AsyncMock(return_value="run"),
                                   table_data=AsyncMock(return_value=([TABLE], CELLS)),
                                   list_runs=AsyncMock(), get_run=AsyncMock(), claim=AsyncMock(return_value="event"),
                                   heartbeat=AsyncMock(return_value=True), finish=AsyncMock()),
        artifacts=SimpleNamespace(_assert_role=AsyncMock(return_value=(artifact, "owner"))),
        kbs=SimpleNamespace(_assert_read=AsyncMock()),
        kb_db=SimpleNamespace(get_kb=AsyncMock(return_value={"domain": "domain"}), get_user=AsyncMock(return_value={"id": "u"})),
        storage=FakeObjectStore(str(tmp_path)), objects=Objects(), bucket="test-binary", provider="fake",
    )


async def test_extract_preserves_caption_coordinates_and_provenance():
    rows, text = extract_rows(SOURCE, [TABLE], CELLS, IR)
    assert text == "参数说明"
    assert rows[0]["table_name"] == "参数说明"
    assert rows[0]["table_content"] == {"表头": ["参数"], "数据": [{"参数": "命令"}]}
    assert rows[0]["provenance"]["snapshot_id"] == "snap"
    assert rows[0]["provenance"]["name_source"] == "caption"
    assert extract_rows(SOURCE, [TABLE], CELLS, {})[0][0]["table_name"] == "sheet"
    assert extract_rows(SOURCE, [TABLE | {"sheet_name": None}], CELLS, {})[0][0]["provenance"]["name_source"] == "generated"


async def test_submit_dedupes_and_freezes_sources_without_artifact_writes(service):
    assert await service.submit({"id": "u"}, "a", "kb", ["doc", "doc"]) == "run"
    service.repo.freeze_sources.assert_awaited_once_with("kb", ["doc"])
    frozen = service.repo.enqueue.call_args.args[3]
    assert frozen["nodes"][0]["params"]["sources"] == [SOURCE]
    assert set(vars(service.artifacts)) == {"_assert_role"}


async def test_steps_freeze_parent_input_and_branch_without_mutating_history(service):
    snapshot = {"columns": COLUMNS, "rows": [], "issues": [], "documents": {}}
    output = await service.write_snapshot(snapshot)
    parent = {"id": "parent", "status": "completed", "metadata_json": {"kb_id": "kb", "output": output},
              "workflow_manifest_json": extraction_manifest([SOURCE])}
    service.repo.get_run.return_value = parent
    await service.submit_step({"id": "u"}, "a", "parent", "select", {"input_object_id": "attacker"})
    frozen = service.repo.enqueue.call_args.args[3]
    assert frozen["parent_run_id"] == "parent" and frozen["step_index"] == 2
    assert frozen["nodes"][0]["params"]["input_object_id"] == output["storage_object_id"]
    await service.submit_step({"id": "u"}, "a", "parent", "select", {})
    assert service.repo.enqueue.call_count == 2
    assert parent["status"] == "completed"


async def test_lookup_upload_uses_existing_snapshot_and_rejects_formulas(service, monkeypatch):
    import io
    from openpyxl import Workbook
    from knowledge_mining.mining.workflow.repositories.global_workflow_repository import GlobalWorkflowRepository
    insert = AsyncMock()
    monkeypatch.setattr(GlobalWorkflowRepository, "insert_workflow", insert)
    service.repo.pool = object()
    def workbook(value):
        book = Workbook()
        book.active.append(["key", "label"])
        book.active.append(["A", value])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        return stream.getvalue()
    result = await service.upload_lookup({"id": "u"}, "a", "lookup.xlsx", workbook("label"))
    assert result["row_count"] == 1
    metadata = insert.call_args.args[0]["metadata_json"]
    assert metadata["artifact_id"] == "a" and metadata["filename"] == "lookup.xlsx"
    saved = await service.read_json(metadata["output"]["storage_object_id"])
    assert saved["rows"][0]["label"] == "label"
    with pytest.raises(InvalidOperation, match="公式"):
        await service.upload_lookup({"id": "u"}, "a", "lookup.xlsx", workbook("=1+1"))
    assert insert.call_count == 1


async def test_llm_checkpoint_and_failed_only_retry(service, monkeypatch):
    from knowledge_mining.mining.infra import llm_task_client, mining_config
    calls = []
    class Client:
        def __init__(self, **kw): pass
        def submit_chat(self, messages, **kw):
            calls.append(kw)
            return "task-1"
        def batch_status(self, ids, **kw): return {"task-1": {"status": "succeeded", "result": {"parsed_output": {"new": "42", "next": "43"}}}}
        def close(self): pass
    monkeypatch.setattr(llm_task_client, "LlmTaskClient", Client)
    monkeypatch.setattr(mining_config, "MiningConfig", lambda: SimpleNamespace(llm_service_url="http://test"))
    service.repo.progress = AsyncMock(return_value=True)
    service._run = {"id": "run", "metadata_json": {"domain": "domain"}}
    service._worker = "worker"
    snapshot = {"columns": [{"key": "text", "name": "text", "type": "text"}, {"key": "new", "name": "new", "type": "int"}],
                "rows": [{"row_id": "a", "text": "a", "new": 10, "_llm": {"status": "succeeded", "task_id": "old"}},
                         {"row_id": "b", "text": "b", "_llm": {"status": "failed"}}], "issues": [], "documents": {}}
    out = await service.llm_columns(snapshot, {"retry_failed": True, "columns": [{"key": "new", "type": "int"}], "input_columns": ["text"], "prompt": "extract"})
    assert len(calls) == 1 and calls[0]["expected_output_type"] == "json_object"
    assert [r["new"] for r in out["rows"]] == [10, 42]
    assert out["rows"][0]["_llm"]["task_id"] == "old"
    assert service._run["metadata_json"]["progress"]["checkpoint"]
    restored = await service.read_json(service._run["metadata_json"]["progress"]["checkpoint"])
    assert restored["rows"][1]["_llm"]["task_id"] == "task-1"
    # A second LLM operator must not mistake the previous step's successes for its own checkpoint.
    service._run = {"id": "second", "metadata_json": {"domain": "domain"}}
    next_output = await service.llm_columns(out, {"columns": [{"key": "next", "type": "int"}], "input_columns": ["text"], "prompt": "another operation"})
    assert len(calls) == 3
    assert next_output["rows"][0]["_llm_history"][0]["task_id"] == "old"
    assert next_output["rows"][0]["_llm"]["run_id"] == "second"


async def test_rejects_cross_domain_private_kb_and_missing_documents(service):
    service.kb_db.get_kb.return_value = {"domain": "another"}
    with pytest.raises(ArtifactNotFound):
        await service.submit({"id": "u"}, "a", "kb", ["doc"])
    service.repo.enqueue.assert_not_awaited()
    service.kb_db.get_kb.return_value = {"domain": "domain"}
    service.kbs._assert_read.side_effect = NotFound("kb")
    with pytest.raises(ArtifactNotFound):
        await service.submit({"id": "u"}, "a", "kb", ["doc"])
    service.kbs._assert_read.side_effect = None
    service.repo.freeze_sources.return_value = []
    with pytest.raises(InvalidOperation):
        await service.submit({"id": "u"}, "a", "kb", ["doc"])


async def test_snapshot_roundtrip_survives_new_service_and_paginates_without_truncation(service):
    rows, text = extract_rows(SOURCE, [TABLE], CELLS, IR)
    payload = {"rows": [rows[0] | {"row_id": str(n)} for n in range(51)], "documents": {"doc": text}, "issues": []}
    output = await service.write_snapshot(payload)
    reconstructed = copy.copy(service)
    run = {"id": "r", "status": "completed", "metadata_json": {"kb_id": "kb", "output": output},
           "workflow_manifest_json": extraction_manifest([SOURCE])}
    reconstructed.repo.get_run.return_value = run
    page = await reconstructed.page({"id": "u"}, "a", "r", 3, 20)
    assert page["total"] == 51 and len(page["rows"]) == 11
    assert page["rows"][0]["file_content"] == "参数说明"
    assert (await reconstructed.read_json(output["storage_object_id"])) == payload


async def test_snapshot_integrity_and_reauthorization(service):
    output = await service.write_snapshot({"rows": [], "documents": {}, "issues": []})
    key = output["storage_object_id"]
    service.objects.rows[key] = replace(service.objects.rows[key], sha256="0" * 64)
    with pytest.raises(InvalidOperation, match="校验失败"):
        await service.read_json(key)
    service.repo.get_run.return_value = {"metadata_json": {"kb_id": "kb"}}
    service.kbs._assert_read.side_effect = NotFound("kb")
    with pytest.raises(ArtifactNotFound):
        await service.page({"id": "u"}, "a", "r", 1, 20)


async def test_extraction_reports_each_missing_source_instead_of_silent_skips(service):
    service.read_json = AsyncMock(side_effect=[{"structured_assets": {}}, IR, OSError("missing")])
    service.repo.table_data.return_value = ([], [])
    sources = [SOURCE | {"id": "unmined", "snapshot_id": None}] + [
        SOURCE | {"id": key, "parse_ir_storage_object_id": "ir"} for key in ("empty", "pending", "bad")]
    result = await service.extract(sources)
    assert [i["reason"] for i in result["issues"]] == ["not_extracted", "no_tables", "projection_missing", "read_failed"]


async def test_execution_uses_registered_operator_and_durable_snapshot(service):
    manifest = extraction_manifest([SOURCE | {"parse_ir_storage_object_id": "ir"}])
    run = {"metadata_json": {"kb_id": "kb", "user_id": "u"},
           "workflow_manifest_json": manifest, "workflow_graph_hash": value_hash(manifest)}
    service.repo.get_run.return_value = run
    service.read_json = AsyncMock(return_value=IR)
    candidate = {"id": "r", "metadata_json": {"artifact_id": "a"}}
    await service.execute(candidate)
    output = service.repo.finish.call_args.kwargs["output"]
    assert output["row_count"] == 1
    assert output["storage_object_id"] in service.objects.rows
    assert table_handler_registry().resolve("table_extract", "1")


async def test_execution_does_not_run_without_claim_or_with_changed_manifest(service):
    service.repo.get_run.return_value = {"metadata_json": {"kb_id": "kb", "user_id": "u"},
                                       "workflow_manifest_json": {}, "workflow_graph_hash": "wrong"}
    service.repo.claim.return_value = None
    assert (await service.execute({"id": "r", "metadata_json": {"artifact_id": "a"}}))["status"] == "claimed_elsewhere"
    service.repo.table_data.assert_not_awaited()
    service.repo.claim.return_value = "event"
    await service.execute({"id": "r", "metadata_json": {"artifact_id": "a"}})
    assert service.repo.finish.call_args.kwargs["error"]
    service.repo.table_data.assert_not_awaited()


async def test_routes_validate_input_and_return_accepted_without_inline_execution(service):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import Mock
    from knowledge_mining.mining.kb.auth import current_user
    from knowledge_mining.mining.kb.routes.artifacts import router, get_pipeline_service

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: {"id": "u"}
    app.dependency_overrides[get_pipeline_service] = lambda: service
    app.state.domain_run_dispatcher = SimpleNamespace(kick=Mock())
    service.repo.list_runs.return_value = []
    service.repo.get_run.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post('/api/artifacts/a/pipeline/extract', json={"kb_id": "kb", "document_ids": []})
        assert invalid.status_code == 422
        response = await client.post('/api/artifacts/a/pipeline/extract', json={"kb_id": "kb", "document_ids": ["doc"]})
        assert response.status_code == 202 and response.json() == {"run_id": "run"}
        app.state.domain_run_dispatcher.kick.assert_called_once_with("domain")
        service.repo.claim.assert_not_awaited()
        assert (await client.get('/api/artifacts/a/pipeline/runs')).json() == {"items": []}
        assert (await client.get('/api/artifacts/a/pipeline/runs/unknown')).status_code == 404


async def test_queue_routes_new_and_recovered_table_runs_to_table_executor():
    from unittest.mock import Mock
    from knowledge_mining.mining.api.domain_run_queue import build_domain_run_dispatcher
    executor = Mock(return_value={"status": "done"})
    dispatcher = build_domain_run_dispatcher(None, None, artifact_executor=executor)
    candidates = iter([
        {"id": "first", "status": "queued", "metadata_json": {"profile": "artifact_table"}},
        {"id": "recovered", "status": "interrupted", "metadata_json": {"profile": "artifact_table"}},
        None,
    ])
    dispatcher._next_candidate = lambda domain: next(candidates)
    assert dispatcher.drain("domain") == 2
    assert [call.args[0]["id"] for call in executor.call_args_list] == ["first", "recovered"]
