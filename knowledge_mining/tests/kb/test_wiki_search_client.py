"""WikiSearchClient 转发客户端 —— httpx.MockTransport 桩 serving（不碰网络）。

覆盖（63号批次4 Task 4）：
- 转发契约：POST /api/internal/artifact-wiki-search 的 body 定稿
  （artifact_ref/q/mode/top_k/domain/username + is_admin=False + key_id=None）
  与 X-Internal-Auth 头；mode/top_k 缺省不进 body；
- 响应：200 JSON 原样透传；非 JSON 体 → serving_error；
- 状态翻译矩阵：404→artifact_not_found（反探测透传）/400→invalid_query/
  503→serving_unavailable/其余 ≥400→serving_error；
- 网络异常：TimeoutException→serving_timeout；连接异常→serving_unavailable；
- 本侧 secret 未配：本地即拦（不空跑网络）；
- env 解析：SERVING_INTERNAL_URL 覆盖缺省 127.0.0.1:8081。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from knowledge_mining.mining.kb.db_wiki import WikiError
from knowledge_mining.mining.kb.wiki_search_client import (
    SEARCH_PATH,
    WikiSearchClient,
)

pytestmark = pytest.mark.asyncio

SECRET = "internal-secret"

#: serving wiki_search 视图样例（snake_case 契约，批次4 Task 2 定稿）。
HITS_PAYLOAD: dict[str, Any] = {
    "view": "wiki_search",
    "artifact_ref": "art_w1",
    "revision": 3,
    "degraded_dense": False,
    "results": [
        {
            "page_key": "核心网-术语",
            "title": "核心网 术语",
            "unit_key": "u-1",
            "snippet": "MOP 是移动对外门户……",
            "score": 0.87,
            "channels": ["wiki_dense", "wiki_keyword"],
        },
    ],
}


def _capture(
    outcome: httpx.Response | Exception, sink: list[httpx.Request],
) -> httpx.MockTransport:
    """桩 transport：记录请求；按预设返回响应或抛网络异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> WikiSearchClient:
    return WikiSearchClient(
        base_url="http://serving-test", secret=SECRET, transport=transport)


# ------------------------------------------------------------ 转发契约

async def test_search_forwards_params_and_auth_header():
    """POST body 契约（is_admin=False/key_id=None）+ X-Internal-Auth 头 +
    200 响应 dict 原样透传。"""
    sink: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, json=HITS_PAYLOAD), sink)

    out = await _client(transport).search(
        "art_w1", "核心网", "fusion", 5, "cloud_core_network", "u-alice")

    assert out == HITS_PAYLOAD
    assert len(sink) == 1
    req = sink[0]
    assert req.method == "POST"
    assert req.url.path == SEARCH_PATH
    assert req.headers["X-Internal-Auth"] == SECRET
    assert json.loads(req.read()) == {
        "artifact_ref": "art_w1",
        "q": "核心网",
        "mode": "fusion",
        "top_k": 5,
        "domain": "cloud_core_network",
        "username": "u-alice",
        "is_admin": False,
        "key_id": None,
    }


async def test_search_omits_optional_mode_top_k_when_none():
    """mode/top_k 缺省不进 body——默认值（fusion/10/夹取）由 serving 定。"""
    sink: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, json=HITS_PAYLOAD), sink)

    await _client(transport).search(
        "art_w1", "核心网", None, None, "cloud_core_network", "u-alice")

    body = json.loads(sink[0].read())
    assert "mode" not in body
    assert "top_k" not in body


async def test_search_env_url_and_default(monkeypatch):
    """SERVING_INTERNAL_URL 覆盖缺省；未设 → http://127.0.0.1:8081（同容器
    supervisor 直连口径）。"""
    sink: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, json=HITS_PAYLOAD), sink)

    monkeypatch.setenv("SERVING_INTERNAL_URL", "http://serving-x:9000/")
    client = WikiSearchClient(secret=SECRET, transport=transport)
    await client.search("art_w1", "q", None, None, "d", "u")
    assert str(sink[-1].url).startswith("http://serving-x:9000/api/internal/")

    monkeypatch.delenv("SERVING_INTERNAL_URL", raising=False)
    client = WikiSearchClient(secret=SECRET, transport=transport)
    await client.search("art_w1", "q", None, None, "d", "u")
    assert str(sink[-1].url).startswith("http://127.0.0.1:8081/api/internal/")


# ------------------------------------------------------------ 状态翻译矩阵

@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "artifact_not_found"),   # serving 反探测透传（rev0 不可见等）
        (400, "invalid_query"),        # q 空白/mode 非法
        (503, "serving_unavailable"),  # serving 侧 secret 未配
        (500, "serving_error"),
        (502, "serving_error"),
        (401, "serving_error"),        # 内部密钥失配（基础设施错）
    ],
)
async def test_search_status_translation(status: int, code: str):
    """serving HTTP 状态 → WikiError.code（路由层再翻 HTTP+中文）。"""
    transport = _capture(
        httpx.Response(status, json={"error": {"code": "x", "message": "y"}}), [])

    with pytest.raises(WikiError) as ei:
        await _client(transport).search("art_w1", "q", None, None, "d", "u")

    assert ei.value.code == code


async def test_search_non_json_success_body():
    """200 但非 JSON（网关误配等）→ serving_error，不抛裸 ValueError。"""
    transport = _capture(httpx.Response(200, text="<html>gateway</html>"), [])

    with pytest.raises(WikiError) as ei:
        await _client(transport).search("art_w1", "q", None, None, "d", "u")

    assert ei.value.code == "serving_error"


# ------------------------------------------------------------ 网络异常

async def test_search_timeout_translates_serving_timeout():
    """httpx.TimeoutException（含 ConnectTimeout/ReadTimeout 子类）→
    serving_timeout（路由层 502）。"""
    transport = _capture(httpx.ReadTimeout("slow"), [])

    with pytest.raises(WikiError) as ei:
        await _client(transport).search("art_w1", "q", None, None, "d", "u")

    assert ei.value.code == "serving_timeout"


async def test_search_connect_error_translates_serving_unavailable():
    """连接拒绝等非超时网络异常 → serving_unavailable。"""
    transport = _capture(httpx.ConnectError("refused"), [])

    with pytest.raises(WikiError) as ei:
        await _client(transport).search("art_w1", "q", None, None, "d", "u")

    assert ei.value.code == "serving_unavailable"


# ------------------------------------------------------------ 密钥未配

async def test_search_secret_missing_short_circuits(monkeypatch):
    """本侧 SERVING_INTERNAL_AUTH_SECRET 未配 → 本地即拦 serving_unavailable
    （与 serving 侧 503 同口径），不发起网络请求。"""
    monkeypatch.delenv("SERVING_INTERNAL_AUTH_SECRET", raising=False)
    sink: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, json=HITS_PAYLOAD), sink)
    client = WikiSearchClient(base_url="http://serving-test", transport=transport)

    with pytest.raises(WikiError) as ei:
        await client.search("art_w1", "q", None, None, "d", "u")

    assert ei.value.code == "serving_unavailable"
    assert sink == []
