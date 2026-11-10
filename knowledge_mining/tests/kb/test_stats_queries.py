"""概览页统计的 SQL 语义（KbDB.stats_*）。

路由装配与边界在 test_stats_route.py（用假 KbDB，不需要库）；这里跑真 SQL。
写法与 test_overview_queries.py 一致。

⚠️ 需要 PostgreSQL（`_test` 结尾的可丢弃库），与 tests/kb 下其余用例一致。

这里最要紧的一组断言是**「当前知识」口径**：资产计数必须按「每个文档最近一次进入
validated/published build 的快照」算，而不是对 asset_* 表直接 COUNT。重挖会产生新快照，
累计计数会把挖了 3 遍的 10 篇文档报成 30 篇的知识量——图表上看不出来，但数字是错的。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_mining.mining.kb.db import KbDB

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
T0 = "2026-01-01T00:00:00+00:00"


def _uid() -> str:
    return uuid.uuid4().hex


async def _add_doc(db, kb, name):
    return await db.insert_document_identity(
        domain=DOMAIN, kb_id=kb["id"], document_key=f"doc:/{name}", document_name=name,
        storage_path=f"/tmp/{kb['id']}/{name}", directory_path="",
    )


async def _insert_run(
    pool, *, run_id, kb_id, status="completed", started_at=T0, committed=1,
):
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO mining_runs
               (id, kb_id, input_path, domain, channel, status, current_stage, started_at,
                finished_at, execution_engine, workflow_manifest_json, metadata_json,
                total_documents, new_count, updated_count, skipped_count,
                failed_count, committed_count)
               VALUES (%s, %s, %s, %s, 'prod', %s, 'done', %s, %s,
                       'workflow', '{}'::jsonb, '{}'::jsonb, 1, 1, 0, 0, 0, %s)""",
            (run_id, kb_id, f"/tmp/{kb_id}", DOMAIN, status, started_at, started_at,
             committed),
        )


async def _insert_run_document(pool, *, rd_id, run_id, document_key, status):
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO mining_run_documents
               (id, run_id, document_key, raw_content_hash, normalized_content_hash,
                action, status, started_at, finished_at)
               VALUES (%s, %s, %s, 'h', 'h', 'NEW', %s, %s, %s)""",
            (rd_id, run_id, document_key, status, T0, T0),
        )


async def _build_snapshot(
    pool, *, kb_id, document_id, build_status="validated", created_at=T0,
    selection_status="active", segments=0, units=(), embeddings=0,
):
    """造一份 build + 文档快照，并按需填充 segments / v2 检索单元 / v2 向量。

    返回 (build_id, snapshot_id)。units 传 representation_type 列表，好让类型
    分布也能断言（2026-09-01 口径切换后 stats_* 读 asset_retrieval_units_v2 /
    embeddings_v2，旧链表恒 0，往旧表插数据测不出任何东西）。
    """
    build_id, snap_id = _uid(), _uid()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO asset_document_snapshots
               (id, domain, normalized_content_hash, raw_content_hash, mime_type,
                created_at)
               VALUES (%s, %s, %s, %s, 'text/markdown', %s)""",
            (snap_id, DOMAIN, _uid(), _uid(), created_at),
        )
        await conn.execute(
            """INSERT INTO asset_builds
               (id, build_code, status, build_mode, domain, kb_id, created_at)
               VALUES (%s, %s, %s, 'full', %s, %s, %s)""",
            (build_id, f"build-{build_id[:8]}", build_status, DOMAIN, kb_id, created_at),
        )
        await conn.execute(
            """INSERT INTO asset_build_document_snapshots
               (build_id, document_id, document_snapshot_id, selection_status, reason)
               VALUES (%s, %s, %s, %s, 'add')""",
            (build_id, document_id, snap_id, selection_status),
        )

        for i in range(segments):
            await conn.execute(
                """INSERT INTO asset_raw_segments
                   (id, document_snapshot_id, segment_key, segment_index, raw_text,
                    normalized_text, content_hash, normalized_hash)
                   VALUES (%s, %s, %s, %s, 't', 't', %s, %s)""",
                (_uid(), snap_id, f"seg-{i}", i, _uid(), _uid()),
            )
        rep_ids: list[str] = []
        for i, unit_type in enumerate(units):
            rep_id = _uid()
            rep_ids.append(rep_id)
            await conn.execute(
                """INSERT INTO asset_retrieval_units_v2
                   (representation_id, snapshot_id, representation_type,
                    content_type, content_text, target_type, target_ref,
                    canonical_evidence_id)
                   VALUES (%s, %s, %s, 'text', 't', 'raw_segment', %s, %s)""",
                (rep_id, snap_id, unit_type, f"seg-{i % segments}" if segments else "seg-0", _uid()),
            )
        for _ in range(embeddings):
            vec = "[" + ",".join(["0.1"] * 1024) + "]"
            await conn.execute(
                """INSERT INTO asset_retrieval_embeddings_v2
                   (embedding_id, snapshot_id, representation_id, strategy,
                    policy_version, provider, model, model_version, dimension,
                    input_hash, embedding_vector_vec)
                   VALUES (%s, %s, %s, 'dense', 'pv-1', 'test', 'm', '1', 1024,
                           %s, %s ::vector)""",
                (_uid(), snap_id, rep_ids[0] if rep_ids else _uid(), _uid(), vec),
            )
    return build_id, snap_id


# ── stats_document_status ───────────────────────────────────────────────────

async def test_document_status_seven_keys_always_present(async_pool):
    """七个键恒存在——调用方（路由与前端）不必判 undefined。

    update_failed 是第 7 态：同一文档重挖失败不回落 failed，单列出来才能解释
    「上次挖成的还在，这次更新坏了」。
    """
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])

    counts = await db.stats_document_status(kb_ids=[kb["id"]])
    assert set(counts) == {
        "uploaded", "mining", "mined", "published", "withdrawn", "failed",
        "update_failed",
    }
    assert all(v == 0 for v in counts.values())


async def test_document_status_buckets_by_run_state(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    docs = {n: await _add_doc(db, kb, n) for n in ("a.md", "b.md", "c.md", "d.md", "e.md")}

    # c.md / e.md 已入库：进过 validated build（36号 §九——mined 的口径是
    # Build 成员资格，不是 run_document committed）
    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=docs["c.md"]["id"])
    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=docs["e.md"]["id"])

    await _insert_run(async_pool, run_id="run-1", kb_id=kb["id"])
    await _insert_run_document(
        async_pool, rd_id="rd-a", run_id="run-1", document_key="doc:/a.md",
        status="failed")
    await _insert_run_document(
        async_pool, rd_id="rd-b", run_id="run-1", document_key="doc:/b.md",
        status="processing")
    await _insert_run_document(
        async_pool, rd_id="rd-c", run_id="run-1", document_key="doc:/c.md",
        status="committed")
    # d.md 没有 run 记录 → uploaded
    # e.md 已入库后最近一次更新失败 → update_failed（Build 旧版本仍在服务，
    # 不回落 failed——这两种失败对用户的含义不同）
    await _insert_run_document(
        async_pool, rd_id="rd-e", run_id="run-1", document_key="doc:/e.md",
        status="failed")

    counts = await db.stats_document_status(kb_ids=[kb["id"]])
    assert counts["failed"] == 1          # a.md
    assert counts["mining"] == 1          # b.md
    assert counts["mined"] == 1           # c.md
    assert counts["uploaded"] == 1        # d.md
    assert counts["update_failed"] == 1   # e.md


async def test_document_status_without_release_never_reports_published(async_pool):
    """with_release=False 时那两档恒 0 —— 这是「口径不适用」，不是数值为零。"""
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    await _add_doc(db, kb, "a.md")

    counts = await db.stats_document_status(kb_ids=[kb["id"]], with_release=False)
    assert counts["published"] == 0
    assert counts["withdrawn"] == 0


async def test_document_status_is_bounded_by_kb_ids(async_pool):
    """别的库的文档不得计进来——这是端点的越权边界。"""
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    mine = await db.create_kb(domain=DOMAIN, name="mine", owner_id=owner["id"])
    theirs = await db.create_kb(domain=DOMAIN, name="theirs", owner_id=owner["id"])
    await _add_doc(db, mine, "a.md")
    await _add_doc(db, theirs, "b.md")
    await _add_doc(db, theirs, "c.md")

    counts = await db.stats_document_status(kb_ids=[mine["id"]])
    assert sum(counts.values()) == 1


async def test_document_status_empty_input_short_circuits(async_pool):
    """空可见集不查库——kb_id = ANY('{}') 在 psycopg 里推断不出元素类型会直接报错。"""
    db = KbDB(async_pool)
    counts = await db.stats_document_status(kb_ids=[])
    assert sum(counts.values()) == 0
    assert len(counts) == 7


# ── stats_assets ────────────────────────────────────────────────────────────

async def test_assets_count_current_snapshot_only(async_pool):
    """**本文件最要紧的一条**：重挖过的文档只算最新那份快照，不是累加。

    同一篇文档挖两遍会有两份快照，各 3 个切片。累计 COUNT 会报 6；正确答案是 3。
    """
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    doc = await _add_doc(db, kb, "a.md")

    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=doc["id"],
        created_at="2026-01-01T00:00:00+00:00", segments=3, units=("raw_text",))
    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=doc["id"],
        created_at="2026-06-01T00:00:00+00:00", segments=3, units=("raw_text",))

    assets = await db.stats_assets(kb_ids=[kb["id"]])
    assert assets["snapshots"] == 1
    assert assets["segments"] == 3
    assert assets["retrieval_units"] == 1
    assert assets["embeddings"] == 0   # 没造向量就是 0，不是键缺失


async def test_assets_count_all_four_kinds(async_pool):
    """2026-09-01 口径：快照/切片/检索单元/向量四类；实体提及与切片关系
    随实体本体线下线移出统计。"""
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    doc = await _add_doc(db, kb, "a.md")

    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=doc["id"],
        segments=4, units=("raw_text", "summary"), embeddings=3)

    assets = await db.stats_assets(kb_ids=[kb["id"]])
    assert assets == {
        "snapshots": 1, "segments": 4, "retrieval_units": 2, "embeddings": 3,
    }


async def test_assets_exclude_withdrawn_and_unvalidated_builds(async_pool):
    """selection_status='removed'（撤回）与 status='building'（没验完）都不算当前知识。"""
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    removed_doc = await _add_doc(db, kb, "removed.md")
    building_doc = await _add_doc(db, kb, "building.md")

    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=removed_doc["id"],
        selection_status="removed", segments=5)
    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=building_doc["id"],
        build_status="building", segments=5)

    assets = await db.stats_assets(kb_ids=[kb["id"]])
    assert assets["snapshots"] == 0
    assert assets["segments"] == 0


async def test_assets_are_bounded_by_kb_ids(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    mine = await db.create_kb(domain=DOMAIN, name="mine", owner_id=owner["id"])
    theirs = await db.create_kb(domain=DOMAIN, name="theirs", owner_id=owner["id"])
    my_doc = await _add_doc(db, mine, "a.md")
    their_doc = await _add_doc(db, theirs, "b.md")

    await _build_snapshot(async_pool, kb_id=mine["id"], document_id=my_doc["id"], segments=2)
    await _build_snapshot(
        async_pool, kb_id=theirs["id"], document_id=their_doc["id"], segments=9)

    assert (await db.stats_assets(kb_ids=[mine["id"]]))["segments"] == 2


async def test_assets_empty_input_short_circuits(async_pool):
    db = KbDB(async_pool)
    assert await db.stats_assets(kb_ids=[]) == {
        "snapshots": 0, "segments": 0, "retrieval_units": 0, "embeddings": 0,
    }


# ── stats_retrieval_unit_types ──────────────────────────────────────────────

async def test_unit_types_grouped_and_scoped_to_current_snapshot(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])
    doc = await _add_doc(db, kb, "a.md")

    await _build_snapshot(
        async_pool, kb_id=kb["id"], document_id=doc["id"], segments=1,
        units=("raw_text", "raw_text", "summary"))

    assert await db.stats_retrieval_unit_types(kb_ids=[kb["id"]]) == {
        "raw_text": 2, "summary": 1,
    }


async def test_unit_types_empty_input_short_circuits(async_pool):
    db = KbDB(async_pool)
    assert await db.stats_retrieval_unit_types(kb_ids=[]) == {}


# ── stats_mining_trend ──────────────────────────────────────────────────────

async def test_trend_fills_empty_days_with_zeros(async_pool):
    """空天必须补零。折线图跳过没有数据的日期，会把「三周没挖」画成持续产出。"""
    db = KbDB(async_pool)
    trend = await db.stats_mining_trend(kb_ids=[], days=7)

    assert len(trend) == 7
    assert all(p["runs"] == 0 and p["documents"] == 0 for p in trend)
    # 升序，且末位是今天
    assert [p["date"] for p in trend] == sorted(p["date"] for p in trend)
    assert trend[-1]["date"] == datetime.now(timezone.utc).date().isoformat()


async def test_trend_counts_runs_and_committed_documents(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])

    today = datetime.now(timezone.utc).date()
    stamp = f"{today.isoformat()}T03:00:00+00:00"
    await _insert_run(
        async_pool, run_id="r1", kb_id=kb["id"], started_at=stamp, committed=4)
    await _insert_run(
        async_pool, run_id="r2", kb_id=kb["id"], started_at=stamp,
        status="failed", committed=1)

    trend = await db.stats_mining_trend(kb_ids=[kb["id"]], days=7)
    point = next(p for p in trend if p["date"] == today.isoformat())

    assert point["runs"] == 2
    assert point["completed"] == 1      # 只有 r1 是 completed
    assert point["documents"] == 5


async def test_trend_ignores_runs_outside_the_window(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    kb = await db.create_kb(domain=DOMAIN, name="K", owner_id=owner["id"])

    old = (datetime.now(timezone.utc).date() - timedelta(days=90)).isoformat()
    await _insert_run(
        async_pool, run_id="ancient", kb_id=kb["id"],
        started_at=f"{old}T00:00:00+00:00", committed=99)

    trend = await db.stats_mining_trend(kb_ids=[kb["id"]], days=7)
    assert sum(p["documents"] for p in trend) == 0


async def test_trend_is_bounded_by_kb_ids(async_pool):
    db = KbDB(async_pool)
    owner = await db.upsert_user_by_username("alice")
    mine = await db.create_kb(domain=DOMAIN, name="mine", owner_id=owner["id"])
    theirs = await db.create_kb(domain=DOMAIN, name="theirs", owner_id=owner["id"])

    stamp = f"{datetime.now(timezone.utc).date().isoformat()}T03:00:00+00:00"
    await _insert_run(
        async_pool, run_id="r-theirs", kb_id=theirs["id"], started_at=stamp, committed=7)

    trend = await db.stats_mining_trend(kb_ids=[mine["id"]], days=7)
    assert sum(p["documents"] for p in trend) == 0
