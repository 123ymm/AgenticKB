"""真实 PostgreSQL：MCP/网页共用读取、来源归属、共享快照及权限分页。"""
from __future__ import annotations

import hashlib
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.types.json import Jsonb

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip("set KB_RUN_POSTGRES_ACCEPTANCE=1 for PostgreSQL tests", allow_module_level=True)

from knowledge_mining.mining.api.auth_guard import MiningApiAuthMiddleware
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
from knowledge_mining.mining.kb.db_wiki import WikiDB
from knowledge_mining.mining.kb.routes.mcp_tools import router as mcp_router
from knowledge_mining.mining.kb.routes.wiki import router as wiki_router
from knowledge_mining.tests.conftest import kb_headers
from knowledge_mining.tests.kb.test_wiki_db import _seed_kb, _add_doc, _seed_build, _page, _upsert

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
TEXT = "甲😀乙丙丁\n" * 503


@pytest_asyncio.fixture
async def source_env(async_pool):
    kbdb, kb_id = await _seed_kb(async_pool)
    owner = (await kbdb.get_kb(kb_id))["owner_id"]
    async with async_pool.connection() as conn:
        user = await (await conn.execute("SELECT * FROM kb_users WHERE id=%s", (owner,))).fetchone()
    doc = await _add_doc(kbdb, kb_id)
    snap = await _seed_build(async_pool, kb_id=kb_id, document_id=doc, units=2)
    rid = f"ru-{snap[:16]}-0"
    async with async_pool.connection() as conn:
        await conn.execute("UPDATE asset_retrieval_units_v2 SET content_text=%s WHERE representation_id=%s", (TEXT, rid))
    adb, wdb = ArtifactDB(async_pool), WikiDB(async_pool)
    art = await adb.create_artifact(domain_id="plant-a", kind="wiki", title="来源测试", owner_user_id=owner)
    aid = art["artifact_id"]
    await adb.update_artifact_meta(aid, usage_note="核对来源")
    await wdb.ensure_draft(aid)
    provenance = [{"kb_id": kb_id, "snapshot_id": snap, "unit_ids": [rid]}]
    await _upsert(wdb, aid, _page("首页", "首页", f"[[src:{rid}]]", provenance=provenance))
    await wdb.publish(aid, expected_updated_at=(await adb.get_artifact(aid))["updated_at"],
                      change_summary="v1", published_by=owner)
    key_id = uuid.uuid4().hex
    key_hash = hashlib.sha256(key_id.encode()).hexdigest()
    await kbdb.create_mcp_key(user_id=owner, name="来源测试", domain="plant-a", key_id=key_id,
                              key_hash=key_hash, key_prefix=key_hash[:8])
    await kbdb.replace_mcp_key_open_kbs(key_id=key_id, kb_ids=[kb_id])
    await kbdb.replace_mcp_key_open_artifacts(key_id=key_id, artifact_ids=[aid])
    app = FastAPI()
    app.state.pg_pool = async_pool
    app.include_router(mcp_router)
    app.include_router(wiki_router)
    app.add_middleware(MiningApiAuthMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def read(query=None):
            return await client.post("/api/kb/mcp-tools/wiki-source",
                headers={"X-Internal-Auth": kb_headers()["X-Internal-Auth"]},
                json={"username": user["username"], "key_id": key_id, "domain": "plant-a",
                      "artifact_ref": "art_" + aid,
                      "query": {"source": rid} if query is None else query})
        yield SimpleNamespace(pool=async_pool, kbdb=kbdb, kb_id=kb_id, owner=owner, user=user,
                              doc=doc, snap=snap, rid=rid, aid=aid, key_id=key_id, adb=adb,
                              wdb=wdb, read=read, client=client, provenance=provenance)


async def test_full_original_slice_and_web_preview(source_env):
    e = source_env
    # 文档已有新快照，Wiki 仍必须读被引用的旧切片。
    await _seed_build(e.pool, kb_id=e.kb_id, document_id=e.doc, units=1,
                      created_at="2026-11-19T00:00:00+00:00")
    parts, cursor = [], None
    while True:
        query = {"source": e.rid, "max_chars": 101}
        if cursor: query["cursor"] = cursor
        r = await e.read(query)
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["source"]["snapshot_id"] == e.snap
        assert len(page["content_text"]) <= 101
        parts.append(page["content_text"])
        cursor = page["cursor"]
        if not page["has_more"]:
            assert cursor is None
            break
    assert "".join(parts) == TEXT
    r = await e.client.get(f"/api/artifacts/{e.aid}/wiki/unit-source",
                           params={"unit_id": e.rid}, headers=kb_headers(e.user["username"]))
    assert r.status_code == 200 and r.json()["content_text"] == TEXT[:500]


@pytest.mark.parametrize("change", ["forged_kb", "forged_snapshot", "not_cited", "deleted_doc", "deleted_unit"])
async def test_actual_source_ownership_gate(source_env, change):
    e = source_env
    async with e.pool.connection() as conn:
        if change == "deleted_doc":
            await conn.execute("UPDATE asset_documents SET deleted_at=now() WHERE id=%s", (e.doc,))
        elif change == "deleted_unit":
            await conn.execute("DELETE FROM asset_retrieval_units_v2 WHERE representation_id=%s", (e.rid,))
        else:
            pv = [dict(e.provenance[0])]
            if change == "forged_kb":
                # 即使钥匙开放此库，也不能伪造切片归属。
                other = await e.kbdb.create_kb(domain="plant-a", name=uuid.uuid4().hex, owner_id=e.owner)
                await e.kbdb.replace_mcp_key_open_kbs(key_id=e.key_id, kb_ids=[other["id"]])
                pv[0]["kb_id"] = other["id"]
            elif change == "forged_snapshot": pv[0]["snapshot_id"] = "wrong-snapshot"
            else: pv[0]["unit_ids"] = [f"ru-{e.snap[:16]}-1"]
            await conn.execute("UPDATE kb_wiki_pages SET provenance=%s WHERE artifact_id=%s AND revision=1",
                               (Jsonb(pv), e.aid))
    r = await e.read()
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unit_not_found"


@pytest.mark.parametrize("change", ["source_key_scope", "source_user_permission", "artifact_key_scope", "key_revoked"])
async def test_revoke_after_first_page(source_env, change):
    e = source_env
    first = (await e.read()).json()
    if change == "source_key_scope":
        # admin 也不能突破钥匙开放范围。
        async with e.pool.connection() as conn:
            await conn.execute("UPDATE kb_users SET site_role='admin' WHERE id=%s", (e.owner,))
        await e.kbdb.replace_mcp_key_open_kbs(key_id=e.key_id, kb_ids=[])
    elif change == "source_user_permission":
        other = await e.kbdb.create_user(username="other-" + uuid.uuid4().hex)
        async with e.pool.connection() as conn:
            await conn.execute("UPDATE knowledge_bases SET owner_id=%s,visibility='private' WHERE id=%s", (other["id"], e.kb_id))
            await conn.execute("DELETE FROM kb_members WHERE kb_id=%s AND user_id=%s", (e.kb_id, e.owner))
    elif change == "artifact_key_scope":
        await e.kbdb.replace_mcp_key_open_artifacts(key_id=e.key_id, artifact_ids=[])
    else: await e.kbdb.revoke_mcp_key(key_id=e.key_id)
    r = await e.read({"source": e.rid, "cursor": first["cursor"]})
    assert r.status_code == (401 if change == "key_revoked" else 404), r.text


async def test_shared_snapshot_uses_authorized_source(source_env):
    e = source_env
    other = await e.kbdb.create_kb(domain="plant-a", name=uuid.uuid4().hex, owner_id=e.owner)
    doc = await _add_doc(e.kbdb, other["id"])
    await _seed_build(e.pool, kb_id=other["id"], document_id=doc, units=0)
    async with e.pool.connection() as conn:
        await conn.execute("UPDATE asset_build_document_snapshots SET document_snapshot_id=%s WHERE document_id=%s", (e.snap, doc))
        pv = e.provenance + [{"kb_id": other["id"], "snapshot_id": e.snap, "unit_ids": [e.rid]}]
        await conn.execute("UPDATE kb_wiki_pages SET provenance=%s WHERE artifact_id=%s", (Jsonb(pv), e.aid))
    await e.kbdb.replace_mcp_key_open_kbs(key_id=e.key_id, kb_ids=[other["id"]])
    r = await e.read()
    assert r.status_code == 200, r.text
    assert r.json()["source"]["kb_id"] == other["id"]
    # 游标不能在续页时悄悄切换来源库。
    token = r.json()["cursor"]
    await e.kbdb.replace_mcp_key_open_kbs(key_id=e.key_id, kb_ids=[e.kb_id])
    r = await e.read({"source": e.rid, "cursor": token})
    assert r.status_code == 404
