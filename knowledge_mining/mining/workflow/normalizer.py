from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .core import MiningOperatorDef
from .graph import EdgeDef, NodeDef, WorkflowGraph


_PROTECTED_TYPES = {"entity_review_gate", "ontology_review_gate", "graph_write"}
_GLOBAL_CHAIN_TYPES = {
    "asset_persist",
    "entity_review_gate",
    "ontology_induction",
    "ontology_review_gate",
    "graph_write",
    "mining_finalize",
}


def required_protected_types(enabled_types: set[str]) -> tuple[str, ...]:
    has_entities = bool(
        enabled_types
        & {"entity_extract", "entity_resolve", "entity_relation_extract"}
    )
    has_induction = "ontology_induction" in enabled_types
    if has_induction:
        return ("entity_review_gate", "ontology_review_gate", "graph_write")
    if has_entities:
        return ("entity_review_gate", "graph_write")
    return ()


@dataclass(frozen=True)
class WorkflowNormalizer:
    catalog: Mapping[str, MiningOperatorDef]

    def normalize(self, graph: WorkflowGraph) -> WorkflowGraph:
        nodes = list(graph.nodes)
        enabled_types = {node.operator_type for node in nodes if not node.disabled}
        required = required_protected_types(enabled_types)
        existing_types = {node.operator_type for node in nodes}

        for operator_type in required:
            if operator_type not in existing_types:
                definition = self.catalog[operator_type]
                nodes.append(
                    NodeDef(
                        node_id=operator_type,
                        operator_type=operator_type,
                        operator_version=definition.version,
                        ui={},
                    )
                )
                existing_types.add(operator_type)

        by_type = {
            node.operator_type: node
            for node in nodes
            if not node.disabled and node.operator_type in _GLOBAL_CHAIN_TYPES
        }
        chain_types = ["asset_persist"]
        if "entity_review_gate" in required:
            chain_types.append("entity_review_gate")
        if "ontology_induction" in enabled_types:
            chain_types.append("ontology_induction")
        if "ontology_review_gate" in required:
            chain_types.append("ontology_review_gate")
        if "graph_write" in required:
            chain_types.append("graph_write")
        chain_types.append("mining_finalize")
        chain_ids = [by_type[item].node_id for item in chain_types if item in by_type]

        node_by_id = {node.node_id: node for node in nodes}
        edges = [
            edge
            for edge in graph.edges
            if not (
                edge.from_node in node_by_id
                and edge.to_node in node_by_id
                and node_by_id[edge.from_node].operator_type in _GLOBAL_CHAIN_TYPES
                and node_by_id[edge.to_node].operator_type in _GLOBAL_CHAIN_TYPES
            )
        ]
        edges.extend(
            EdgeDef(source, "finalizeInput", target, "finalizeInput")
            for source, target in zip(chain_ids, chain_ids[1:])
        )
        return WorkflowGraph(
            nodes=tuple(nodes),
            edges=tuple(edges),
            output=graph.output,
            schema_version=graph.schema_version,
        )
