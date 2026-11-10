"""整库拷贝（v1.1 快照血缘共享）— CopyService + /api/kb/{id}/copy。

设计契约见 docs/kb-copy-design.md：
- 拷文件 + 目录树 + snapshot links；**不建 Build**——拷完文档均为 uploaded、
  不可检索（get_current_serving_snapshot 为 None）；
- 对象字节零复制（storage_object_id 同源共享）；
- 配置只复制 visibility（防放大：public 仅源 owner / site-admin）；
- 新库 mining_workflow_id = NULL（范式不复制）。
"""
from __future__ import annotations

import uuid

import pytest

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.services.copy_service import (
    MAX_COPY_DOCUMENTS,
    CopyService,
    SourceEmpty,
    SourceTooLarge,
    _derive_copy_name,
)
from knowledge_mining.mining.kb.services.kb_service import (
    DomainNotBound,
    Duplicate,
    Forbidden,
    NotFound,
)

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"


def _hex() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------ helpers

async def _seed_source_kb(
    pool, *, name: str, visibility: str = "private",
    with_build: bool = True, extra_soft_deleted: bool = False,
    owner: dict | None = None,
):
    """源库：1 文件夹 + 2 active 文档（a.md 带对象身份 + 可选 Build 快照血缘；
    b.md 仅身份行）。返回 (owner_id, kb_id, doc_a, doc_b, snap_id)。"""
    db = KbDB(pool)
    if owner is None:
        owner = await db.upsert_user_by_username(f"u_{_hex()[:10]}")
    kb = await db.create_kb(
        domain=DOMAIN, name=name, owner_id=owner["id"], visibility=visibility,
    )
    folder = await db.insert_folder(
        folder_id=_hex(), kb_id=kb["id"], parent_id=None, name="5G",
        path="5G", created_by=owner["id"],
    )
    doc_a = await db.insert_document_from_storage(
        domain=DOMAIN, kb_id=kb["id"], document_key="doc:/5G/a.md",
        document_name="a.md", storage_object_id=f"so-{_hex()[:8]}",
        source_raw_hash=f"rh-{_hex()[:8]}", directory_path="5G",
        document_type="reference", owner_id=owner["id"], file_size=123,
    )
    doc_b = await db.insert_document_identity(
        domain=DOMAIN, kb_id=kb["id"], document_key="doc:/b.md",
        document_name="b.md", storage_path="/tmp/b.md", directory_path="",
        owner_id=owner["id"],
    )
    snap_id = None
    if with_build:
        snap_id = f"snap-{_hex()[:10]}"
        build_id = f"build-{_hex()[:10]}"
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO asset_document_snapshots
                   (id, domain, normalized_content_hash, raw_content_hash,
                    mime_type, created_at)
                   VALUES (%s, %s, %s, %s, 'text/markdown', %s)""",
                (snap_id, DOMAIN, f"nh-{snap_id}", f"rh2-{snap_id}",
                 "2026-01-01T00:00:00+00:00"),
            )
            await conn.execute(
                """INSERT INTO asset_builds
                   (id, build_code, status, build_mode, domain, kb_id, created_at)
                   VALUES (%s, %s, 'validated', 'full', %s, %s, %s)""",
                (build_id, f"BUILD-{build_id}", DOMAIN, kb["id"],
                 "2026-01-01T00:00:00+00:00"),
            )
            await conn.execute(
                """INSERT INTO asset_build_document_snapshots
                   (build_id, document_id, document_snapshot_id,
                    selection_status, reason)
                   VALUES (%s, %s, %s, 'active', 'add')""",
                (build_id, doc_a["id"], snap_id),
            )
            await conn.execute(
                """INSERT INTO asset_document_snapshot_links
                   (id, document_id, document_snapshot_id, relative_path,
                    source_uri, title, linked_at, source_storage_object_id,
                    source_content_revision)
                   VALUES (%s, %s, %s, '5G/a.md', 'kb://src/5G/a.md', 'a',
                           %s, %s, 1)""",
                (_hex(), doc_a["id"], snap_id,
                 "2026-01-01T00:00:00+00:00", doc_a["storage_object_id"]),
            )
    if extra_soft_deleted:
        doc_c = await db.insert_document_identity(
            domain=DOMAIN, kb_id=kb["id"], document_key="doc:/c.md",
            document_name="c.md", storage_path="/tmp/c.md", directory_path="",
            owner_id=owner["id"],
        )
        await db.soft_delete_document(doc_c["id"])
    return owner["id"], kb["id"], doc_a, doc_b, snap_id


async def _links_of(pool, document_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, document_id, document_snapshot_id, source_batch_id,
                      relative_path, source_uri, linked_at
               FROM asset_document_snapshot_links WHERE document_id = %s""",
            [document_id],
        )
        return [dict(r) for r in await cur.fetchall()]


# ------------------------------------------------------- 名称派生（纯函数）

async def test_derive_copy_name_basic():
    name = _derive_copy_name("产品手册")
    assert name.startswith("产品手册-副本-") and len(name) == len("产品手册") + 12


async def test_derive_copy_name_truncates_long_source():
    # 源名超长时截源名保后缀，总长 ≤80（normalize_kb_name 的上限）
    long_source = "长" * 100
    name = _derive_copy_name(long_source)
    assert len(name) == 80
    assert name.endswith("-副本-" + name[-8:])


# ------------------------------------------------------------ 基本拷贝语义

async def test_copy_basic_lineage_and_shape(async_pool):
    db = KbDB(async_pool)
    owner_id, kb_id, doc_a, doc_b, snap_id = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}",
    )
    svc = CopyService(db)

    result = await svc.copy_kb(kb_id=kb_id, actor_id=owner_id, overrides={})

    # 新 KB 行：owner=操作者、visibility 继承、范式不复制、copy_source 溯源
    assert result["owner_id"] == owner_id
    assert result["visibility"] == "private"
    assert result["mining_workflow_id"] is None
    assert result["copy_source"]["kb_id"] == kb_id
    assert result["copy_stats"] == {"documents": 2, "folders": 1, "linked_snapshots": 1}
    assert result["next_step_hint"] == "select_mining_paradigm"
    assert result["readiness"]["level"] == "empty"  # 无 Build → 空档（不可检索）

    new_kb_id = result["id"]

    # 目录树：path 原样、id 全新
    folders = await db.list_folders(new_kb_id)
    assert [f["path"] for f in folders] == ["5G"]
    assert folders[0]["id"] != (await db.list_folders(kb_id))[0]["id"]

    # 文档：全部 uploaded（无 Build 成员）、key/对象指针同源、owner=操作者
    docs = await db.list_documents_in_kb(kb_id=new_kb_id)
    assert len(docs) == 2 and all(d["status"] == "uploaded" for d in docs)
    by_key = {d["document_key"]: d for d in docs}
    assert by_key["doc:/5G/a.md"]["storage_object_id"] == doc_a["storage_object_id"]
    assert by_key["doc:/5G/a.md"]["source_raw_hash"] == doc_a["source_raw_hash"]
    assert by_key["doc:/5G/a.md"]["owner_id"] == owner_id
    assert by_key["doc:/5G/a.md"]["folder_id"] == folders[0]["id"]  # 重映射
    assert by_key["doc:/b.md"]["folder_id"] is None

    # 快照血缘：仅 a.md（有 build 映射）建 link，指向同一 snapshot；b.md 无
    new_a = by_key["doc:/5G/a.md"]["id"]
    new_b = by_key["doc:/b.md"]["id"]
    links = await _links_of(async_pool, new_a)
    assert len(links) == 1
    assert links[0]["document_snapshot_id"] == snap_id
    assert links[0]["source_batch_id"] is None
    assert links[0]["relative_path"] == "5G/a.md"
    assert await _links_of(async_pool, new_b) == []

    # 不可检索（预期行为）：无 current_serving 快照
    assert await db.get_current_serving_snapshot(new_kb_id, new_a) is None
    # 源库不受影响：serving 仍在、文档仍在
    assert (await db.get_current_serving_snapshot(kb_id, doc_a["id"]))[
        "document_snapshot_id"] == snap_id
    assert len(await db.list_documents_in_kb(kb_id=kb_id)) == 2


async def test_copy_excludes_soft_deleted_docs(async_pool):
    db = KbDB(async_pool)
    owner_id, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", extra_soft_deleted=True,
    )
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=owner_id, overrides={},
    )
    assert result["copy_stats"]["documents"] == 2  # 软删的 c.md 不拷


# ------------------------------------------------------ 权限与防放大规则

async def _make_member(pool, db: KbDB, kb_id: str, role: str = "viewer"):
    member = await db.upsert_user_by_username(f"m_{_hex()[:10]}")
    await db.add_member(kb_id=kb_id, user_id=member["id"], role=role)
    return member["id"]


async def test_member_can_copy_private_kb(async_pool):
    """member 拷 private 库合法（读到的东西变成自己名下副本），副本强制 private。"""
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}",
    )
    member_id = await _make_member(async_pool, db, kb_id)
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=member_id, overrides={},
    )
    assert result["owner_id"] == member_id
    assert result["visibility"] == "private"


async def test_member_explicit_public_forbidden(async_pool):
    """防放大：member 显式请求 public → 403 visibility_not_allowed。"""
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}",
    )
    member_id = await _make_member(async_pool, db, kb_id)
    with pytest.raises(Forbidden, match="visibility_not_allowed"):
        await CopyService(db).copy_kb(
            kb_id=kb_id, actor_id=member_id,
            overrides={"visibility": "public"},
        )


async def test_non_member_public_copy_downgrades_to_private(async_pool):
    """非 owner 拷 public 库未显式指定 visibility → 强制 private（防放大）。"""
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", visibility="public",
    )
    stranger = await db.upsert_user_by_username(f"s_{_hex()[:10]}")
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=stranger["id"], overrides={},
    )
    assert result["visibility"] == "private"


async def test_owner_can_copy_public(async_pool):
    db = KbDB(async_pool)
    owner_id, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", visibility="public",
    )
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=owner_id, overrides={},
    )
    assert result["visibility"] == "public"


async def test_site_admin_can_copy_public(async_pool):
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", visibility="public",
    )
    admin = await db.upsert_user_by_username(f"adm_{_hex()[:10]}")
    async with async_pool.connection() as conn:
        await conn.execute(
            "UPDATE kb_users SET site_role='admin' WHERE id=%s", [admin["id"]],
        )
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=admin["id"], overrides={"visibility": "public"},
    )
    assert result["visibility"] == "public"


async def test_invisible_source_404(async_pool):
    """非成员拷他人 private 库 → NotFound（不泄露存在性）。"""
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(async_pool, name=f"src-{_hex()[:8]}")
    stranger = await db.upsert_user_by_username(f"s_{_hex()[:10]}")
    with pytest.raises(NotFound):
        await CopyService(db).copy_kb(
            kb_id=kb_id, actor_id=stranger["id"], overrides={},
        )


async def test_unbound_operator_unified_404(async_pool):
    """未绑定域的操作者：public 库也统一 404（不泄露存在性）。

    51号批次1起 is_visible 本身要求 access_domain 绑定——未绑定用户在可见性
    闸门即被挡，走不到建库资格检查（can_create_in_domain → DomainNotBound）；
    DomainNotBound 经 copy_kb 仅剩"可见性复核与建库之间被解绑"的竞态路径
    （db.copy_kb 事务内复核，译法见 copy_service.UserDeletionConflict 分支）。
    另：55号守卫要求 member 至少保留一个域绑定——先补第二域再解绑 DOMAIN。
    """
    db = KbDB(async_pool)
    _, kb_id, *_ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", visibility="public",
    )
    outsider = await db.upsert_user_by_username(f"o_{_hex()[:10]}")
    async with async_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO user_domains (user_id, domain) VALUES (%s, 'generic')"
            " ON CONFLICT DO NOTHING",
            [outsider["id"]],
        )
    await db.unbind_domain(user_id=outsider["id"], domain=DOMAIN)
    with pytest.raises(NotFound):
        await CopyService(db).copy_kb(
            kb_id=kb_id, actor_id=outsider["id"], overrides={},
        )


# ------------------------------------------------------- 名称与闸门

async def test_copy_name_conflict_duplicate(async_pool):
    db = KbDB(async_pool)
    owner_id, kb_id, *_ = await _seed_source_kb(async_pool, name=f"src-{_hex()[:8]}")
    svc = CopyService(db)
    first = await svc.copy_kb(
        kb_id=kb_id, actor_id=owner_id, overrides={"new_name": "我的副本"},
    )
    assert first["name"] == "我的副本"
    with pytest.raises(Duplicate):
        await svc.copy_kb(
            kb_id=kb_id, actor_id=owner_id, overrides={"new_name": "我的副本"},
        )


async def test_copy_empty_source_422(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username(f"e_{_hex()[:10]}")
    kb = await db.create_kb(domain=DOMAIN, name=f"empty-{_hex()[:8]}",
                            owner_id=owner["id"])
    with pytest.raises(SourceEmpty):
        await CopyService(db).copy_kb(
            kb_id=kb["id"], actor_id=owner["id"], overrides={},
        )


async def test_copy_over_limit_422(async_pool, monkeypatch):
    db = KbDB(async_pool)
    owner_id, kb_id, *_ = await _seed_source_kb(async_pool, name=f"src-{_hex()[:8]}")
    monkeypatch.setattr(
        "knowledge_mining.mining.kb.services.copy_service.MAX_COPY_DOCUMENTS", 1,
    )
    with pytest.raises(SourceTooLarge):
        await CopyService(db).copy_kb(
            kb_id=kb_id, actor_id=owner_id, overrides={},
        )
    assert MAX_COPY_DOCUMENTS == 5000  # 常量本身不被测试改写


# ---------------------------------------------------- 拷贝后的生命周期隔离

async def test_copy_then_source_doc_delete_keeps_copy(async_pool):
    """源库后续删文档不影响副本（副本身份独立、对象按全局引用计数不误删）。"""
    db = KbDB(async_pool)
    owner_id, kb_id, doc_a, doc_b, _ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}",
    )
    result = await CopyService(db).copy_kb(
        kb_id=kb_id, actor_id=owner_id, overrides={},
    )
    await db.soft_delete_document(doc_a["id"])
    await db.soft_delete_document(doc_b["id"])

    docs = await db.list_documents_in_kb(kb_id=result["id"])
    assert len(docs) == 2  # 副本文档仍 active
    assert docs  # 拷贝的文档身份与源库生命周期解耦


# ------------------------------------------------------------- 路由层（HTTP）

async def _client(async_pool):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from knowledge_mining.mining.kb.routes.kbs import router as kb_router

    app = FastAPI()
    app.state.pg_pool = async_pool
    app.include_router(kb_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_route_copy_201_and_mapping(async_pool):
    from knowledge_mining.tests.conftest import kb_headers

    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username(f"r_{_hex()[:10]}")
    _, kb_id, _, _, _ = await _seed_source_kb(
        async_pool, name=f"src-{_hex()[:8]}", owner=owner,
    )
    async with await _client(async_pool) as c:
        r = await c.post(
            f"/api/kb/{kb_id}/copy", json={},
            headers=kb_headers(owner["username"]),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["copy_stats"]["documents"] == 2
        assert body["next_step_hint"] == "select_mining_paradigm"
        assert body["copy_source"]["kb_id"] == kb_id

        # 重名 → 409（同显式名）
        r = await c.post(
            f"/api/kb/{kb_id}/copy", json={"new_name": body["name"]},
            headers=kb_headers(owner["username"]),
        )
        assert r.status_code == 409

        # 不可见 → 404
        stranger = await db.upsert_user_by_username(f"x_{_hex()[:10]}")
        r = await c.post(
            f"/api/kb/{kb_id}/copy", json={},
            headers=kb_headers(stranger["username"]),
        )
        assert r.status_code == 404

        # 空库 → 422
        empty_kb = await db.create_kb(
            domain=DOMAIN, name=f"empty-{_hex()[:8]}", owner_id=owner["id"],
        )
        r = await c.post(
            f"/api/kb/{empty_kb['id']}/copy", json={},
            headers=kb_headers(owner["username"]),
        )
        assert r.status_code == 422
