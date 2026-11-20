"""来源读取契约：真实 Service + ASGI 路由/认证中间件，数据库替身。"""
from __future__ import annotations

import base64
import json

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.api.auth_guard import MiningApiAuthMiddleware
from knowledge_mining.mining.kb.deps import get_kb_db, get_wiki_service
from knowledge_mining.mining.kb.routes.mcp_tools import router
from knowledge_mining.tests.conftest import kb_headers
from knowledge_mining.tests.kb.test_wiki_routes import (
    DOMAIN, WID, env, _publish_one_page, _seed_unit,
)
from knowledge_mining.tests.kb.test_artifacts_service import _user

pytestmark = pytest.mark.asyncio
PATH = "/api/kb/mcp-tools/wiki-source"
TEXT = "甲😀乙丙\n" * 701


class KeyDB:
    def __init__(self):
        self.user = {**_user("u-viewer"), "username": "viewer", "status": "active"}
        self.key = {"id": "key-1", "user_id": "u-viewer", "domain": DOMAIN,
                    "status": "active"}
        self.artifacts = [WID]
        self.kbs = ["kb-1"]

    async def get_user_by_username(self, username):
        return self.user if username == "viewer" else None

    async def get_mcp_key(self, *, key_id):
        return self.key if key_id == "key-1" else None

    async def key_open_artifact_ids(self, *, key_id):
        return self.artifacts

    async def key_open_kb_ids(self, *, key_id):
        return self.kbs


@pytest_asyncio.fixture
async def api(env):
    await _publish_one_page(env)
    _seed_unit(env, "ru-1", content_text=TEXT)
    db = KeyDB()
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(MiningApiAuthMiddleware)
    app.dependency_overrides[get_kb_db] = lambda: db
    app.dependency_overrides[get_wiki_service] = lambda: env.svc
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, db


async def read(api, query=None, **extra):
    client, _ = api
    return await client.post(PATH, headers={"X-Internal-Auth": kb_headers()["X-Internal-Auth"]},
                             json={"username": "viewer", "key_id": "key-1",
                                   "domain": DOMAIN, "artifact_ref": "art_" + WID,
                                   "query": {"source": "ru-1"} if query is None else query,
                                   **extra})


async def test_complete_slice_paging_and_web_preview(api, env):
    r = await read(api)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["view"] == "wiki_source" and first["revision"] == 1
    assert len(first["content_text"]) == 2000
    assert first["source"] == {"kb_id": "kb-1", "snapshot_id": "snap-1", "section_ref": "s-0"}
    full, page = first["content_text"], first
    while page["has_more"]:
        r = await read(api, {"source": "ru-1", "max_chars": 127, "cursor": page["cursor"]})
        assert r.status_code == 200, r.text
        page = r.json()
        full += page["content_text"]
    assert full == TEXT and page["cursor"] is None
    web = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source", params={"unit_id": "ru-1"})
    assert web.json() == {"unit_id": "ru-1", "content_text": TEXT[:500],
                          "kb_id": "kb-1", "snapshot_id": "snap-1", "section_ref": "s-0"}


@pytest.mark.parametrize("change,status,code", [
    ("key_kb", 404, "unit_not_found"), ("user_kb", 404, "unit_not_found"),
    ("key_artifact", 404, "artifact_not_found"),
    ("user_artifact", 404, "artifact_not_found"),
    ("domain_binding", 404, "artifact_not_found"),
    ("unpublished", 404, "artifact_not_found"),
    ("key_revoked", 401, None), ("user_disabled", 401, None),
    ("user_deleted", 401, None),
])
async def test_authorization_is_rechecked_on_every_page(api, env, change, status, code):
    first = (await read(api)).json()
    _, db = api
    if change == "key_kb": db.kbs = []
    elif change == "user_kb": env.inputs.readable.clear()
    elif change == "key_artifact": db.artifacts = []
    elif change == "user_artifact": del env.adb.members[WID]["u-viewer"]
    elif change == "domain_binding": env.adb.domain_bindings.remove(("u-viewer", DOMAIN))
    elif change == "unpublished": env.adb.artifacts[WID]["status"] = "disabled"
    elif change == "key_revoked": db.key["status"] = "revoked"
    elif change == "user_disabled": db.user["status"] = "disabled"
    elif change == "user_deleted": db.user["deleted_at"] = "2026-11-19"
    r = await read(api, {"source": "ru-1", "cursor": first["cursor"]})
    assert r.status_code == status, r.text
    if code:
        assert r.json()["detail"]["code"] == code


@pytest.mark.parametrize("query", [
    {}, {"source": ""}, {"source": 1}, {"source": "x" * 201},
    {"source": "ru-1", "page": "首页"}, {"source": "ru-1", "q": "告警"},
    {"source": "ru-1", "links": "首页"}, {"source": "ru-1", "max_chars": True},
    {"source": "ru-1", "max_chars": 0}, {"source": "ru-1", "max_chars": 10001},
    {"source": "ru-1", "max_chars": "2000"}, {"source": "ru-1", "cursor": ""},
    {"source": "ru-1", "cursor": None}, {"source": "ru-1", "cursor": "bad cursor"},
    {"source": "ru-1", "cursor": "x" * 4097},
    {"source": "ru-1", "cursor": base64.urlsafe_b64encode(b"[" * 1100 + b"]" * 1100).decode()},
])
async def test_invalid_query_and_cursor(api, query):
    assert (await read(api, query)).status_code == 400


@pytest.mark.parametrize("change", ["revision", "content", "snapshot"])
async def test_cursor_rejects_changed_source(api, env, change):
    first = (await read(api)).json()
    if change == "revision": env.adb.artifacts[WID]["current_revision"] = 2
    elif change == "content": env.inputs.unit_rows["ru-1"]["content_text"] = "新内容"
    else: env.inputs.unit_rows["ru-1"]["snapshot_id"] = "new-snapshot"
    r = await read(api, {"source": "ru-1", "cursor": first["cursor"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "expired_source_cursor"


@pytest.mark.parametrize("index,value", [(1, "other-artifact"), (3, "other-unit"),
                                        (7, -1), (7, True), (7, 2**40), (7, len(TEXT))])
async def test_cursor_cannot_cross_resources_or_use_bad_offsets(api, index, value):
    token = (await read(api)).json()["cursor"]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    payload[index] = value
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    r = await read(api, {"source": "ru-1", "cursor": token})
    assert r.status_code == 400


async def test_missing_source_and_denied_source_have_same_response(api):
    missing = await read(api, {"source": "not-cited"})
    api[1].kbs = []
    denied = await read(api)
    assert missing.status_code == denied.status_code == 404
    assert missing.json() == denied.json()


async def test_internal_secret_key_identity_and_domain(api):
    client, db = api
    assert (await client.post(PATH, json={})).status_code == 401
    assert (await read(api, key_id="wrong-key")).status_code == 401
    assert (await read(api, username="stranger")).status_code == 401
    assert (await read(api, domain="other-domain")).status_code == 404
    db.key["user_id"] = "someone-else"
    assert (await read(api)).status_code == 401


async def test_empty_source_is_a_complete_page(api, env):
    env.inputs.unit_rows["ru-1"]["content_text"] = ""
    r = (await read(api)).json()
    assert r["content_text"] == "" and r["cursor"] is None and r["has_more"] is False
