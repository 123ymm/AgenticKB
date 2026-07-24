from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .core import _freeze_value, _thaw_value
from .graph import EdgeDef, WorkflowGraph


@dataclass(frozen=True)
class PlannedNode:
    node_id: str
    operator_type: str
    operator_version: str
    params: Mapping[str, Any]
    requires: frozenset[str]
    provides: frozenset[str]
    error_policy: str
    guard: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _freeze_value(self.params))
        object.__setattr__(self, "requires", frozenset(self.requires))
        object.__setattr__(self, "provides", frozenset(self.provides))

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "type": self.operator_type,
            "version": self.operator_version,
            "params": _thaw_value(self.params),
            "requires": sorted(self.requires),
            "provides": sorted(self.provides),
            "errorPolicy": self.error_policy,
            "guard": self.guard,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    graph: WorkflowGraph
    nodes: tuple[PlannedNode, ...]
    edges: tuple[EdgeDef, ...]
    input_order: tuple[str, ...]
    document_order: tuple[str, ...]
    global_order: tuple[str, ...]
    required_completion: frozenset[str]
    schema_version: str = "1.0"
    catalog_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "input_order", tuple(self.input_order))
        object.__setattr__(self, "document_order", tuple(self.document_order))
        object.__setattr__(self, "global_order", tuple(self.global_order))
        object.__setattr__(self, "required_completion", frozenset(self.required_completion))

    def node(self, node_id: str) -> PlannedNode:
        try:
            return next(item for item in self.nodes if item.node_id == node_id)
        except StopIteration as exc:
            raise KeyError(node_id) from exc

    def to_manifest(self, *, workflow_id: str, workflow_version: int) -> dict[str, Any]:
        from .manifest import build_publish_manifest

        return build_publish_manifest(workflow_id, workflow_version, self)
