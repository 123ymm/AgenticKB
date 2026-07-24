from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_mining.mining.contracts.models import (
    DocumentProfile,
    RawSegmentData,
)
from knowledge_mining.mining.pipeline import DocumentContext, PipelineConfig
from knowledge_mining.mining.workflow.core import (
    DocumentState,
    OperatorResult,
    OperatorStatus,
)
from knowledge_mining.mining.workflow.handler_registry import (
    HandlerRegistry,
    UnsafeRunOverride,
    UnsupportedOperatorVersion,
    resolve_effective_parameters,
)
from knowledge_mining.mining.workflow.handlers import document as handlers
from knowledge_mining.mining.workflow.handlers.document import enrich_handler
from knowledge_mining.mining.workflow.operators.options import (
    DiscourseOptions,
    EmbeddingOptions,
    EmptyOptions,
    EnrichOptions,
    EntityExtractOptions,
    EntityRelationOptions,
    EntityResolveOptions,
    RetrievalUnitOptions,
)


def _segment(index: int = 0, *, metadata: dict | None = None) -> RawSegmentData:
    return RawSegmentData(
        document_key="doc:/a",
        segment_index=index,
        block_type="paragraph",
        raw_text="AMF behavior is described in sufficient detail.",
        normalized_text="amf behavior is described in sufficient detail.",
        token_count=20,
        metadata_json=metadata or {},
        entity_refs_json=[{"type": "network_element", "name": "AMF"}],
    )


def state(
    run_document_id: str,
    *,
    doc_key: str | None = None,
    tags: tuple[str, ...] = (),
    capabilities: frozenset[str] = frozenset(),
) -> DocumentState:
    key = doc_key or f"doc:/{run_document_id}"
    return DocumentState(
        run_document_id=run_document_id,
        doc_key=key,
        context=DocumentContext(
            profile=DocumentProfile(document_key=key),
            segments=(_segment(metadata={"nested": {"value": 1}}),),
            run_document_id=run_document_id,
        ),
        capabilities=capabilities,
        tags=tags,
    )


def runtime(*, ontology_version_id: str | None = "ontology-v1", **services):
    pipeline_config = PipelineConfig(domain="odn", **services)
    return SimpleNamespace(
        domain="odn",
        channel="prod",
        domain_profile=pipeline_config.domain_profile,
        ontology_version_id=ontology_version_id,
        services=SimpleNamespace(pipeline_config=pipeline_config),
        manifest={"runOverrides": {}},
    )


def test_registry_resolves_exact_operator_version() -> None:
    def parse_segment_handler():
        pass

    registry = HandlerRegistry()
    registry.register("parse_segment", "1.0.0", parse_segment_handler)

    assert registry.resolve("parse_segment", "1.0.0") is parse_segment_handler
    with pytest.raises(UnsupportedOperatorVersion):
        registry.resolve("parse_segment", "1.0.1")


def test_registry_rejects_duplicate_registration() -> None:
    registry = HandlerRegistry()
    registry.register("parse_segment", "1", lambda: None)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("parse_segment", "1", lambda: None)


def test_branch_merge_uses_document_identity_not_position() -> None:
    left = DocumentState.batch([
        state("doc-a", tags=("left",)), state("doc-b")
    ])
    right = DocumentState.batch([
        state("doc-b", tags=("right",)), state("doc-a")
    ])

    merged = DocumentState.merge_batches([left, right])

    assert [item.run_document_id for item in merged] == ["doc-a", "doc-b"]
    assert merged[0].tags == ("left",)
    assert merged[1].tags == ("right",)


def test_branch_merge_rejects_duplicate_missing_and_identity_drift() -> None:
    with pytest.raises(ValueError, match="duplicate run_document_id"):
        DocumentState.batch([state("doc-a"), state("doc-a")])
    with pytest.raises(ValueError, match="identity set"):
        DocumentState.merge_batches([
            DocumentState.batch([state("doc-a"), state("doc-b")]),
            DocumentState.batch([state("doc-a")]),
        ])
    with pytest.raises(ValueError, match="doc_key drift"):
        DocumentState.merge_batches([
            DocumentState.batch([state("doc-a", doc_key="doc:/a")]),
            DocumentState.batch([state("doc-a", doc_key="doc:/other")]),
        ])


def test_fork_deep_copies_nested_document_values() -> None:
    original = state("doc-a")
    forked = original.fork()

    forked.context.segments[0].metadata_json["nested"]["value"] = 2

    assert original.context.segments[0].metadata_json["nested"]["value"] == 1
    assert forked.context.segments[0].metadata_json["nested"]["value"] == 2


def test_effective_parameter_precedence_and_override_allowlist() -> None:
    effective = resolve_effective_parameters(
        algorithm_defaults={"maxWorkers": 1, "publishOnPartialFailure": False},
        domain_defaults={"maxWorkers": 2, "publishOnPartialFailure": False},
        workflow_params={"maxWorkers": 3, "publishOnPartialFailure": False},
        run_overrides={"maxWorkers": 4, "publishOnPartialFailure": True},
    )

    assert effective == {"maxWorkers": 4, "publishOnPartialFailure": True}
    with pytest.raises(UnsafeRunOverride, match="llmBaseUrl"):
        resolve_effective_parameters(
            algorithm_defaults={},
            domain_defaults={},
            workflow_params={},
            run_overrides={"llmBaseUrl": "https://unsafe"},
        )


def test_enrich_handler_returns_fallback_without_losing_segments() -> None:
    class Enricher:
        def enrich_batch(self, segments):
            raise RuntimeError("llm unavailable")

    source = state("doc-a")
    result = enrich_handler(
        source,
        {"minEnrichTokens": 10},
        runtime(enricher=Enricher()),
    )

    assert result.status is OperatorStatus.FALLBACK
    assert result.outputs.context.segments == source.context.segments
    assert result.warnings[0].code == "enrich_fallback"


def test_handler_status_contract_covers_all_executor_outcomes() -> None:
    values = {
        OperatorResult(None, frozenset(), status).status
        for status in OperatorStatus
    }
    assert values == {
        OperatorStatus.SUCCESS,
        OperatorStatus.SKIPPED,
        OperatorStatus.FALLBACK,
        OperatorStatus.FAILED,
        OperatorStatus.PAUSED,
        OperatorStatus.NOT_APPLICABLE,
    }


@pytest.mark.parametrize(
    ("handler", "params", "stage_name", "expected_type"),
    [
        (handlers.enrich_handler, {"minEnrichTokens": 11}, "enrich_stage", EnrichOptions),
        (
            handlers.discourse_line_handler,
            {"windowSize": 4, "minConfidence": 0.7},
            "discourse_stage",
            DiscourseOptions,
        ),
        (
            handlers.contextual_retrieval_enrich_handler,
            {},
            "contextual_retrieval_stage",
            EmptyOptions,
        ),
        (
            handlers.retrieval_unit_build_handler,
            {"maxQuestionsPerSegment": 1},
            "retrieval_units_stage",
            RetrievalUnitOptions,
        ),
        (
            handlers.embedding_handler,
            {"unitTypes": ["raw_text"]},
            "embedding_stage",
            EmbeddingOptions,
        ),
        (
            handlers.entity_extract_handler,
            {"minConfidence": 0.8},
            "entity_extract_stage",
            EntityExtractOptions,
        ),
        (
            handlers.entity_resolve_handler,
            {"autoResolveAliases": False},
            "resolve_stage",
            EntityResolveOptions,
        ),
        (
            handlers.entity_relation_extract_handler,
            {"requireResolvedEntities": True},
            "entity_relations_stage",
            EntityRelationOptions,
        ),
    ],
)
def test_document_handlers_convert_all_editable_operator_params(
    monkeypatch, handler, params, stage_name, expected_type
) -> None:
    captured = []

    def stage_fn(ctx, cfg, **kwargs):
        captured.append(kwargs.get("options"))
        return ctx

    monkeypatch.setattr(handlers, stage_name, stage_fn)

    result = handler(state("doc-a"), params, runtime())

    assert result.status in {OperatorStatus.SUCCESS, OperatorStatus.FALLBACK}
    assert isinstance(captured[0], expected_type)


def test_ontology_document_handler_is_not_applicable_without_frozen_ontology() -> None:
    result = handlers.entity_extract_handler(
        state("doc-a"), {}, runtime(ontology_version_id=None)
    )

    assert result.status is OperatorStatus.NOT_APPLICABLE
    assert "ontology_not_applicable" in result.capabilities
