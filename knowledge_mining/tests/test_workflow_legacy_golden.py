from knowledge_mining.mining.contracts.models import (
    DocumentProfile,
    RawFileData,
    RawSegmentData,
    RetrievalUnitData,
    SegmentRelationData,
)
from knowledge_mining.mining.pipeline import DocumentContext
from knowledge_mining.tests.workflow_golden import normalize_document_output


def test_normalize_document_output_is_stable() -> None:
    ctx = DocumentContext(
        raw_file=RawFileData(
            file_path="C:/fixture/a.md",
            relative_path="a.md",
            file_name="a.md",
            file_type="markdown",
            content="# A\nbody",
            raw_content_hash="raw",
            normalized_content_hash="norm",
        ),
        profile=DocumentProfile(document_key="doc:/a.md", title="A"),
        segments=(
            RawSegmentData(
                document_key="doc:/a.md",
                segment_index=0,
                block_type="paragraph",
                raw_text="body",
                normalized_text="body",
                token_count=1,
                entity_refs_json=[{"type": "NF", "name": "AMF"}],
            ),
        ),
        relations=(
            SegmentRelationData(
                "doc:/a.md#0",
                "doc:/a.md#0",
                "elaborates",
                confidence=0.8,
            ),
        ),
        retrieval_units=(
            RetrievalUnitData(
                segment_key="doc:/a.md#0",
                unit_key="ru:0",
                unit_type="raw_text",
                target_type="raw_segment",
                text="body",
                search_text="body",
            ),
        ),
        embeddings=({"unit_key": "ru:0", "vector": [0.1, 0.2]},),
        run_document_id="rd-1",
    )

    assert normalize_document_output(ctx) == {
        "document_key": "doc:/a.md",
        "run_document_id": "rd-1",
        "segments": [
            {
                "index": 0,
                "block_type": "paragraph",
                "role": "unknown",
                "text": "body",
                "entities": [{"name": "AMF", "type": "NF"}],
            }
        ],
        "relations": [
            {
                "source": "doc:/a.md#0",
                "target": "doc:/a.md#0",
                "type": "elaborates",
                "confidence": 0.8,
            }
        ],
        "retrieval_units": [{"key": "ru:0", "type": "raw_text", "text": "body"}],
        "embeddings": [{"unit_key": "ru:0", "vector": [0.1, 0.2]}],
    }
