from __future__ import annotations

from typing import Any


def normalize_document_output(ctx: Any) -> dict[str, Any]:
    """Return the stable business fields used for legacy/workflow comparison."""

    return {
        "document_key": ctx.profile.document_key if ctx.profile else "",
        "run_document_id": ctx.run_document_id,
        "segments": [
            {
                "index": item.segment_index,
                "block_type": item.block_type,
                "role": item.semantic_role,
                "text": item.normalized_text or item.raw_text,
                "entities": sorted(
                    (
                        {"name": ref.get("name"), "type": ref.get("type")}
                        for ref in item.entity_refs_json
                    ),
                    key=lambda ref: (str(ref["type"]), str(ref["name"])),
                ),
            }
            for item in ctx.segments
        ],
        "relations": [
            {
                "source": item.source_segment_key,
                "target": item.target_segment_key,
                "type": item.relation_type,
                "confidence": item.confidence,
            }
            for item in ctx.relations
        ],
        "retrieval_units": [
            {"key": item.unit_key, "type": item.unit_type, "text": item.text}
            for item in ctx.retrieval_units
        ],
        "embeddings": [dict(item) for item in ctx.embeddings],
    }
