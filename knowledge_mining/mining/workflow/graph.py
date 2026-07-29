from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .core import _freeze_value, _thaw_value


@dataclass(frozen=True)
class NodeDef:
    node_id: str
    operator_type: str
    operator_version: str = "1"
    params: Mapping[str, Any] = field(default_factory=dict)
    ui: Mapping[str, Any] = field(default_factory=dict)
    disabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _freeze_value(self.params))
        object.__setattr__(self, "ui", _freeze_value(self.ui))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeDef":
        return cls(
            node_id=str(value.get("nodeId", "")),
            operator_type=str(value.get("operatorType", "")),
            operator_version=str(value.get("operatorVersion", "1")),
            params=dict(value.get("params") or {}),
            ui=dict(value.get("ui") or {}),
            disabled=bool(value.get("disabled", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "nodeId": self.node_id,
            "operatorType": self.operator_type,
            "operatorVersion": self.operator_version,
            "params": _thaw_value(self.params),
            "ui": _thaw_value(self.ui),
        }
        if self.disabled:
            value["disabled"] = True
        return value


@dataclass(frozen=True)
class EdgeDef:
    from_node: str
    from_slot: str
    to_node: str
    to_slot: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EdgeDef":
        return cls(
            str(value.get("fromNode", "")),
            str(value.get("fromSlot", "")),
            str(value.get("toNode", "")),
            str(value.get("toSlot", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "fromNode": self.from_node,
            "fromSlot": self.from_slot,
            "toNode": self.to_node,
            "toSlot": self.to_slot,
        }


@dataclass(frozen=True)
class OutputDef:
    node_id: str
    slot: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputDef":
        return cls(str(value.get("nodeId", "")), str(value.get("slot", "")))

    def to_dict(self) -> dict[str, str]:
        return {"nodeId": self.node_id, "slot": self.slot}


@dataclass(frozen=True)
class WorkflowGraph:
    nodes: tuple[NodeDef, ...]
    edges: tuple[EdgeDef, ...]
    output: OutputDef
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowGraph":
        return cls(
            nodes=tuple(NodeDef.from_dict(item) for item in value.get("nodes", [])),
            edges=tuple(EdgeDef.from_dict(item) for item in value.get("edges", [])),
            output=OutputDef.from_dict(value.get("output") or {}),
            schema_version=str(value.get("schemaVersion", "1.0")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "output": self.output.to_dict(),
        }
