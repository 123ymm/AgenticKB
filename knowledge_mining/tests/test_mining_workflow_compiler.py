from dataclasses import replace

from knowledge_mining.mining.workflow.compiler import WorkflowCompiler
from knowledge_mining.mining.workflow.graph import EdgeDef, NodeDef
from knowledge_mining.mining.workflow.operators.catalog import builtin_catalog
from knowledge_mining.mining.workflow.templates import builtin_templates


def _compile(graph):
    return WorkflowCompiler(builtin_catalog()).compile(graph, mode="publish")


def test_full_compiles_into_input_document_and_global_zones() -> None:
    result = _compile(builtin_templates()["full"])
    assert result.valid is True
    assert result.plan is not None
    assert result.plan.input_order == ("input_ingest",)
    assert result.plan.document_order[0] == "parse_segment"
    assert result.plan.document_order[-1] == "asset_persist"
    assert result.plan.global_order == (
        "entity_review_gate",
        "ontology_induction",
        "ontology_review_gate",
        "graph_write",
        "mining_finalize",
    )


def test_normalizer_restores_protected_chain_but_not_missing_fixed_nodes() -> None:
    full = builtin_templates()["full"]
    damaged = replace(
        full,
        nodes=tuple(node for node in full.nodes if node.operator_type != "graph_write"),
        edges=tuple(
            edge
            for edge in full.edges
            if edge.from_node != "graph_write" and edge.to_node != "graph_write"
        ),
    )
    result = _compile(damaged)
    assert result.valid is True
    assert result.plan is not None
    assert "graph_write" in result.plan.global_order

    missing_fixed = replace(
        full,
        nodes=tuple(
            node for node in full.nodes if node.operator_type != "asset_persist"
        ),
    )
    invalid = _compile(missing_fixed)
    assert "missing_fixed_operator" in {error.kind for error in invalid.errors}


def test_compiler_rejects_capability_gap_cycle_and_ordering_error() -> None:
    graph = builtin_templates()["discourse_only"]
    bad_edges = tuple(
        edge
        for edge in graph.edges
        if not (
            edge.from_node == "retrieval_unit_build" and edge.to_node == "embedding"
        )
    ) + (EdgeDef("embedding", "documents", "retrieval_unit_build", "documents"),)
    result = _compile(replace(graph, edges=bad_edges))
    kinds = {error.kind for error in result.errors}
    assert "cycle" in kinds or "missing_capability" in kinds


def test_ontology_nodes_carry_a_runtime_guard() -> None:
    plan = _compile(builtin_templates()["full"]).require_plan()
    guarded = {
        "entity_extract",
        "entity_resolve",
        "entity_relation_extract",
        "entity_review_gate",
        "ontology_induction",
        "ontology_review_gate",
        "graph_write",
    }
    assert {
        node.operator_type
        for node in plan.nodes
        if node.guard == "ontology_applicable"
    } == guarded


def test_publish_rejects_disabled_or_duplicate_fixed_nodes() -> None:
    minimal = builtin_templates()["minimal"]
    disabled = replace(
        minimal,
        nodes=tuple(
            replace(node, disabled=True)
            if node.operator_type == "parse_segment"
            else node
            for node in minimal.nodes
        ),
    )
    assert "disabled_fixed_operator" in {error.kind for error in _compile(disabled).errors}

    duplicate = replace(
        minimal,
        nodes=minimal.nodes + (NodeDef("parse-copy", "parse_segment"),),
    )
    assert "duplicate_operator" in {error.kind for error in _compile(duplicate).errors}


def test_publish_rejects_incompatible_slots_and_orphan_document_output() -> None:
    discourse = builtin_templates()["discourse_only"]
    incompatible = replace(
        discourse,
        edges=discourse.edges
        + (EdgeDef("input_ingest", "rawFiles", "embedding", "documents"),),
    )
    assert "incompatible_slot" in {error.kind for error in _compile(incompatible).errors}

    orphaned = replace(
        discourse,
        edges=tuple(
            edge
            for edge in discourse.edges
            if not (edge.from_node == "embedding" and edge.to_node == "asset_persist")
        ),
    )
    assert "orphan_document_output" in {error.kind for error in _compile(orphaned).errors}


def test_resolved_entity_parameter_promotes_capability_requirement() -> None:
    ontology = builtin_templates()["ontology_only"]
    configured = replace(
        ontology,
        nodes=tuple(
            replace(node, params={"requireResolvedEntities": True})
            if node.operator_type == "entity_relation_extract"
            else node
            for node in ontology.nodes
        ),
    )
    assert _compile(configured).valid is True

    without_resolve_edge = replace(
        configured,
        edges=tuple(
            edge
            for edge in configured.edges
            if edge.from_node != "entity_resolve"
        )
        + (
            EdgeDef(
                "entity_extract",
                "documents",
                "entity_relation_extract",
                "documents",
            ),
        ),
    )
    assert "missing_capability" in {
        error.kind for error in _compile(without_resolve_edge).errors
    }
