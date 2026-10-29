# -*- coding: utf-8 -*-
"""effective 配置解析单测（域覆盖消费侧，62号镜像）.

覆盖：from_effective 全 10 键构造 / TTL 命中与过期 / stale-if-error /
冷启动回退旧通道（env→file 语义）/ 都不可用返回 None / 域键隔离。
fetch 一律打桩（_fetch_effective seam），不发真实 HTTP。
"""
from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from knowledge_mining.mining.onenet import effective_config as ec
from knowledge_mining.mining.onenet.config import OnenetConfig

pytestmark = pytest.mark.asyncio

GLOBAL_RAW = {"app_id": "global-app", "static_token": "global-tok"}
DOMAIN_RAW = {"app_id": "domain-app", "static_token": "domain-tok",
              "chunk_width": 500, "verify_tls": True}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每用例独立缓存；默认禁止旧通道兜底（需测兜底时单独打桩）。"""
    ec.clear_cache()

    async def no_legacy():
        raise AssertionError("resolve_config 不应被调用")

    monkeypatch.setattr(ec, "resolve_config", no_legacy)
    yield
    ec.clear_cache()


def _fetch_stub(sink, *, responses=None, fail_after=None):
    """按域回放响应并记录调用序列的 fetch 桩（第 fail_after+1 次起抛）."""
    async def fetch(domain):
        sink.append(domain)
        if fail_after is not None and len(sink) > fail_after:
            raise RuntimeError("control plane down")
        if responses is not None:
            key = "__global__" if domain is None else domain
            return responses[key]
        return DOMAIN_RAW if domain else GLOBAL_RAW
    return fetch


# ---------------------------------------------------------------- from_effective


async def test_from_effective_picks_all_ten_keys():
    raw = {"app_id": "a", "static_token": "s", "token_url": "http://t",
           "search_url": "http://q", "source_type": 1, "timeout": 60,
           "verify_tls": True, "chunk_width": 500, "page_size": 200,
           "throttle_seconds": 0.5, "unknown_key": "ignored"}
    cfg = OnenetConfig.from_effective(raw)
    assert cfg.app_id == "a" and cfg.static_token == "s"
    assert cfg.verify_tls is True            # from_file 历史缺口在此通道补齐
    assert cfg.chunk_width == 500 and cfg.page_size == 200
    assert cfg.throttle_seconds == 0.5


async def test_from_effective_rejects_non_mapping_and_bad_range():
    with pytest.raises(ValueError):
        OnenetConfig.from_effective(["not", "a", "map"])
    with pytest.raises(ValueError):
        OnenetConfig.from_effective({**DOMAIN_RAW, "chunk_width": 99999})


# ---------------------------------------------------------------- TTL 缓存


async def test_ttl_hit_avoids_refetch(monkeypatch):
    sink: list = []
    monkeypatch.setattr(ec, "_fetch_effective", _fetch_stub(sink))
    first = await ec.resolve_effective_config(None)
    second = await ec.resolve_effective_config(None)
    assert sink == [None]                    # TTL 内只拉一次
    assert first is second
    assert first.app_id == "global-app"


async def test_ttl_expiry_refetches(monkeypatch):
    sink: list = []
    monkeypatch.setattr(ec, "_fetch_effective", _fetch_stub(sink))
    await ec.resolve_effective_config(None)
    # 把缓存条目时间戳拨回过期前
    key = ec._GLOBAL_KEY
    cfg, _ = ec._cache[key]
    ec._cache[key] = (cfg, 0.0)
    await ec.resolve_effective_config(None)
    assert sink == [None, None]              # 过期后重拉


async def test_domain_and_global_cached_separately(monkeypatch):
    sink: list = []
    monkeypatch.setattr(ec, "_fetch_effective", _fetch_stub(sink))
    g = await ec.resolve_effective_config(None)
    d = await ec.resolve_effective_config("cloud_core_network")
    assert sink == [None, "cloud_core_network"]
    assert g.app_id == "global-app" and d.app_id == "domain-app"
    # 命中各自的缓存，不再发请求
    await ec.resolve_effective_config("cloud_core_network")
    await ec.resolve_effective_config(None)
    assert len(sink) == 2


# ---------------------------------------------------------------- 失败回落链


async def test_fetch_failure_serves_stale(monkeypatch):
    sink: list = []
    monkeypatch.setattr(ec, "_fetch_effective", _fetch_stub(sink, fail_after=1))
    first = await ec.resolve_effective_config(None)
    # TTL 过期后控制面挂了 → 沿用上次成功值
    ec._cache[ec._GLOBAL_KEY] = (first, 0.0)
    stale = await ec.resolve_effective_config(None)
    assert stale is first
    assert stale.app_id == "global-app"


async def test_cold_start_falls_back_to_legacy_channel(monkeypatch):
    sink: list = []
    monkeypatch.setattr(ec, "_fetch_effective", _fetch_stub(sink, fail_after=0))
    legacy = OnenetConfig(app_id="legacy-app", static_token="legacy-tok")
    monkeypatch.setattr(ec, "resolve_config", lambda: legacy)
    cfg = await ec.resolve_effective_config("cloud_core_network")
    assert cfg is legacy                     # env→file 旧通道兜底
    assert ec._cache == {}                   # 兜底值不进缓存


async def test_cold_start_all_down_returns_none(monkeypatch):
    async def boom(domain):
        raise RuntimeError("down")
    monkeypatch.setattr(ec, "_fetch_effective", boom)
    monkeypatch.setattr(ec, "resolve_config", lambda: None)
    assert await ec.resolve_effective_config(None) is None


async def test_fetch_400_on_override_regression_not_fatal(monkeypatch):
    """控制面对盘改坏的覆盖文件回 400：同 stale-if-error，不致命."""
    sink: list = []

    async def fetch(domain):
        sink.append(domain)
        if len(sink) == 1:
            return GLOBAL_RAW
        raise RuntimeError("HTTP 400")

    monkeypatch.setattr(ec, "_fetch_effective", fetch)
    first = await ec.resolve_effective_config(None)
    ec._cache[ec._GLOBAL_KEY] = (first, 0.0)
    assert (await ec.resolve_effective_config(None)) is first
