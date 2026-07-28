"""P3 — /api/kb/{kb_id}/documents routes (upload/zip/list/get/patch/download/permissions)."""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.routes.documents import router as docs_router
from knowledge_mining.mining.kb.routes.kbs import router as kb_router

pytestmark = pytest.mark.asyncio
DOMAIN = "cloud_core_network"


@pytest.fixture(scope="module")
def upload_root(tmp_path_factory):
    """Redirect UPLOAD_ROOT to a tmp dir so tests don't write to the real upload root."""
    p = tmp_path_factory.mktemp("kb_uploads")
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
    app = FastAPI()
    app.state.pg_pool = async_pool
    app.include_router(kb_router)
    app.include_router(docs_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_upload_list_get_patch_download(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "KB1"}, headers=h)).json()["id"]

        r = await c.post(
            f"/api/kb/{kb_id}/documents",
            files={"file": ("a.txt", b"hello world")},
            data={"directory": "sub"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        doc = r.json()
        doc_id = doc["id"]
        assert doc["status"] == "uploaded"
        assert doc["document_key"] == "doc:/sub/a.txt"
        assert doc["directory_path"] == "sub"

        r = await c.get(f"/api/kb/{kb_id}/documents", headers=h)
        assert r.status_code == 200 and len(r.json()) == 1

        r = await c.get(f"/api/kb/{kb_id}/documents/{doc_id}", headers=h)
        assert r.json()["status"] == "uploaded"

        r = await c.patch(
            f"/api/kb/{kb_id}/documents/{doc_id}",
            json={"document_type": "reference", "document_name": "renamed.txt"},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["document_type"] == "reference"

        r = await c.get(f"/api/kb/{kb_id}/documents/{doc_id}/download", headers=h)
        assert r.status_code == 200 and r.content == b"hello world"


async def test_upload_zip_extracts_with_directory(async_pool, upload_root):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dir1/x.txt", "x contents")
        zf.writestr("dir1/y.txt", "y contents")
    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "KBz"}, headers=h)).json()["id"]
        r = await c.post(
            f"/api/kb/{kb_id}/documents",
            files={"file": ("u.zip", buf.getvalue())},
            headers=h,
        )
        assert r.status_code == 201, r.text
        docs = r.json()["documents"]
        assert {d["document_name"] for d in docs} == {"x.txt", "y.txt"}
        assert {d["directory_path"] for d in docs} == {"dir1"}
        assert all(d["document_key"].startswith("doc:/dir1/") for d in docs)


async def test_other_user_cannot_access_private_kb_docs(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h_a, h_b = {"X-KB-User": "alice"}, {"X-KB-User": "bob"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "priv"}, headers=h_a)).json()["id"]
        doc_id = (await c.post(
            f"/api/kb/{kb_id}/documents", files={"file": ("a.txt", b"x")}, headers=h_a,
        )).json()["id"]

        # bob 对 private KB 的文档：list/get/download 全 404（不泄露）
        assert (await c.get(f"/api/kb/{kb_id}/documents", headers=h_b)).status_code == 404
        assert (await c.get(f"/api/kb/{kb_id}/documents/{doc_id}", headers=h_b)).status_code == 404
        assert (await c.get(f"/api/kb/{kb_id}/documents/{doc_id}/download", headers=h_b)).status_code == 404
        assert (await c.patch(f"/api/kb/{kb_id}/documents/{doc_id}", json={"document_name": "h"}, headers=h_b)).status_code == 404


async def test_withdraw_returns_501_pending_wiring(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "KBw"}, headers=h)).json()["id"]
        doc_id = (await c.post(
            f"/api/kb/{kb_id}/documents", files={"file": ("a.txt", b"x")}, headers=h,
        )).json()["id"]
        r = await c.delete(f"/api/kb/{kb_id}/documents/{doc_id}", headers=h)
        # withdraw stub：release 机制待接（设计 §10）→ 501
        assert r.status_code == 501


async def test_path_traversal_rejected(async_pool, upload_root):
    async with await _client(async_pool) as c:
        h = {"X-KB-User": "alice"}
        kb_id = (await c.post("/api/kb", json={"domain": DOMAIN, "name": "KBtr"}, headers=h)).json()["id"]
        r = await c.post(
            f"/api/kb/{kb_id}/documents",
            files={"file": ("a.txt", b"x")},
            data={"directory": "../escape"},
            headers=h,
        )
        assert r.status_code == 400
