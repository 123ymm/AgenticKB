"""61号批次3 后半：get_knowledge 接 art_ 制品视图 + 发现通道。

- tools 层：query_artifact / list_artifacts 经 serving internal REST 转发
  （X-Internal-Auth 同族；请求体契约 = Task 7 InternalArtifactController：
  is_admin 恒 false——MCP 钥匙均为用户钥匙）；
- server 层：_get_by_ref art_ 分支三视图透传（view 原样）、relation/mode
  显式拒绝、artifact_not_found 404 反探测转译为中文用户可见消息、
  _browse_top 附 artifacts 段（发现通道失败静默降级为不带该段）。

Uses httpx mock（照 test_client_structure.py）：断言 serving 真实可见的
URL / headers / payload。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from mcp_server import server
from mcp_server import tools as backend
from mcp_server.identity import Identity


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SERVING_INTERNAL_AUTH_SECRET", "serving-internal-secret")
    monkeypatch.setattr(backend, "SERVING_INTERNAL_URL", "http://serving-test:8081")


@pytest.fixture
def calls():
    return []


def install(monkeypatch, handler, calls):
    """Patch tools 层使用的 httpx.post → mock handler（记录每个请求）。"""

    def patched_post(url, *, json=None, headers=None, timeout=None, trust_env=False):
        request = httpx.Request("POST", url, json=json, headers=headers or {})
        return handler(request)

    monkeypatch.setattr(httpx, "post", patched_post)
    return patched_post


def ok(body: dict):
    return httpx.Response(200, json=body)


def handler_for(calls, responder):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)
    return handler


IDENT = Identity(
    username="alice",
    user_id="u-1",
    key_id="key-1",
    key_domain="cloud_core_network",
    open_kbs=({"id": "kb-1", "name": "网络手册库", "domain": "cloud_core_network"},),
)

DSL = {
    "select": ["端口数"],
    "where": [{"field": "端口数", "op": "gte", "value": 8}],
    "order_by": [{"field": "端口数", "direction": "desc"}],
    "limit": 20,
}


def ident_server(monkeypatch, ident=IDENT):
    monkeypatch.setattr(server, "_identity", lambda: ident)


def patch_listing(monkeypatch, kbs):
    monkeypatch.setattr(
        server.backend, "list_knowledge_bases",
        lambda username, key_id: {"knowledge_bases": kbs})


# ── tools 层：转发契约（路径 / 头 / 请求体） ──────────────────────────────


def test_query_artifact_forwards_dsl_contract(monkeypatch, calls):
    def responder(request):
        assert request.url.path == "/api/internal/artifact-query"
        return ok({"view": "artifact_rows", "artifact_ref": "art_ab12",
                   "columns": [], "rows": [], "cursor": None, "has_more": False})

    install(monkeypatch, handler_for(calls, responder), calls)
    out = backend.query_artifact("art_ab12", DSL, "cloud_core_network", IDENT)

    req = calls[0]
    assert req.headers["X-Internal-Auth"] == "serving-internal-secret"
    assert json.loads(req.content) == {
        "artifact_ref": "art_ab12",
        "domain": "cloud_core_network",
        "username": "alice",
        "is_admin": False,
        # 008：钥匙 id 下沉——serving 谓词折叠开放子集判定（子集不放大）
        "key_id": "key-1",
        "query": DSL,
    }
    assert out["view"] == "artifact_rows"


def test_query_artifact_card_sends_null_query(monkeypatch, calls):
    """query=None = 说明卡（Task 7 契约：query 键恒在，null 形态）。"""
    def responder(request):
        return ok({"view": "artifact_card", "artifact_ref": "art_ab12"})

    install(monkeypatch, handler_for(calls, responder), calls)
    backend.query_artifact("art_ab12", None, "cloud_core_network", IDENT)

    body = json.loads(calls[0].content)
    assert body["query"] is None
    assert body["artifact_ref"] == "art_ab12"


def test_list_artifacts_forwards_contract(monkeypatch, calls):
    def responder(request):
        assert request.url.path == "/api/internal/artifact-list"
        return ok({"view": "artifacts", "artifacts": [
            {"artifact_ref": "art_ab12", "title": "端口规格表", "kind": "table",
             "description": None, "usage_note": None, "revision": 3, "row_count": 120}],
            "cursor": None, "has_more": False})

    install(monkeypatch, handler_for(calls, responder), calls)
    out = backend.list_artifacts("cloud_core_network", IDENT, cursor="Y3Vyc29y", limit=20)

    req = calls[0]
    assert req.headers["X-Internal-Auth"] == "serving-internal-secret"
    assert json.loads(req.content) == {
        "domain": "cloud_core_network",
        "username": "alice",
        "is_admin": False,
        "key_id": "key-1",  # 008：发现通道同样限钥匙开放子集
        "cursor": "Y3Vyc29y",
        "limit": 20,
    }
    assert out["artifacts"][0]["artifact_ref"] == "art_ab12"


# ── server 层：_get_by_ref art_ 分支 ─────────────────────────────────────


def test_art_ref_card_view_passthrough(monkeypatch, calls):
    """只传 art_ ref → 说明卡；上游 view 自标识原样透传，不被覆写。"""
    def responder(request):
        return ok({"view": "artifact_card", "artifact_ref": "art_ab12",
                   "title": "端口规格表", "revision": 3, "row_count": 120,
                   "kind": "table", "columns": [
                       {"name": "端口数", "type": "int", "sortable": True,
                        "can_aggregate": True, "operations": ["eq", "gte"]}]})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    out = server.get_knowledge(ref="art_ab12")

    assert out["view"] == "artifact_card"
    assert out["columns"][0]["name"] == "端口数"
    body = json.loads(calls[0].content)
    assert body["domain"] == "cloud_core_network"  # 钥匙域自动填充
    assert body["username"] == "alice"
    assert body["query"] is None


def test_art_ref_rows_and_aggregate_views_passthrough(monkeypatch, calls):
    """query 透传：行视图 view=artifact_rows；aggregate DSL → artifact_aggregate。"""
    state = {"n": 0}

    def responder(request):
        state["n"] += 1
        if state["n"] == 1:
            return ok({"view": "artifact_rows", "artifact_ref": "art_ab12",
                       "rows": [{"row_key": "r1", "data": {"端口数": 24},
                                 "provenance": []}],
                       "cursor": None, "has_more": False})
        return ok({"view": "artifact_aggregate", "artifact_ref": "art_ab12",
                   "aggregate": {"op": "avg", "field": "端口数",
                                 "value": 18.5, "row_count": 120}})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)

    out_rows = server.get_knowledge(ref="art_ab12", query=DSL)
    assert out_rows["view"] == "artifact_rows"
    assert out_rows["rows"][0]["data"]["端口数"] == 24
    assert json.loads(calls[0].content)["query"] == DSL

    out_agg = server.get_knowledge(
        ref="art_ab12", query={"aggregate": {"op": "avg", "field": "端口数"}})
    assert out_agg["view"] == "artifact_aggregate"
    assert out_agg["aggregate"]["value"] == 18.5


def test_art_ref_rejects_relation_and_mode(monkeypatch, calls):
    """relation/mode 是结构/证据语义，与制品互斥——显式报错不静默。"""
    install(monkeypatch, handler_for(calls, lambda request: ok({})), calls)
    ident_server(monkeypatch)
    with pytest.raises(ToolError, match="relation"):
        server.get_knowledge(ref="art_ab12", relation="children")
    with pytest.raises(ToolError, match="mode"):
        server.get_knowledge(ref="art_ab12", mode="parent")
    assert calls == []  # 参数检查在前，未发任何上游请求


def test_art_ref_works_with_zero_open_kbs(monkeypatch, calls):
    """codex二审批号⑨回归：制品与库权限解绑（61号拍板）——零开放库钥匙
    也能查 art_。_scope_kbs 对空开放集直接报"未开放任何知识库"，art_ 分支
    必须分流在它之前；对照：同身份查 ev_/st_ 仍被挡。"""
    def responder(request):
        return ok({"view": "artifact_card", "artifact_ref": "art_ab12",
                   "title": "端口规格表"})

    install(monkeypatch, handler_for(calls, responder), calls)
    bare = Identity(
        username="bob",
        user_id="u-2",
        key_id="key-2",
        key_domain="cloud_core_network",
        open_kbs=(),
    )
    ident_server(monkeypatch, ident=bare)

    out = server.get_knowledge(ref="art_ab12")
    assert out["view"] == "artifact_card"
    assert len(calls) == 1   # 正常转发到 serving 制品端点，未触发 kb scope 错误

    # 对照：零开放库查结构引用仍按原口径拒绝（"未开放任何知识库"）
    with pytest.raises(ToolError, match="未开放任何知识库"):
        server.get_knowledge(ref="st_x1")


# ── server 层：错误转译 ──────────────────────────────────────────────────


def test_artifact_not_found_translated_to_user_message(monkeypatch, calls):
    """404 反探测（{"error": "artifact_not_found"} 字符串码形态）→ 中文可修正消息。"""
    def responder(request):
        return httpx.Response(404, json={
            "error": "artifact_not_found",
            "message": "Artifact not found or not readable"})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    with pytest.raises(ToolError, match="未找到或无权访问"):
        server.get_knowledge(ref="art_missing")


def test_artifact_typed_400_still_surfaced_with_code(monkeypatch, calls):
    """DSL 校验失败（typed 400 dict 形态）仍走既定 [code] 口径。"""
    def responder(request):
        return httpx.Response(400, json={"error": {
            "code": "unknown_field", "message": "未知字段: 端口",
            "details": {"allowed_fields": ["端口数"]}}})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    with pytest.raises(ToolError, match=r"\[unknown_field\].*可用字段"):
        server.get_knowledge(ref="art_ab12", query={"select": ["端口"]})


def test_artifact_401_infra_error_not_typed(monkeypatch, calls):
    """401 unauthenticated 同体形但是基础设施错误——不进 typed（invalid）口径，
    留在 upstream failed 兜底（Agent 密钥在服务端配置，不可修正）。"""
    def responder(request):
        return httpx.Response(401, json={"error": "unauthenticated"})

    install(monkeypatch, handler_for(calls, responder), calls)
    with pytest.raises(backend.ToolBackendError) as exc_info:
        backend.query_artifact("art_ab12", None, "cloud_core_network", IDENT)
    assert not isinstance(exc_info.value, backend.ServingToolError)


@pytest.mark.parametrize("status", [500, 502])
def test_artifact_5xx_string_error_not_typed(monkeypatch, calls, status):
    """M1 回归：≥500 + 字符串错误体（internal_error 等真基础设施故障）不进
    typed invalid 口径——修复前会被误归 ServingToolError("internal_error")
    （ledger invalid），现走 upstream failed 兜底。"""
    def responder(request):
        return httpx.Response(status, json={
            "error": "internal_error", "message": "Internal server error"})

    install(monkeypatch, handler_for(calls, responder), calls)
    with pytest.raises(backend.ToolBackendError) as exc_info:
        backend.query_artifact("art_ab12", None, "cloud_core_network", IDENT)
    assert not isinstance(exc_info.value, backend.ServingToolError)
    assert f"HTTP {status}" in str(exc_info.value)


def test_artifact_404_string_error_still_typed(monkeypatch, calls):
    """对照：4xx 字符串码（artifact_not_found 反探测 404）仍进 typed 口径——
    M1 只把 ≥500/401/503 摘出去，稳定业务码不受影响。"""
    def responder(request):
        return httpx.Response(404, json={
            "error": "artifact_not_found", "message": "Artifact not found"})

    install(monkeypatch, handler_for(calls, responder), calls)
    with pytest.raises(backend.ServingToolError) as exc_info:
        backend.query_artifact("art_ab12", None, "cloud_core_network", IDENT)
    assert exc_info.value.code == "artifact_not_found"


# ── server 层：_browse_top artifacts 发现段 ──────────────────────────────


def test_browse_top_appends_artifacts_section(monkeypatch, calls):
    def responder(request):
        assert request.url.path == "/api/internal/artifact-list"
        return ok({"view": "artifacts", "artifacts": [
            {"artifact_ref": "art_ab12", "title": "端口规格表", "kind": "table",
             "revision": 3, "row_count": 120}],
            "cursor": "next", "has_more": True})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    patch_listing(monkeypatch, [
        {"id": "kb-1", "name": "网络手册库", "domain": "cloud_core_network"}])

    out = server.get_knowledge()

    assert out["view"] == "kb_tree"
    assert [a["artifact_ref"] for a in out["artifacts"]] == ["art_ab12"]
    body = json.loads(calls[0].content)
    assert body == {
        "domain": "cloud_core_network", "username": "alice",
        "is_admin": False, "key_id": "key-1", "cursor": None, "limit": 20,
    }


def test_browse_top_artifacts_failure_degrades_silently(monkeypatch, calls):
    """发现通道挂了（500/拒连）不影响主浏览：仍 200、无 artifacts 段。"""
    def responder(request):
        return httpx.Response(500, json={"detail": "boom"})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    patch_listing(monkeypatch, [
        {"id": "kb-1", "name": "网络手册库", "domain": "cloud_core_network"}])

    out = server.get_knowledge()

    assert out["view"] == "kb_tree"
    assert out["domains"][0]["knowledge_bases"] == [{"name": "网络手册库"}]
    assert "artifacts" not in out


def test_browse_top_artifacts_unreachable_also_degrades(monkeypatch, calls):
    def responder(request):
        raise httpx.ConnectError("serving down")

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    patch_listing(monkeypatch, [])

    out = server.get_knowledge()
    assert out["view"] == "kb_tree"
    assert "artifacts" not in out


# ── 63号批次4 T3：wiki query 形态透传契约 ────────────────────────────────

# wiki 三种 dict 形态 + 表格最小 where 形态（对照）。第四种形态 query=None（说明
# 卡）已由 test_query_artifact_card_sends_null_query / test_art_ref_card_view_
# passthrough 覆盖；表格全量 DSL 转发契约见 test_query_artifact_forwards_dsl_contract。
WIKI_QUERY_CASES = [
    ({"q": "网络集成方案"}, "wiki_search"),
    # mode/top_k 写在 query dict 内部——不是顶层 mode 参数，不得触发 art_ 分支
    # 对顶层 mode 的拒绝
    ({"q": "核心交换机选型", "mode": "dense", "top_k": 5}, "wiki_search"),
    ({"page": "网络集成方案/总体设计"}, "wiki_page"),
    ({"links": "网络集成方案/总体设计", "direction": "in"}, "wiki_links"),
    # 对照：表格 DSL 最小 dict 形态不受 wiki 分流影响
    ({"where": [{"field": "端口数", "op": "gte", "value": 8}]}, "artifact_rows"),
]


@pytest.mark.parametrize("query,upstream_view", WIKI_QUERY_CASES)
def test_art_ref_query_shapes_pass_through_untouched(
        monkeypatch, calls, query, upstream_view):
    """wiki query dict（{"q"} / {"page"} / {"links"}）与表格 DSL 均经 get_knowledge
    art_ 分支**原样**到达 POST /api/internal/artifact-query 的 payload["query"]。

    契约要点：art_ 分支只拒顶层 relation/mode 参数，不做 query 结构预校验——
    kind 分流的权威在 Java ArtifactQueryService；未来改 query_artifact 时不得
    吞 wiki 形态的键（mode/top_k/direction）、不得增删改 query 内容。

    局限（勿踩坑）：本测试直接调 Python 函数，不过 FastMCP 的 pydantic 入参
    校验——字符串形态由 get_knowledge 签名（query: dict | None）在 MCP 入口
    排除，直接传字符串会绕过该签名校验，加字符串形态契约测试会假绿，勿加。
    """
    def responder(request):
        return ok({"view": upstream_view, "artifact_ref": "art_wk01",
                   "results": [], "degraded_dense": False, "revision": 3})

    install(monkeypatch, handler_for(calls, responder), calls)
    ident_server(monkeypatch)
    out = server.get_knowledge(ref="art_wk01", query=query)

    req = calls[0]
    assert req.url.path == "/api/internal/artifact-query"
    assert req.headers["X-Internal-Auth"] == "serving-internal-secret"
    # 整包断言：query 原样 + 既有转发契约键一个不多一个不少
    assert json.loads(req.content) == {
        "artifact_ref": "art_wk01",
        "domain": "cloud_core_network",  # 钥匙域自动填充
        "username": "alice",
        "is_admin": False,
        "key_id": "key-1",
        "query": query,
    }
    # 上游 view 自标识原样透传（wiki_* / artifact_*），不被覆写
    assert out["view"] == upstream_view
