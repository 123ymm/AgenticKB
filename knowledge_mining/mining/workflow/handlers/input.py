from __future__ import annotations

from typing import Any, Mapping

from ..core import OperatorResult, OperatorStatus, OperatorWarning


def input_ingest_handler(
    input_spec: Any, params: Mapping[str, Any], runtime: Any
) -> OperatorResult:
    del params
    ingest = getattr(runtime.services, "input_ingest", None)
    if ingest is None:
        return OperatorResult(
            None,
            frozenset(),
            OperatorStatus.FAILED,
            error_code="input_ingest_unavailable",
            error_message="Input ingest service is not configured",
        )
    try:
        output = ingest(input_spec, runtime)
    except Exception as exc:
        return OperatorResult(
            None,
            frozenset(),
            OperatorStatus.FAILED,
            warnings=(OperatorWarning("input_ingest_failed", str(exc)),),
            error_code="input_ingest_failed",
            error_message=str(exc),
        )
    if output is None:
        return OperatorResult(None, frozenset(), OperatorStatus.SKIPPED)
    return OperatorResult(
        output, frozenset({"raw_files"}), OperatorStatus.SUCCESS
    )
