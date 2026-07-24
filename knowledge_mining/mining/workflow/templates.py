from __future__ import annotations

from .graph import EdgeDef, NodeDef, OutputDef, WorkflowGraph


_POSITIONS = {
    "input_ingest": (40, 260),
    "parse_segment": (280, 260),
    "enrich": (520, 80),
    "discourse_line": (760, 80),
    "contextual_retrieval_enrich": (1000, 80),
    "retrieval_unit_build": (1240, 80),
    "embedding": (1480, 80),
    "entity_extract": (520, 440),
    "entity_resolve": (760, 440),
    "entity_relation_extract": (1000, 440),
    "asset_persist": (1720, 260),
    "entity_review_gate": (1960, 440),
    "ontology_induction": (2200, 440),
    "ontology_review_gate": (2440, 440),
    "graph_write": (2680, 440),
    "mining_finalize": (2920, 260),
}

_DOCUMENT_DISCOURSE_ORDER = (
    "enrich",
    "discourse_line",
    "contextual_retrieval_enrich",
    "retrieval_unit_build",
    "embedding",
)
_DOCUMENT_ONTOLOGY_ORDER = (
    "entity_extract",
    "entity_resolve",
    "entity_relation_extract",
)
_GLOBAL_ONTOLOGY_ORDER = (
    "entity_review_gate",
    "ontology_induction",
    "ontology_review_gate",
    "graph_write",
)

EDITABLE_BY_TEMPLATE = {
    "full": set(_DOCUMENT_DISCOURSE_ORDER)
    | set(_DOCUMENT_ONTOLOGY_ORDER)
    | {"ontology_induction"},
    "discourse_only": set(_DOCUMENT_DISCOURSE_ORDER),
    "ontology_only": set(_DOCUMENT_ONTOLOGY_ORDER) | {"ontology_induction"},
    "minimal": set(),
}


def _node(operator_type: str) -> NodeDef:
    x, y = _POSITIONS[operator_type]
    return NodeDef(
        node_id=operator_type,
        operator_type=operator_type,
        params={},
        ui={"x": x, "y": y},
    )


def _connect_chain(types: tuple[str, ...], slot: str) -> list[EdgeDef]:
    return [
        EdgeDef(source, slot, target, slot)
        for source, target in zip(types, types[1:])
    ]


def _template(template_name: str) -> WorkflowGraph:
    enabled = EDITABLE_BY_TEMPLATE[template_name]
    has_discourse = bool(enabled & set(_DOCUMENT_DISCOURSE_ORDER))
    has_ontology = bool(enabled & set(_DOCUMENT_ONTOLOGY_ORDER))

    types = ["input_ingest", "parse_segment"]
    if has_discourse:
        types.extend(_DOCUMENT_DISCOURSE_ORDER)
    if has_ontology:
        types.extend(_DOCUMENT_ONTOLOGY_ORDER)
    types.append("asset_persist")
    if has_ontology:
        types.extend(_GLOBAL_ONTOLOGY_ORDER)
    types.append("mining_finalize")

    edges = [
        EdgeDef("input_ingest", "rawFiles", "parse_segment", "rawFiles"),
        EdgeDef("parse_segment", "documents", "asset_persist", "documents"),
    ]
    if has_discourse:
        edges.append(
            EdgeDef("parse_segment", "documents", _DOCUMENT_DISCOURSE_ORDER[0], "documents")
        )
        edges.extend(_connect_chain(_DOCUMENT_DISCOURSE_ORDER, "documents"))
        edges.append(
            EdgeDef("embedding", "documents", "asset_persist", "discourseAssets")
        )
    if has_ontology:
        edges.append(
            EdgeDef("parse_segment", "documents", _DOCUMENT_ONTOLOGY_ORDER[0], "documents")
        )
        edges.extend(_connect_chain(_DOCUMENT_ONTOLOGY_ORDER, "documents"))
        edges.append(
            EdgeDef(
                "entity_relation_extract",
                "documents",
                "asset_persist",
                "ontologyAssets",
            )
        )
        global_chain = ("asset_persist",) + _GLOBAL_ONTOLOGY_ORDER + ("mining_finalize",)
    else:
        global_chain = ("asset_persist", "mining_finalize")
    edges.extend(_connect_chain(global_chain, "finalizeInput"))

    return WorkflowGraph(
        nodes=tuple(_node(operator_type) for operator_type in types),
        edges=tuple(edges),
        output=OutputDef("mining_finalize", "result"),
    )


_BUILTIN_TEMPLATES = {
    name: _template(name)
    for name in ("full", "discourse_only", "ontology_only", "minimal")
}


def builtin_templates() -> dict[str, WorkflowGraph]:
    return dict(_BUILTIN_TEMPLATES)
