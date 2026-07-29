from __future__ import annotations

from typing import Any, Mapping

from ..core import OperatorResult, OperatorStatus
from ..operators.options import FinalizeOptions


def mining_finalize_handler(
    state: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    options = FinalizeOptions.model_validate(dict(params))
    required = set(
        (runtime.manifest.get("executionPlan") or {}).get(
            "requiredCompletion", []
        )
    )
    required.discard("finalized")
    binding = runtime.manifest.get("runtimeBinding") or {}
    if binding.get("ontologyApplicable") is False:
        required -= {
            "entity_review_approved",
            "ontology_review_approved",
            "graph_written",
        }
    missing = required - set(getattr(state, "capabilities", frozenset()))
    if missing:
        raise RuntimeError(
            "Cannot finalize before capabilities: " + ", ".join(sorted(missing))
        )
    run_id = runtime.manifest.get("runId") or runtime.manifest.get("run_id")
    run_id = run_id or getattr(runtime.services, "run_id", None)
    if not run_id:
        raise ValueError("Workflow Manifest has no Run identity")
    execution_mode = getattr(runtime.services, "execution_mode", "publish")
    summary = runtime.services.finalize_mining(
        str(run_id),
        execution_mode=execution_mode,
        publish_on_partial_failure=options.publish_on_partial_failure,
    )
    capabilities = {"finalized"}
    if summary.get("release_id"):
        capabilities.add("release_published")
    return OperatorResult(
        state,
        frozenset(capabilities),
        OperatorStatus.SUCCESS,
    )
