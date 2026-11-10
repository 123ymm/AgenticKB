# -*- coding: utf-8 -*-
"""一张网 effective 配置解析（按域，镜像 62号 LLM 域覆盖的消费侧）.

每请求向控制面拉取 merge 后的 effective 配置，进程内短 TTL 缓存兜底：

- ``domain=None`` → 全局基底（``GET /api/v1/system/onenet``）
- ``domain=X``    → 域 effective（``GET /api/v1/system/onenet/effective/X``；
  无覆盖域控制面直接回全局）

失败链（逐级回落，绝不致命）：
1. 控制面 HTTP 失败 / effective 校验 400 → 用同域上次成功值（stale-if-error，
   控制面短暂重启无感）；
2. 冷启动即不可达（无任何缓存）→ 旧通道 ``resolve_config()``（env → 本地盘
   onenet.yaml），保住 dev/测试与极端窗口——注意语义变化：环境变量仅在此
   回退通道压过盘上文件，控制面配置始终优先；
3. 都不可用 → ``None``（调用方按未配置处理，503 onenet_not_configured）。

覆盖/全局保存后无需任何通知机制——下一次缓存过期（~10s）自然拉到新值。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from knowledge_mining.mining.infra.control_plane import CONTROL_PLANE_BASE_URL
from knowledge_mining.mining.onenet.config import OnenetConfig, resolve_config

logger = logging.getLogger(__name__)

#: 缓存 TTL（秒）：保存后最多此时长生效；控制面抖动期间 stale 兜底。
TTL_SECONDS = 10.0

#: domain=None 的缓存键（与真实域名不冲突即可）。
_GLOBAL_KEY = "__global__"

#: (域键 → (OnenetConfig, 取得时刻 monotonic))。进程级缓存。
_cache: dict[str, tuple[OnenetConfig, float]] = {}


def clear_cache() -> None:
    """清空 effective 缓存（测试隔离用）。"""
    _cache.clear()


async def _fetch_effective(domain: str | None) -> dict[str, Any]:
    """从控制面拉全局（domain=None）或域 effective 配置（JSON dict）。

    任何失败（网络/非 200/类型不符）抛异常，由调用方走回落链。
    """
    if domain:
        url = f"{CONTROL_PLANE_BASE_URL}/api/v1/system/onenet/effective/{domain}"
    else:
        url = f"{CONTROL_PLANE_BASE_URL}/api/v1/system/onenet"
    from main_control_service.internal_auth import control_plane_internal_headers

    async with httpx.AsyncClient(timeout=5.0, proxy=None, trust_env=False) as client:
        resp = await client.get(url, headers=control_plane_internal_headers())
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"onenet effective 响应非映射: {type(data).__name__}")
    return data


async def resolve_effective_config(domain: str | None) -> OnenetConfig | None:
    """解析该域的 effective 一张网配置（TTL 缓存 + stale-if-error + 旧通道兜底）."""
    key = domain or _GLOBAL_KEY
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[1] < TTL_SECONDS:
        return hit[0]
    try:
        cfg = OnenetConfig.from_effective(await _fetch_effective(domain))
    except Exception as exc:  # noqa: BLE001 —— 回落链不致命
        if hit is not None:
            logger.warning(
                "onenet effective 拉取失败，沿用上次成功值（domain=%s）: %s",
                domain or "全局", exc,
            )
            return hit[0]
        return resolve_config()
    _cache[key] = (cfg, now)
    return cfg


__all__ = ["TTL_SECONDS", "clear_cache", "resolve_effective_config"]
