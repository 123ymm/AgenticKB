"""Worker embedding 任务结果验证测试（batch 审查问题二）。

provider 返回必须在任务标记 succeeded 前通过完整性校验——数量/Index/
维度一致/请求 dimensions 匹配；无效结果进入 fail→重试流程，绝不留下
succeeded 的坏任务（坏任务会被幂等键永久复用，RETRY 无法恢复）。
"""
from __future__ import annotations

import json

import pytest

from llm_service.providers.model_base import ModelProviderError
from llm_service.runtime.worker import Worker


def _provider_result(data: list[dict]) -> dict:
    return {"model": "fake-embed", "data": data, "usage": {"total_tokens": 8}}


def _vec(index: int, dim: int = 2) -> dict:
    return {"index": index, "embedding": [0.1 * (index + 1)] * dim}


class _FakeDB:
    def __init__(self, *, texts: list[str], dimensions: int | None):
        self.executes: list[tuple[str, tuple]] = []
        self._request = {
            "id": "req-1",
            "task_id": "task-1",
            "input_json": json.dumps(
                {"texts": texts, "model": "fake-embed", "dimensions": dimensions}
            ),
        }
        self._task = {"id": "task-1", "task_type": "embedding",
                      "attempt_count": 0, "max_attempts": 3}

    async def fetchone(self, sql, params):
        if "FROM agent_llm_requests" in sql:
            return self._request
        if "FROM agent_llm_tasks" in sql:
            return self._task
        raise AssertionError(f"unexpected fetchone: {sql}")

    async def execute(self, sql, params):
        self.executes.append((sql, params))


class _FakeMgr:
    def __init__(self):
        self.completed: list[str] = []
        self.failed: list[tuple[str, str, str]] = []

    async def complete(self, task_id):
        self.completed.append(task_id)

    async def fail(self, task_id, error_type, error_message):
        self.failed.append((task_id, error_type, error_message))


class _FakeProvider:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple] = []

    async def embed(self, texts, *, model=None, dimensions=None):
        self.calls.append((tuple(texts), model, dimensions))
        if self._error:
            raise self._error
        return self._result

    async def rerank(self, *a, **k):
        return {"results": []}


def _worker(provider, texts, dimensions=None, default_dimensions=None):
    db = _FakeDB(texts=texts, dimensions=dimensions)
    mgr = _FakeMgr()
    worker = Worker(
        db=db, task_manager=mgr, event_bus=None, provider=None,
        model_provider=provider,
        default_embedding_dimensions=default_dimensions,
    )
    return db, mgr, worker


pytestmark = pytest.mark.asyncio


async def test_valid_result_completes():
    texts = ["a", "b", "c"]
    db, mgr, worker = _worker(_FakeProvider(_provider_result([_vec(i) for i in range(3)])), texts)
    await worker._execute_embedding("task-1")
    assert mgr.completed == ["task-1"]
    assert mgr.failed == []


async def test_short_return_fails_instead_of_completing():
    texts = ["a", "b", "c"]
    db, mgr, worker = _worker(_FakeProvider(_provider_result([_vec(0)])), texts)
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert len(mgr.failed) == 1
    assert mgr.failed[0][1] == "invalid_response"


async def test_empty_vector_fails():
    db, mgr, worker = _worker(
        _FakeProvider(_provider_result([{"index": 0, "embedding": []}])), ["a"]
    )
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_duplicate_index_fails():
    data = [_vec(0), _vec(0)]
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a", "b"])
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_missing_index_fails():
    data = [_vec(0), _vec(2)]  # 覆盖 {0,2} ≠ {0,1}
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a", "b"])
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_inconsistent_dimensions_fails():
    data = [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3]}]
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a", "b"])
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_requested_dimensions_mismatch_fails():
    data = [_vec(0, dim=4)]  # 请求 dimensions=2，返回 4 维
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a"], dimensions=2)
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_requested_dimensions_match_completes():
    data = [_vec(0, dim=2)]
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a"], dimensions=2)
    await worker._execute_embedding("task-1")
    assert mgr.completed == ["task-1"]


async def test_malformed_items_fail():
    data = ["not-a-dict", _vec(1)]
    db, mgr, worker = _worker(_FakeProvider(_provider_result(data)), ["a", "b"])
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_provider_error_still_fails_with_provider_error_type():
    err = ModelProviderError("rate_limited", "429")
    db, mgr, worker = _worker(_FakeProvider(error=err), ["a"])
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "rate_limited"


# ── 存量 None 任务兜底：提交侧修复前入库的任务 input 里 dimensions=None ──

async def test_none_dimensions_falls_back_to_default():
    provider = _FakeProvider(_provider_result([_vec(0, dim=2)]))
    db, mgr, worker = _worker(provider, ["a"], dimensions=None, default_dimensions=2)
    await worker._execute_embedding("task-1")
    assert mgr.completed == ["task-1"]
    # provider 收到的是解析后的默认维度，而不是 None（None 会让请求不带
    # dimensions 参数 → 智谱按 2048 维返回 → 入库 expected 1024 报错）
    assert provider.calls[0][2] == 2


async def test_none_dimensions_default_mismatch_fails():
    # 兜底后校验必须生效：provider 返回维度 ≠ 解析出的默认维度 → 不许 complete
    provider = _FakeProvider(_provider_result([_vec(0, dim=4)]))
    db, mgr, worker = _worker(provider, ["a"], dimensions=None, default_dimensions=2)
    await worker._execute_embedding("task-1")
    assert mgr.completed == []
    assert mgr.failed[0][1] == "invalid_response"


async def test_none_dimensions_without_default_stays_none():
    # 未配置默认维度（老配置）：行为与修复前一致，None 直通
    provider = _FakeProvider(_provider_result([_vec(0, dim=4)]))
    db, mgr, worker = _worker(provider, ["a"], dimensions=None, default_dimensions=None)
    await worker._execute_embedding("task-1")
    assert mgr.completed == ["task-1"]
    assert provider.calls[0][2] is None
