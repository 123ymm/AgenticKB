"""MCP 来源分流与 mining HTTP 契约，原有 Wiki/表格仍走 serving。"""
import json
from dataclasses import replace

import httpx
import pytest
from fastmcp.exceptions import ToolError

from mcp_server import server, tools
from mcp_server.tests.test_artifact_tools import IDENT, ident_server


@pytest.fixture
def transport(monkeypatch):
    calls = []
    replies = []
    ident_server(monkeypatch, replace(IDENT, open_kbs=()))
    monkeypatch.setattr(tools, "_internal_auth_secret", lambda: "mining-secret")
    monkeypatch.setattr(tools, "_serving_secret", lambda: "serving-secret")

    def post(url, *, json, headers, **kwargs):
        calls.append(httpx.Request("POST", url, json=json, headers=headers))
        return replies.pop(0) if replies else httpx.Response(200, json={"view": "wiki_source"})
    monkeypatch.setattr(httpx, "post", post)
    return calls, replies


def test_source_uses_mining_and_authoritative_key_scope(transport):
    calls, _ = transport
    query = {"source": "ru-1", "max_chars": 2, "cursor": "opaque"}
    result = server.get_knowledge(ref="art_art_1", query=query)
    assert result["view"] == "wiki_source"
    assert calls[0].url.path == "/api/kb/mcp-tools/wiki-source"
    assert calls[0].headers["X-Internal-Auth"] == "mining-secret"
    assert json.loads(calls[0].content) == {
        "artifact_ref": "art_art_1", "query": query, "domain": IDENT.key_domain,
        "username": IDENT.username, "key_id": IDENT.key_id,
    }


@pytest.mark.parametrize("query", [None, {"q": "告警"}, {"page": "首页"},
                                   {"links": "首页"}, {"select": ["型号"]}])
def test_other_artifact_queries_still_work_with_no_open_kbs(transport, query):
    calls, _ = transport
    server.get_knowledge(ref="art_art_1", query=query)
    assert calls[0].url.path == "/api/internal/artifact-query"
    assert calls[0].headers["X-Internal-Auth"] == "serving-secret"


@pytest.mark.parametrize("args", [{"limit": 1}, {"cursor": "x"}, {"depth": 1},
                                  {"kb_names": ["kb"]}, {"relation": "children"}, {"mode": "full"}])
def test_source_rejects_unconsumed_parameters(transport, args):
    with pytest.raises(ToolError):
        server.get_knowledge(ref="art_art_1", query={"source": "ru-1"}, **args)
    assert transport[0] == []


@pytest.mark.parametrize("status,code", [(404, "unit_not_found"), (404, "artifact_not_found"),
                                       (400, "invalid_source_cursor"), (409, "expired_source_cursor")])
def test_business_errors_keep_their_codes(transport, status, code):
    transport[1].append(httpx.Response(status, json={"detail": {"code": code, "message": "test"}}))
    with pytest.raises(ToolError) as exc:
        server.get_knowledge(ref="art_art_1", query={"source": "ru-1"})
    assert exc.value.error_code == code


@pytest.mark.parametrize("response", [httpx.Response(500, text="internal trace"),
                                     httpx.Response(401, json={"detail": "invalid key"}),
                                     httpx.Response(200, text="not JSON")])
def test_infrastructure_errors_do_not_become_missing_source(transport, response):
    transport[1].append(response)
    with pytest.raises(ToolError) as exc:
        server.get_knowledge(ref="art_art_1", query={"source": "ru-1"})
    assert "unit_not_found" not in str(exc.value)
    assert "internal trace" not in str(exc.value)
