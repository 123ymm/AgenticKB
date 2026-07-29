from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .core import _thaw_value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def graph_hash(graph: Any) -> str:
    return value_hash(graph.to_dict() if hasattr(graph, "to_dict") else graph)


def build_publish_manifest(
    workflow_id: str,
    workflow_version: int,
    plan: Any,
) -> dict[str, Any]:
    nodes = []
    for node in plan.nodes:
        params = _thaw_value(node.params)
        nodes.append(
            {
                "nodeId": node.node_id,
                "type": node.operator_type,
                "version": node.operator_version,
                "params": params,
                "paramsHash": value_hash(params),
                "requires": sorted(node.requires),
                "provides": sorted(node.provides),
                "errorPolicy": node.error_policy,
                "guard": node.guard,
            }
        )
    return {
        "schemaVersion": plan.schema_version,
        "catalogVersion": plan.catalog_version,
        "workflowId": workflow_id,
        "workflowVersion": workflow_version,
        "graphHash": graph_hash(plan.graph),
        "graph": plan.graph.to_dict(),
        "nodes": nodes,
        "edges": [edge.to_dict() for edge in plan.edges],
        "executionPlan": {
            "inputOrder": list(plan.input_order),
            "documentOrder": list(plan.document_order),
            "globalOrder": list(plan.global_order),
            "requiredCompletion": sorted(plan.required_completion),
        },
    }


def bind_run_manifest(published: dict[str, Any], **binding: Any) -> dict[str, Any]:
    result = deepcopy(published)
    result["runtimeBinding"] = {
        "domain": binding["domain"],
        "channel": binding["channel"],
        "ontologyVersionId": binding.get("ontology_version_id"),
        "ontologyApplicable": binding.get("ontology_version_id") is not None,
        "uploadBatchId": binding.get("upload_batch_id"),
        "configFingerprint": binding["config_fingerprint"],
    }
    return result
