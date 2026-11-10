"""ExportService / 路由层测试 — 权限门禁 / 闸门 / 幂等 / 下载现签 / 删除语义.

错误族（NotFound→404、Empty·TooLarge→422、Conflict→409、Disabled→503）
在 route 层 _map_error 译码；这里对 service 断异常类型，路由测 HTTP 码。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from knowledge_mining.mining.contracts.storage.types import (
    ObjectLocation, PutOptions,
)
from knowledge_mining.mining.infra import control_plane as cp
from knowledge_mining.mining.infra.export_config import ExportConfig
from knowledge_mining.mining.infra.object_store.config import ObjectStoreConfig
from knowledge_mining.mining.infra.object_store.fake import FakeObjectStore
from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_export import ExportDB
from knowledge_mining.mining.kb.services import export_tasks as et
from knowledge_mining.mining.kb.services.export_service import (
    ExportDisabled, ExportService, ExportTaskConflict, ExportTooLarge,
    SourceEmptyExport,
)
from knowledge_mining.mining.kb.services.kb_service import NotFound

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"


def _hex() -> str:
    return uuid.uuid4().hex


async def _drain_bg() -> None:
    tasks = [t for t in et._bg_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def svc_env(async_pool, tmp_path):
    orig = cp.get_mining_service_config()
    obj_root = tmp_path / "objstore"
    store = FakeObjectStore(str(obj_root))
    store_cfg = ObjectStoreConfig(
        provider="fake", bucket_prefix="kb-x-", root_path=str(obj_root),
    )
    config = ExportConfig(
        export_tmp_root=str(tmp_path / "exports"),
        export_presign_ttl_seconds=3600,
    )
    svc = ExportService(
        KbDB(async_pool), ExportDB(async_pool),
        object_store=store, object_store_config=store_cfg,
        config=config, pool=async_pool,
    )
    try:
        yield {
            "pool": async_pool, "db": KbDB(async_pool), "svc": svc,
            "store": store, "store_cfg": store_cfg, "config": config,
        }
    finally:
        cp.set_mining_service_config(orig)


async def _seed_kb(env, *, docs: int = 1, visibility: str = "private"):
    db = env["db"]
    owner = await db.upsert_user_by_username(f"u_{_hex()[:10]}")
    kb = await db.create_kb(
        domain=DOMAIN, name=f"产品库-{_hex()[:6]}", owner_id=owner["id"],
        visibility=visibility,
    )
    for i in range(docs):
        oid = f"so-{_hex()[:10]}"
        payload = f"doc-{i}-bytes".encode("utf-8") * 10
        res = await env["store"].put_bytes(
            ObjectLocation(bucket="kb-x-source", object_key=f"objects/{oid}"),
            payload, PutOptions(artifact_class="source"),
        )
        from knowledge_mining.mining.contracts.file_management import (
            StorageObjectRecord,
        )
        from knowledge_mining.mining.file_management.repositories_pg import (
            PgStorageObjectRepository,
        )
        await PgStorageObjectRepository(env["pool"]).register(
            StorageObjectRecord(
                id=oid, provider="fake", bucket="kb-x-source",
                object_key=f"objects/{oid}", object_version_id=None,
                sha256=res.sha256, size=res.size, mime="text/markdown",
                artifact_class="source", state="AVAILABLE", etag=res.etag,
            ))
        await db.insert_document_from_storage(
            domain=DOMAIN, kb_id=kb["id"], document_key=f"doc:/f{i}.md",
            document_name=f"f{i}.md", storage_object_id=oid,
            source_raw_hash=res.sha256, directory_path="",
            document_type="reference", owner_id=owner["id"],
            file_size=len(payload),
        )
    return owner, kb


async def _succeeded_row(env, kb: dict, requested_by: str,
                         *, expires_hours: float = 24.0):
    """直接造一条 succeeded 任务行 + 产物对象（绕过 worker，测读路径）."""
    repo = et.ExportTaskRepo(env["pool"])
    row = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
        requested_by=requested_by)
    bucket = env["config"].bucket_for(env["store_cfg"])
    key = et.artifact_object_key(kb["id"], row["id"])
    res = await env["store"].put_bytes(
        ObjectLocation(bucket=bucket, object_key=key), b"PK-fake-zip",
        PutOptions(artifact_class="temporary"),
    )
    expires = (datetime.now(timezone.utc)
               + timedelta(hours=expires_hours)).isoformat()
    await repo.update_task(
        row["id"], status="succeeded", phase="finalize",
        artifact_bucket=bucket, artifact_object_key=key,
        artifact_size=res.size, artifact_sha256=res.sha256, doc_count=1,
        expires_at=expires)
    return await repo.get_task(row["id"])


# ------------------------------------------------------------------ 创建闸门

async def test_create_export_runs_to_success(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env, docs=2)
    result = await env["svc"].create_export(kb_id=kb["id"], user_id=owner["id"])
    assert result["already_started"] is False
    assert result["status"] == "queued"
    await _drain_bg()
    rows = await env["svc"].list_tasks(kb_id=kb["id"], user_id=owner["id"])
    assert len(rows) == 1 and rows[0]["status"] == "succeeded"
    assert rows[0]["doc_count"] == 2


async def test_create_export_idempotent_while_active(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    repo = et.ExportTaskRepo(env["pool"])
    active = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
        requested_by=owner["id"])
    await repo.update_task(active["id"], status="running", phase="packaging")

    again = await env["svc"].create_export(kb_id=kb["id"], user_id=owner["id"])
    assert again["already_started"] is True
    assert again["id"] == active["id"]
    rows = await env["svc"].list_tasks(kb_id=kb["id"], user_id=owner["id"])
    assert len(rows) == 1  # 没有重复任务


async def test_create_export_invisible_404(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)  # private
    stranger = await env["db"].upsert_user_by_username(f"s_{_hex()[:10]}")
    with pytest.raises(NotFound):
        await env["svc"].create_export(kb_id=kb["id"], user_id=stranger["id"])


async def test_create_export_empty_kb_422(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env, docs=0)
    with pytest.raises(SourceEmptyExport):
        await env["svc"].create_export(kb_id=kb["id"], user_id=owner["id"])


async def test_create_export_too_large_gate(svc_env, tmp_path):
    env = svc_env
    owner, kb = await _seed_kb(env, docs=1)
    env["config"].export_max_export_bytes = 1
    with pytest.raises(ExportTooLarge):
        await env["svc"].create_export(kb_id=kb["id"], user_id=owner["id"])


async def test_create_export_disabled_gate(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    env["config"].export_enabled = False
    with pytest.raises(ExportDisabled):
        await env["svc"].create_export(kb_id=kb["id"], user_id=owner["id"])


# ------------------------------------------------------------------ 列表权限

async def test_list_tasks_domain_admin_sees_all(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env, visibility="public")
    other = await env["db"].upsert_user_by_username(f"m_{_hex()[:10]}")
    admin = await env["db"].upsert_user_by_username(f"a_{_hex()[:10]}")
    async with env["pool"].connection() as conn:
        await conn.execute(
            "UPDATE kb_users SET site_role='admin' WHERE id=%s", [admin["id"]],
        )
    await _succeeded_row(env, kb, requested_by=owner["id"])
    await _succeeded_row(env, kb, requested_by=other["id"])

    mine = await env["svc"].list_tasks(kb_id=kb["id"], user_id=other["id"])
    assert {r["requested_by"] for r in mine} == {other["id"]}
    every = await env["svc"].list_tasks(kb_id=kb["id"], user_id=admin["id"])
    assert {r["requested_by"] for r in every} == {owner["id"], other["id"]}


# ------------------------------------------------------- 详情 / 下载 / 删除

async def test_get_task_stranger_404_requester_ok(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    stranger = await env["db"].upsert_user_by_username(f"x_{_hex()[:10]}")
    row = await _succeeded_row(env, kb, requested_by=owner["id"])
    with pytest.raises(NotFound):
        await env["svc"].get_task(task_id=row["id"], user_id=stranger["id"])
    got = await env["svc"].get_task(task_id=row["id"], user_id=owner["id"])
    assert got["status"] == "succeeded" and got["artifact_sha256"]


async def test_download_url_only_succeeded_unexpired(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    repo = et.ExportTaskRepo(env["pool"])

    running = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
        requested_by=owner["id"])
    await repo.update_task(running["id"], status="running")
    with pytest.raises(ExportTaskConflict):
        await env["svc"].download_url(task_id=running["id"],
                                      user_id=owner["id"])

    expired = await _succeeded_row(env, kb, requested_by=owner["id"],
                                   expires_hours=-1.0)
    with pytest.raises(ExportTaskConflict):
        await env["svc"].download_url(task_id=expired["id"],
                                      user_id=owner["id"])


async def test_download_url_presigns_with_chinese_filename(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    row = await _succeeded_row(env, kb, requested_by=owner["id"])

    access = await env["svc"].download_url(task_id=row["id"],
                                           user_id=owner["id"])
    assert access["expires_in"] == 3600
    assert access["sha256"] == row["artifact_sha256"]
    assert access["size"] == row["artifact_size"]
    # 浏览器保存名带中文名（fake URL 附加 filename= 编码值，对齐 MinIO
    # response-content-disposition 行为）
    from urllib.parse import quote
    assert f"filename={quote(access['filename'])}" in access["url"]
    assert access["filename"].startswith("产品库-") and \
        access["filename"].endswith(".zip")


async def test_delete_task_cancel_vs_delete(svc_env):
    env = svc_env
    owner, kb = await _seed_kb(env)
    repo = et.ExportTaskRepo(env["pool"])

    # 活跃任务 → 取消（无 worker 在跑：DB 兜底收口 cancelled）
    active = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
        requested_by=owner["id"])
    await repo.update_task(active["id"], status="running")
    cancelled = await env["svc"].delete_task(task_id=active["id"],
                                             user_id=owner["id"])
    assert cancelled["cancelled"] is True
    assert (await repo.get_task(active["id"]))["status"] == "cancelled"

    # 终态任务 → 产物对象删除 + 行删除
    done = await _succeeded_row(env, kb, requested_by=owner["id"])
    deleted = await env["svc"].delete_task(task_id=done["id"],
                                           user_id=owner["id"])
    assert deleted["deleted"] is True
    assert await repo.get_task(done["id"]) is None
    assert not await env["store"].head_exists(ObjectLocation(
        bucket=done["artifact_bucket"],
        object_key=done["artifact_object_key"]))


# ------------------------------------------------------------------ 路由层

async def _client(svc_env):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from knowledge_mining.mining.kb.routes.exports import router

    app = FastAPI()
    app.state.pg_pool = svc_env["pool"]
    app.state.object_store = svc_env["store"]
    app.state.object_store_config = svc_env["store_cfg"]
    app.include_router(router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_routes_lifecycle_202_to_download(svc_env):
    from knowledge_mining.tests.conftest import kb_headers

    env = svc_env
    owner, kb = await _seed_kb(env, docs=1)
    async with await _client(env) as c:
        # 创建 → 202
        r = await c.post(f"/api/kb/{kb['id']}/export", json={},
                         headers=kb_headers(owner["username"]))
        assert r.status_code == 202, r.text
        task_id = r.json()["id"]
        await _drain_bg()

        # 详情 → succeeded；下载 URL 现签可用
        r = await c.get(f"/api/kb/export-tasks/{task_id}",
                        headers=kb_headers(owner["username"]))
        assert r.status_code == 200 and r.json()["status"] == "succeeded"
        r = await c.get(f"/api/kb/export-tasks/{task_id}/download-url",
                        headers=kb_headers(owner["username"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"].startswith("fake://kb-x-staging/export/")
        assert body["filename"].endswith(".zip")

        # 列表
        r = await c.get(f"/api/kb/{kb['id']}/export-tasks",
                        headers=kb_headers(owner["username"]))
        assert r.status_code == 200 and len(r.json()["tasks"]) == 1

        # 终态删除 → 200
        r = await c.delete(f"/api/kb/export-tasks/{task_id}",
                           headers=kb_headers(owner["username"]))
        assert r.status_code == 200 and r.json()["deleted"] is True


async def test_routes_error_mapping(svc_env):
    from knowledge_mining.tests.conftest import kb_headers

    env = svc_env
    owner, kb = await _seed_kb(env)  # private
    stranger = await env["db"].upsert_user_by_username(f"y_{_hex()[:10]}")
    async with await _client(env) as c:
        # 不可见 → 404（不泄露存在性）
        r = await c.post(f"/api/kb/{kb['id']}/export", json={},
                         headers=kb_headers(stranger["username"]))
        assert r.status_code == 404
        # 空库 → 422
        empty_owner, empty_kb = await _seed_kb(env, docs=0)
        r = await c.post(f"/api/kb/{empty_kb['id']}/export", json={},
                         headers=kb_headers(empty_owner["username"]))
        assert r.status_code == 422
        # 未完成任务取下载 URL → 409
        repo = et.ExportTaskRepo(env["pool"])
        row = await repo.insert_task(
            kb_id=kb["id"], kb_name=kb["name"], domain=kb["domain"],
            requested_by=owner["id"])
        await repo.update_task(row["id"], status="running")
        r = await c.get(f"/api/kb/export-tasks/{row['id']}/download-url",
                        headers=kb_headers(owner["username"]))
        assert r.status_code == 409
        # 不存在 → 404
        r = await c.get("/api/kb/export-tasks/nope",
                        headers=kb_headers(owner["username"]))
        assert r.status_code == 404
