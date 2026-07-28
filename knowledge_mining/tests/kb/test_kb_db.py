"""P2.1 — KbDB async repository (kb_users / knowledge_bases / kb_members)."""
from __future__ import annotations

import pytest

from knowledge_mining.mining.kb.db import KbDB

pytestmark = pytest.mark.asyncio


async def test_upsert_user_idempotent(async_pool):
    db = KbDB(async_pool)
    u1 = await db.upsert_user_by_username("alice", display_name="Alice")
    u2 = await db.upsert_user_by_username("alice")  # 幂等
    assert u1["username"] == "alice"
    assert u1["id"] == u2["id"]
    assert u2["display_name"] == "Alice"  # COALESCE 保留


async def test_create_kb_and_get(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain="cloud_core_network", name="KB-A", owner_id=owner["id"])
    assert kb["domain"] == "cloud_core_network"
    assert kb["status"] == "active"
    fetched = await db.get_kb(kb["id"])
    assert fetched is not None and fetched["name"] == "KB-A"


async def test_list_visible_owner_member_public(async_pool):
    db = KbDB(async_pool)
    alice = await db.upsert_user_by_username("alice")
    bob = await db.upsert_user_by_username("bob")
    carol = await db.upsert_user_by_username("carol")
    # alice 的 private，bob 的 public，carol 的 shared（加 alice 为 member）
    priv = await db.create_kb(domain="cloud_core_network", name="priv", owner_id=alice["id"], visibility="private")
    pub = await db.create_kb(domain="cloud_core_network", name="pub", owner_id=bob["id"], visibility="public")
    shared = await db.create_kb(domain="cloud_core_network", name="shared", owner_id=carol["id"], visibility="shared")
    await db.add_member(kb_id=shared["id"], user_id=alice["id"], role="viewer")

    visible_to_alice = {k["name"] for k in await db.list_visible(user_id=alice["id"], domain="cloud_core_network")}
    # alice 看得到：自己的 private + bob 的 public + carol 的 shared（被加为 member）
    assert visible_to_alice == {"priv", "pub", "shared"}

    visible_to_bob = {k["name"] for k in await db.list_visible(user_id=bob["id"], domain="cloud_core_network")}
    # bob 看得到：自己的 public + 别人的 public（无 private、无 shared 未入成员）
    assert visible_to_bob == {"pub"}


async def test_private_invisible_via_is_visible(async_pool):
    db = KbDB(async_pool)
    alice = await db.upsert_user_by_username("alice")
    bob = await db.upsert_user_by_username("bob")
    kb = await db.create_kb(domain="cloud_core_network", name="priv", owner_id=alice["id"], visibility="private")
    assert await db.is_visible(kb_id=kb["id"], user_id=alice["id"]) is True
    assert await db.is_visible(kb_id=kb["id"], user_id=bob["id"]) is False
    assert await db.can_write(kb_id=kb["id"], user_id=bob["id"]) is False


async def test_shared_editor_can_write(async_pool):
    db = KbDB(async_pool)
    alice = await db.upsert_user_by_username("alice")
    bob = await db.upsert_user_by_username("bob")
    kb = await db.create_kb(domain="cloud_core_network", name="sh", owner_id=alice["id"], visibility="shared")
    await db.add_member(kb_id=kb["id"], user_id=bob["id"], role="editor")
    assert await db.can_write(kb_id=kb["id"], user_id=bob["id"]) is True
    # 降级为 viewer → 不能写
    await db.add_member(kb_id=kb["id"], user_id=bob["id"], role="viewer")
    assert await db.can_write(kb_id=kb["id"], user_id=bob["id"]) is False
    assert await db.is_visible(kb_id=kb["id"], user_id=bob["id"]) is True  # 仍可读


async def test_soft_delete_hides_keeps_row(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain="cloud_core_network", name="K", owner_id=owner["id"])
    deleted = await db.soft_delete(kb["id"])
    assert deleted["status"] == "deleted"
    assert deleted["deleted_at"] is not None
    # 默认查不到（status='active' 过滤）
    assert await db.get_kb(kb["id"]) is None
    # 行还在
    assert (await db.get_kb(kb["id"], include_deleted=True))["status"] == "deleted"
    # list_visible 也过滤掉
    assert all(k["id"] != kb["id"] for k in await db.list_visible(user_id=owner["id"], domain="cloud_core_network"))


async def test_update_kb(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain="cloud_core_network", name="K", owner_id=owner["id"])
    updated = await db.update_kb(kb["id"], name="K2", visibility="shared")
    assert updated["name"] == "K2" and updated["visibility"] == "shared"


async def test_unique_domain_name(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    await db.create_kb(domain="cloud_core_network", name="dup", owner_id=owner["id"])
    with pytest.raises(Exception):
        await db.create_kb(domain="cloud_core_network", name="dup", owner_id=owner["id"])
    # 不同 domain 同名允许
    kb2 = await db.create_kb(domain="generic", name="dup", owner_id=owner["id"])
    assert kb2["domain"] == "generic"
