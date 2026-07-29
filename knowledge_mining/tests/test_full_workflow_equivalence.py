from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

from knowledge_mining.mining.contracts.models import (
    DocumentProfile,
    RawSegmentData,
    SegmentRelationData,
)
from knowledge_mining.mining.pipeline import (
    DocumentContext,
    PipelineConfig,
    contextual_retrieval_stage,
    discourse_stage,
    embedding_stage,
    enrich_stage,
    entity_extract_stage,
    entity_relations_stage,
    resolve_stage,
    retrieval_units_stage,
)
from knowledge_mining.mining.workflow.core import DocumentState
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


APPROVED_EQUIVALENCE_DELTAS: tuple[dict[str, str], ...] = ()


class DeterministicEnricher:
    def enrich_batch(self, segments):
        return [
            replace(
                item,
                semantic_role="concept",
                metadata_json={**item.metadata_json, "quality": "authoritative"},
            )
            for item in segments
        ]


class DeterministicEntityExtractor:
    def extract_batch(self, segments, **kwargs):
        assert kwargs == {
            "min_confidence": 0.5,
            "allow_out_of_schema": True,
            "max_entities_per_segment": 20,
        }
        return [
            replace(
                item,
                entity_refs_json=[{
                    "type": "network_function",
                    "name": "AMF",
                    "confidence": "0.99",
                }],
            )
            for item in segments
        ]


class DeterministicResolver:
    def resolve_batch(self, segments):
        return [
            replace(
                item,
                entity_refs_json=[{
                    **ref,
                    "canonical_name": ref["name"],
                    "resolve_status": "resolved",
                } for ref in item.entity_refs_json],
            )
            for item in segments
        ]


class DeterministicEntityRelationBuilder:
    def build_batch(self, segments):
        return [
            replace(
                item,
                metadata_json={
                    **item.metadata_json,
                    "relationship_candidates": [{
                        "source": "AMF",
                        "predicate": "described_by",
                        "target": item.document_key,
                    }],
                },
            )
            for item in segments
        ]


class DeterministicDiscourseBuilder:
    def build(self, segments, **kwargs):
        assert kwargs["min_confidence"] == 0.5
        key = f"{segments[0].document_key}#{segments[0].segment_index}"
        return [SegmentRelationData(key, key, "elaborates", confidence=0.91)]


class DeterministicContextualizer:
    last_task_ids = {"doc:/a.md#0": "context-task-1"}

    def contextualize(self, segments, document_text):
        assert "AMF" in document_text
        return {
            f"{item.document_key}#{item.segment_index}": "5GC control-plane context"
            for item in segments
        }


class DeterministicEmbeddingGenerator:
    def embed_batch(self, texts):
        return [[float(len(text)), 1.0] for text in texts]


def pipeline_config() -> PipelineConfig:
    return PipelineConfig(
        domain="plant-a",
        enricher=DeterministicEnricher(),
        entity_extractor=DeterministicEntityExtractor(),
        resolver=DeterministicResolver(),
        entity_relation_builder=DeterministicEntityRelationBuilder(),
        discourse_relation_builder=DeterministicDiscourseBuilder(),
        contextualizer=DeterministicContextualizer(),
        embedding_generator=DeterministicEmbeddingGenerator(),
    )


def initial_context() -> DocumentContext:
    return DocumentContext(
        profile=DocumentProfile(document_key="doc:/a.md", title="AMF"),
        segments=(RawSegmentData(
            document_key="doc:/a.md",
            segment_index=0,
            block_type="paragraph",
            raw_text="AMF controls registration and mobility for 5GC subscribers.",
            normalized_text="amf controls registration and mobility for 5gc subscribers.",
            token_count=64,
            metadata_json={"source": "fixture"},
        ),),
        seg_ids={"doc:/a.md#0": "segment-0"},
        run_document_id="run-document-1",
    )


def run_legacy_document_line(ctx: DocumentContext, cfg: PipelineConfig) -> DocumentContext:
    """The pre-operatorization document order, using the production stages."""

    ctx = enrich_stage(ctx, cfg, options=EnrichOptions())
    ctx = entity_extract_stage(ctx, cfg, options=EntityExtractOptions())
    ctx = resolve_stage(ctx, cfg, options=EntityResolveOptions())
    ctx = entity_relations_stage(ctx, cfg, options=EntityRelationOptions())
    ctx = discourse_stage(ctx, cfg, options=DiscourseOptions())
    ctx = contextual_retrieval_stage(ctx, cfg, options=EmptyOptions())
    ctx = retrieval_units_stage(
        ctx,
        cfg,
        options=RetrievalUnitOptions(
            generatedQuestionUnit=False,
            tableRowUnit=False,
        ),
    )
    return embedding_stage(ctx, cfg, options=EmbeddingOptions())


def run_full_workflow_document_line(
    ctx: DocumentContext, cfg: PipelineConfig
) -> DocumentContext:
    """The FULL template's two document branches and their production merge."""

    discourse = enrich_stage(ctx, cfg, options=EnrichOptions())
    discourse = discourse_stage(discourse, cfg, options=DiscourseOptions())
    discourse = contextual_retrieval_stage(discourse, cfg, options=EmptyOptions())
    discourse = retrieval_units_stage(
        discourse,
        cfg,
        options=RetrievalUnitOptions(
            generatedQuestionUnit=False,
            tableRowUnit=False,
        ),
    )
    discourse = embedding_stage(discourse, cfg, options=EmbeddingOptions())

    ontology = entity_extract_stage(ctx, cfg, options=EntityExtractOptions())
    ontology = resolve_stage(ontology, cfg, options=EntityResolveOptions())
    ontology = entity_relations_stage(
        ontology, cfg, options=EntityRelationOptions()
    )

    merged = DocumentState.merge_batches([
        (DocumentState("run-document-1", "doc:/a.md", discourse),),
        (DocumentState("run-document-1", "doc:/a.md", ontology),),
    ])
    return merged[0].context


def normalize_business_output(ctx: DocumentContext) -> dict:
    return {
        "segments": [asdict(item) for item in ctx.segments],
        "relations": [asdict(item) for item in ctx.relations],
        "retrieval_units": [asdict(item) for item in ctx.retrieval_units],
        "embeddings": [dict(item) for item in ctx.embeddings],
    }


def test_system_full_document_output_matches_legacy_without_approved_deltas() -> None:
    cfg = pipeline_config()

    legacy = run_legacy_document_line(initial_context(), cfg)
    workflow = run_full_workflow_document_line(initial_context(), cfg)

    assert APPROVED_EQUIVALENCE_DELTAS == ()
    assert normalize_business_output(workflow) == normalize_business_output(legacy)
