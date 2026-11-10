"""整库导出 worker / 重启恢复 / 过期清理 — async_pool + FakeObjectStore.

覆盖：对象文档 + legacy 本地文档混打包、缺对象降级 warnings、取消收口、
恢复重跑幂等、sweep 删产物与孤儿 .part。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from knowledge_mining.mining.contracts.file_management import StorageObjectRecord
from knowledge_mining.mining.contracts.storage.types import (
    ObjectLocation, PutOptions,
)
from knowledge_mining.mining.file_management.repositories_pg import (
    PgStorageObjectRepository,
)
from knowledge_mining.mining.infra import control_plane as cp
from knowledge_mining.mining.infra.export_config import ExportConfig
from knowledge_mining.mining.infra.object_store.config import ObjectStoreConfig
from knowledge_mining.mining.infra.object_store.fake import FakeObjectStore
from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.services import export_tasks as et

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"


def _hex() -> str:
    return uuid.uuid4().hex


async def _drain_bg() -> None:
    """等所有后台导出任务收口（异常吞掉，断言看 DB 终态）."""
    tasks = [t for t in et._bg_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def export_env(async_pool, tmp_path):
    """pool + Fake 对象存储 + tmp 暂存配置（upload.root 也指到 tmp）."""
    orig = cp.get_mining_service_config()
    upload_root = tmp_path / "uploads"
    cp.set_mining_service_config({
        **orig,
        "upload": {**orig.get("upload", {}), "root": str(upload_root)},
    })
    obj_root = tmp_path / "objstore"
    store = FakeObjectStore(str(obj_root))
    store_cfg = ObjectStoreConfig(
        provider="fake", bucket_prefix="kb-x-", root_path=str(obj_root),
    )
    config = ExportConfig(
        export_tmp_root=str(tmp_path / "exports"),
        export_batch_size=2,
        export_progress_interval_docs=1,
        export_progress_interval_seconds=0.01,
    )
    try:
        yield {
            "pool": async_pool, "db": KbDB(async_pool), "store": store,
            "store_cfg": store_cfg, "config": config,
            "upload_root": upload_root,
        }
    finally:
        cp.set_mining_service_config(orig)


async def _seed_object_doc(env, kb_id: str, owner_id: str, *, name="a.md",
                           directory="5G", key="doc:/5G/a.md", data=None):
    """注册一个 MinIO 侧对象（fake store bytes + asset_storage_objects 行）+ 文档行."""
    payload = data if data is not None else "对象文档内容".encode("utf-8") * 10
    oid = f"so-{_hex()[:10]}"
    bucket = "kb-x-source"
    obj_key = f"objects/{oid}"
    res = await env["store"].put_bytes(
        ObjectLocation(bucket=bucket, object_key=obj_key),
        payload, PutOptions(artifact_class="source"),
    )
    await PgStorageObjectRepository(env["pool"]).register(StorageObjectRecord(
        id=oid, provider="fake", bucket=bucket, object_key=obj_key,
        object_version_id=None, sha256=res.sha256, size=res.size,
        mime="text/markdown", artifact_class="source", state="AVAILABLE",
        etag=res.etag,
    ))
    doc = await env["db"].insert_document_from_storage(
        domain=DOMAIN, kb_id=kb_id, document_key=key, document_name=name,
        storage_object_id=oid, source_raw_hash=res.sha256,
        directory_path=directory, document_type="reference",
        owner_id=owner_id, file_size=len(payload),
    )
    return doc, res.sha256, payload


async def _seed_legacy_doc(env, kb_id: str, owner_id: str, *, name="b.md",
                           directory="legacy", key="doc:/legacy/b.md"):
    """legacy 本地文档：文件真实落在 upload_root/{kb_id}/…，storage_path 记绝对路径."""
    payload = "legacy 本地文档".encode("utf-8") * 8
    target = env["upload_root"] / kb_id / directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return await env["db"].insert_document_identity(
        domain=DOMAIN, kb_id=kb_id, document_key=key, document_name=name,
        storage_path=str(target), directory_path=directory, owner_id=owner_id,
    ), payload


async def _seed_kb(env, *, with_object=True, with_legacy=True, broken=False):
    db = env["db"]
    owner = await db.upsert_user_by_username(f"u_{_hex()[:10]}")
    kb = await db.create_kb(
        domain=DOMAIN, name=f"exp-{_hex()[:8]}", owner_id=owner["id"],
    )
    await db.insert_folder(
        folder_id=_hex(), kb_id=kb["id"], parent_id=None, name="5G",
        path="5G", created_by=owner["id"],
    )
    seeded = {"obj": None, "legacy": None, "broken": None, "obj_payload": None}
    if with_object:
        seeded["obj"], seeded["obj_sha"], seeded["obj_payload"] = (
            await _seed_object_doc(env, kb["id"], owner["id"]))
    if with_legacy:
        seeded["legacy"], seeded["legacy_payload"] = await _seed_legacy_doc(
            env, kb["id"], owner["id"])
    if broken:
        # storage_object_id 指向不存在的注册行 → 打包时降级为 warning
        seeded["broken"] = await db.insert_document_from_storage(
            domain=DOMAIN, kb_id=kb["id"], document_key="doc:/broken.md",
            document_name="broken.md", storage_object_id=f"so-{_hex()[:8]}",
            source_raw_hash=f"rh-{_hex()[:8]}", directory_path="",
            document_type="reference", owner_id=owner["id"], file_size=7,
        )
    return owner, kb, seeded


# ------------------------------------------------------------------ 打包主体

async def test_run_export_packs_object_and_legacy_docs(export_env):
    env = export_env
    owner, kb, seeded = await _seed_kb(env)
    kb_row = await env["db"].get_kb(kb["id"])
    row = await et.start_export_task(
        pool=env["pool"], object_store=env["store"],
        object_store_config=env["store_cfg"], config=env["config"],
        kb=kb_row, requested_by=owner["id"])
    await _drain_bg()

    repo = et.ExportTaskRepo(env["pool"])
    final = await repo.get_task(row["id"])
    assert final["status"] == "succeeded", final["error"]
    assert final["doc_count"] == 2
    assert final["artifact_bucket"] == "kb-x-staging"
    assert final["artifact_object_key"] == f"export/{kb['id']}/{row['id']}.zip"
    assert final["expires_at"] is not None

    # 产物可从对象存储读回：sha 对得上、zip 结构/manifest 映射完整
    loc = ObjectLocation(bucket=final["artifact_bucket"],
                         object_key=final["artifact_object_key"])
    data = b"".join([c async for c in env["store"].get_stream(loc)])
    assert hashlib.sha256(data).hexdigest() == final["artifact_sha256"]
    assert len(data) == final["artifact_size"]

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "5G/a.md" in names and "legacy/b.md" in names
        assert names[-1] == "manifest.json"
        assert zf.read("5G/a.md") == seeded["obj_payload"]
        assert zf.read("legacy/b.md") == seeded["legacy_payload"]
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == "coremasterkb-kb-export"
        assert manifest["kb"]["id"] == kb["id"]
        assert manifest["stats"] == {
            "total_documents": 2, "packed_documents": 2,
            "skipped_documents": 0,
            "packed_bytes": len(seeded["obj_payload"]) + len(seeded["legacy_payload"]),
        }
        assert [d["arcname"] for d in manifest["documents"]] == [
            "5G/a.md", "legacy/b.md"]
        assert manifest["documents"][0]["sha256"] == seeded["obj_sha"]
        assert manifest["folders"] and manifest["folders"][0]["path"] == "5G"

    # .part 暂存文件已清理；进度终值带 zip 校验信息
    assert not (env["config"].tmp_root_path / f"{row['id']}.zip.part").exists()
    progress = json.loads(final["progress_json"]) if isinstance(
        final["progress_json"], str) else final["progress_json"]
    assert progress["zip_sha256"] == final["artifact_sha256"]


async def test_run_export_degrades_missing_object_to_warning(export_env):
    env = export_env
    owner, kb, _ = await _seed_kb(env, with_object=True, with_legacy=False,
                                  broken=True)
    kb_row = await env["db"].get_kb(kb["id"])
    row = await et.start_export_task(
        pool=env["pool"], object_store=env["store"],
        object_store_config=env["store_cfg"], config=env["config"],
        kb=kb_row, requested_by=owner["id"])
    await _drain_bg()

    final = await et.ExportTaskRepo(env["pool"]).get_task(row["id"])
    assert final["status"] == "succeeded", final["error"]
    assert final["doc_count"] == 1  # 只算打包成功的

    loc = ObjectLocation(bucket=final["artifact_bucket"],
                         object_key=final["artifact_object_key"])
    data = b"".join([c async for c in env["store"].get_stream(loc)])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["stats"]["skipped_documents"] == 1
        assert manifest["warnings"][0]["document_key"] == "doc:/broken.md"
        assert "not available" in manifest["warnings"][0]["error"]


async def test_cancel_before_worker_starts_lands_cancelled(export_env):
    """排队即取消（协程体未及执行）：request_cancel 兜底收口，不悬挂 cancelling."""
    env = export_env
    owner, kb, _ = await _seed_kb(env, with_object=True, with_legacy=False)
    kb_row = await env["db"].get_kb(kb["id"])
    row = await et.start_export_task(
        pool=env["pool"], object_store=env["store"],
        object_store_config=env["store_cfg"], config=env["config"],
        kb=kb_row, requested_by=owner["id"])
    # worker 尚未让出控制权 → Task.cancel 在协程体执行前送达
    result = await et.request_cancel(env["pool"], row["id"])
    assert result["status"] == "cancelled"
    await _drain_bg()
    final = await et.ExportTaskRepo(env["pool"]).get_task(row["id"])
    assert final["status"] == "cancelled"
    # 没产物
    assert final["artifact_object_key"] is None


# ------------------------------------------------------------------ 重启恢复

async def test_recover_reruns_running_and_closes_cancelling(export_env):
    env = export_env
    owner, kb, seeded = await _seed_kb(env, with_object=True, with_legacy=True)
    repo = et.ExportTaskRepo(env["pool"])

    running = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=DOMAIN,
        requested_by=owner["id"])
    await repo.update_task(running["id"], status="running", phase="packaging")
    stale = env["config"].tmp_root_path / f"{running['id']}.zip.part"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale-from-crash")

    cancelling = await repo.insert_task(
        kb_id=kb["id"], kb_name=kb["name"], domain=DOMAIN,
        requested_by=owner["id"])
    await repo.update_task(cancelling["id"], status="cancelling")

    recovered = await et.recover_export_tasks(
        pool=env["pool"], object_store=env["store"],
        object_store_config=env["store_cfg"], config=env["config"])
    assert recovered == 1
    await _drain_bg()

    rerun = await repo.get_task(running["id"])
    assert rerun["status"] == "succeeded", rerun["error"]
    assert rerun["doc_count"] == 2
    assert not stale.exists()  # 残留 .part 已随重跑清理
    assert (await repo.get_task(cancelling["id"]))["status"] == "cancelled"


# ------------------------------------------------------------------ 过期清理

async def test_sweep_expires_artifacts_and_orphan_parts(export_env):
    env = export_env
    repo = et.ExportTaskRepo(env["pool"])
    bucket = env["config"].bucket_for(env["store_cfg"])
    key = f"export/kb-x/{_hex()[:8]}.zip"
    await env["store"].put_bytes(
        ObjectLocation(bucket=bucket, object_key=key), b"PK-zip-bytes",
        PutOptions(artifact_class="temporary"),
    )
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    row = await repo.insert_task(
        kb_id="kb-x", kb_name="随便", domain=DOMAIN, requested_by="u1")
    await repo.update_task(
        row["id"], status="succeeded", artifact_bucket=bucket,
        artifact_object_key=key, artifact_size=12, expires_at=past)

    # 未过期的 succeeded 不动
    fresh = await repo.insert_task(
        kb_id="kb-x", kb_name="随便2", domain=DOMAIN, requested_by="u1")
    future = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
    await repo.update_task(fresh["id"], status="succeeded", expires_at=future)

    # 孤儿 .part（mtime 25h 前）+ 新 .part（不动）
    env["config"].tmp_root_path.mkdir(parents=True, exist_ok=True)
    orphan = env["config"].tmp_root_path / f"{_hex()[:8]}.zip.part"
    orphan.write_bytes(b"orphan")
    old = time.time() - 25 * 3600
    os.utime(orphan, (old, old))
    recent = env["config"].tmp_root_path / f"{_hex()[:8]}.zip.part"
    recent.write_bytes(b"recent")

    result = await et.sweep_expired_exports(
        pool=env["pool"], object_store=env["store"], config=env["config"])

    assert result == {"expired": 1, "parts_cleaned": 1}
    assert (await repo.get_task(row["id"]))["status"] == "expired"
    assert not await env["store"].head_exists(
        ObjectLocation(bucket=bucket, object_key=key))
    assert (await repo.get_task(fresh["id"]))["status"] == "succeeded"
    assert not orphan.exists()
    assert recent.exists()
