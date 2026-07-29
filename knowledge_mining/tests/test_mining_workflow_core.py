from knowledge_mining.mining.workflow.core import (
    EditPolicy,
    ErrorPolicy,
    ExecutionZone,
    SlotDecl,
    SlotType,
)
from knowledge_mining.mining.workflow.graph import (
    EdgeDef,
    NodeDef,
    OutputDef,
    WorkflowGraph,
)


def test_slot_contract_has_only_five_stable_payload_types() -> None:
    assert {item.value for item in SlotType} == {
        "INPUT_SPEC",
        "RAW_FILE_BATCH",
        "DOCUMENT_BATCH",
        "FINALIZE_INPUT",
        "FINALIZE_RESULT",
    }


def test_graph_round_trip_preserves_params_and_ui_but_not_domain() -> None:
    raw = {
        "schemaVersion": "1.0",
        "nodes": [
            {
                "nodeId": "parse",
                "operatorType": "parse_segment",
                "operatorVersion": "1",
                "params": {"maxSegmentTokens": 512},
                "ui": {"x": 20, "y": 30},
            }
        ],
        "edges": [],
        "output": {"nodeId": "parse", "slot": "documents"},
        "domain": "must-not-survive",
    }

    graph = WorkflowGraph.from_dict(raw)

    assert graph.to_dict() == {
        key: raw[key]
        for key in ("schemaVersion", "nodes", "edges", "output")
    }


def test_policy_enums_are_explicit() -> None:
    assert EditPolicy.FIXED.value == "fixed"
    assert EditPolicy.PROTECTED.value == "protected"
    assert EditPolicy.EDITABLE.value == "editable"
    assert ExecutionZone.DOCUMENT.value == "document"
    assert ErrorPolicy.PAUSE_FOR_REVIEW.value == "PAUSE_FOR_REVIEW"
    assert SlotDecl("documents", SlotType.DOCUMENT_BATCH).to_dict()["type"] == "DOCUMENT_BATCH"


def test_graph_values_are_immutable_copies() -> None:
    params = {"maxSegmentTokens": 512}
    ui = {"x": 20}
    node = NodeDef("parse", "parse_segment", params=params, ui=ui)
    graph = WorkflowGraph(
        nodes=(node,),
        edges=(),
        output=OutputDef("parse", "documents"),
    )

    params["maxSegmentTokens"] = 1024
    ui["x"] = 99

    assert graph.to_dict()["nodes"][0]["params"] == {"maxSegmentTokens": 512}
    assert graph.to_dict()["nodes"][0]["ui"] == {"x": 20}


def test_edge_round_trip_uses_explicit_slot_names() -> None:
    edge = EdgeDef.from_dict(
        {
            "fromNode": "parse",
            "fromSlot": "documents",
            "toNode": "persist",
            "toSlot": "documents",
        }
    )
    assert edge.to_dict() == {
        "fromNode": "parse",
        "fromSlot": "documents",
        "toNode": "persist",
        "toSlot": "documents",
    }
