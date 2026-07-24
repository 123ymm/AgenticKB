from __future__ import annotations

from typing import Any, Mapping

from ..core import OperatorResult, OperatorStatus, OperatorWarning
from ..operators.options import EmptyOptions


def _run_id(runtime: Any) -> str:
    value = runtime.manifest.get("runId") or runtime.manifest.get("run_id")
    if not value:
        raise ValueError("Workflow Manifest has no Run identity")
    return str(value)


def _success(state: Any, capability: str) -> OperatorResult:
    return OperatorResult(
        state,
        frozenset({capability}),
        OperatorStatus.SUCCESS,
    )


def _not_applicable(state: Any) -> OperatorResult:
    return OperatorResult(
        state,
        frozenset({"ontology_not_applicable"}),
        OperatorStatus.NOT_APPLICABLE,
    )


def entity_review_gate_handler(
    state: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    EmptyOptions.model_validate(dict(params))
    if runtime.ontology_version_id is None:
        return _not_applicable(state)
    pending = runtime.services.count_pending_entity_mentions(_run_id(runtime))
    if pending:
        return OperatorResult(
            state,
            frozenset(),
            OperatorStatus.PAUSED,
            warnings=(OperatorWarning(
                "entity_review_required",
                f"{pending} entity mentions require review",
                {"pendingCount": pending},
            ),),
        )
    return _success(state, "entity_review_approved")


def ontology_induction_handler(
    state: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    EmptyOptions.model_validate(dict(params))
    if runtime.ontology_version_id is None:
        return _not_applicable(state)
    runtime.services.run_ontology_induction(
        _run_id(runtime), "ontology_induction"
    )
    return _success(state, "ontology_candidates")


def ontology_review_gate_handler(
    state: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    EmptyOptions.model_validate(dict(params))
    if runtime.ontology_version_id is None:
        return _not_applicable(state)
    pending = runtime.services.count_pending_ontology_candidates(runtime.domain)
    if pending:
        return OperatorResult(
            state,
            frozenset(),
            OperatorStatus.PAUSED,
            warnings=(OperatorWarning(
                "ontology_review_required",
                f"{pending} ontology candidates require review",
                {"pendingCount": pending},
            ),),
        )
    return _success(state, "ontology_review_approved")


def graph_write_handler(
    state: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    EmptyOptions.model_validate(dict(params))
    if runtime.ontology_version_id is None:
        return _not_applicable(state)
    runtime.services.write_graph_strict(_run_id(runtime))
    return _success(state, "graph_written")


GLOBAL_HANDLERS = {
    "entity_review_gate": entity_review_gate_handler,
    "ontology_induction": ontology_induction_handler,
    "ontology_review_gate": ontology_review_gate_handler,
    "graph_write": graph_write_handler,
}
