"""submit_embedding 的 dimensions 提交侧解析回归测试。

背景：mining 异步提交不传 dimensions，老实现把 None 原样落进任务 input，
worker 转发 provider 时请求不带 dimensions 参数——智谱 embedding-3 按默认
2048 维返回，与库列 vector(1024) 不符，向量化入库报
"expected 1024 dimensions, not 2048"。修复后提交侧解析配置默认值
（与 ModelService 同步端点对齐），显式传入不被覆盖。
"""
from __future__ import annotations

import json

import pytest

from llm_service.runtime.service import LLMService
from llm_service.tests.conftest import TEST_CFG

pytestmark = pytest.mark.asyncio


class _FakeConn:
    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []

    async def execute(self, sql, params):
        self.executes.append((sql, params))

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def connection(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakeDB:
    def __init__(self):
        self.pool = _FakePool()

    async def fetchone(self, sql, params):
        # find_existing_task：无同键任务 → 走新任务插入
        return None

    async def execute(self, sql, params):
        pass


def _svc() -> tuple[LLMService, _FakeDB]:
    db = _FakeDB()
    cfg = {**TEST_CFG, "embedding": {"model": "m1", "dimensions": 1024}}
    svc = LLMService(db=db, provider=None, config=cfg, templates=object())
    return svc, db


def _request_input_json(db: _FakeDB) -> dict:
    for sql, params in db.pool.conn.executes:
        if "INSERT INTO agent_llm_requests" in sql:
            # request_params 展开位置：input_json 是 params 第 7 个元素
            return json.loads(params[6])
    raise AssertionError("agent_llm_requests 未插入")


async def test_omitted_dimensions_resolved_from_config():
    svc, db = _svc()
    await svc.submit_embedding(["a"], idempotency_key="k1")
    assert _request_input_json(db)["dimensions"] == 1024


async def test_explicit_dimensions_not_overridden():
    svc, db = _svc()
    await svc.submit_embedding(["a"], dimensions=512, idempotency_key="k2")
    assert _request_input_json(db)["dimensions"] == 512
