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
        "is_admin": False, "cursor": None, "limit": 20,
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
