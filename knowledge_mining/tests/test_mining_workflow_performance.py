from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from statistics import median
from time import perf_counter, sleep

import pytest

from knowledge_mining.mining.workflow.core import OperatorResult, OperatorStatus
from knowledge_mining.mining.workflow.executors.document_executor import DocumentExecutor
from knowledge_mining.mining.workflow.handler_registry import HandlerRegistry
from knowledge_mining.tests.test_mining_document_executor import (
    FakeEventRepository,
    document_state,
    node,
    plan,
    runtime,
)


DOCUMENT_COUNT = 100
MAX_WORKERS = 4
OPERATOR_LATENCY_SECONDS = 0.002


def deterministic_work(state):
    current = state
    for _ in range(3):
        sleep(OPERATOR_LATENCY_SECONDS)
        current = current.with_context(current.context)
    return current


def measure_legacy(states) -> float:
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(deterministic_work, states))
    return perf_counter() - started


def workflow_registry() -> HandlerRegistry:
    registry = HandlerRegistry()

    def handler(state, params, runtime_context):
        del params, runtime_context
        sleep(OPERATOR_LATENCY_SECONDS)
        return OperatorResult(
            state.with_context(state.context),
            frozenset({"done"}),
            OperatorStatus.SUCCESS,
        )

    for operator_type in ("parse", "enrich", "asset_persist"):
        registry.register(operator_type, "1", handler)
    return registry


def measure_workflow(states):
    context = runtime(workflow_registry(), FakeEventRepository())
    executor = DocumentExecutor(context)
    started = perf_counter()
    result = executor.execute(
        plan([node("parse"), node("enrich"), node("asset_persist")]),
        states,
        max_workers=MAX_WORKERS,
    )
    return perf_counter() - started, result


@pytest.mark.performance
def test_workflow_throughput_and_live_document_bound() -> None:
    states = [document_state(f"doc-{index:03d}") for index in range(DOCUMENT_COUNT)]

    measure_legacy(states)
    measure_workflow(states)
    legacy_samples = [measure_legacy(states) for _ in range(3)]
    workflow_samples = [measure_workflow(states)[0] for _ in range(3)]
    _, final_result = measure_workflow(states)

    legacy_median = median(legacy_samples)
    workflow_median = median(workflow_samples)
    assert workflow_median <= legacy_median * 1.20, {
        "legacy_median": legacy_median,
        "workflow_median": workflow_median,
        "ratio": workflow_median / legacy_median,
    }
    assert final_result.max_active_documents <= MAX_WORKERS
    assert len(final_result.outcomes) == DOCUMENT_COUNT
