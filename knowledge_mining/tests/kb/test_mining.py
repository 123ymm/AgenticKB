"""P4.2 — /api/kb/{kb_id}/mine trigger (validation + 202 mechanics with stubbed pipeline).

完整 pipeline 端到端（真实 parse/segment/embedding）依赖 llm_service，超出单测范围；
这里 monkeypatch 掉 mining.jobs.run.run，只验证触发机制（权限/校验/run 行创建）。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.routes.documents import router as docs_router
from knowledge_mining.mining.kb.routes.kbs import router as kb_router
from knowledge_mining.mining.kb.routes.mining import router as kb_mining_router

pytestmark = pytest.mark.asyncio
DOMAIN = "cloud_core_network"


@pytest.fixture(scope="module")
def upload_root(tmp_path_factory):
    p = tmp_path_factory.mktemp("kb_uploads_mine")
    old = os.environ.get("UPLOAD_ROOT")
    os.environ["UPLOAD_ROOT"] = str(p)
    try:
        yield p
    finally:
        if old is None:
            os.environ.pop("UPLOAD_ROOT", None)
        else:
            os.environ["UPLOAD_ROOT"] = old


async def _client(async_pool):
    from knowledge_mining.mining.infra.pg_config import MiningDbConfig
    app = FastAPI()
    app.state.pg_pool = async_pool
    app.state.db_config = MiningDbConfig()
    app.include_router(kb_router)
    app.include_router(docs_router)
    app.include_router(kb_mining_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_kb_with_upload(c, headers, name="KBm"):
    kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": name}, headers=headers)).json()["id"]
    await c.post(f"/api/kb/{kb_id}/documents", files={"file": ("a.txt", b"hello")}, headers=headers)
    return kb_id


async def test_mine_not_found(async_pool, upload_root):
    async with await _client(async_pool) as c:
        r = await c.post("/api/kb/nope-kb/mine", headers={"X-KB-User": "alice"})
        assert r.status_code == 404


async def test_mine_other_user_private_404(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h_a, h_b = {"X-KB-User": "alice"}, {"X-KB-User": "bob"}
        kb_id = await _make_kb_with_upload(c, h_a, name="priv-mine")
        r = await c.post(f"/api/kb/{kb_id}/mine", headers=h_b)
        assert r.status_code == 404  # bob 看不到 alice 的 private


async def test_mine_viewer_forbidden(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h_a, h_b = {"X-KB-User": "alice"}, {"X-KB-User": "bob"}
        kb_id = await _make_kb_with_upload(c, h_a, name="shared-mine")
        await c.post("/api/kb", json={"domain": DOMAIN, "name": "tmp"}, headers=h_b)  # upsert bob
        await c.post(f"/api/kb/{kb_id}/members", json={"username": "bob", "role": "viewer"}, headers=h_a)
        r = await c.post(f"/api/kb/{kb_id}/mine", headers=h_b)
        assert r.status_code == 403  # viewer 不能触发挖掘


async def test_mine_no_uploads_400(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "empty-kb"}, headers=h)).json()["id"]
        r = await c.post(f"/api/kb/{kb_id}/mine", headers=h)
        assert r.status_code == 400


async def test_mine_owner_202_creates_run_row(async_pool, upload_root, monkeypatch):
    # stub 掉真实 pipeline，避免依赖 llm_service
    def _stub_run(*args, **kwargs):
        return None
    monkeypatch.setattr("knowledge_mining.mining.jobs.run.run", _stub_run)

    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = await _make_kb_with_upload(c, h, name="ok-mine")
        r = await c.post(f"/api/kb/{kb_id}/mine", headers=h)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert r.json()["kb_id"] == kb_id

        async with async_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT domain, metadata_json->>'kb_id' AS kb_id FROM mining_runs WHERE id = %s",
                [run_id],
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["domain"] == DOMAIN
        assert row["kb_id"] == kb_id

        # 等 stub 线程释放域锁，避免污染后续测试
        await asyncio.sleep(0.3)
