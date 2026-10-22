"""Wiki 制品编译取材域（63号批次3）—— kb_artifact_sources 仓储 + 编译批次
details 读写 + kb 可读判定组合。

批次3 Task 1 先落 sources CRUD / 指纹观测 / batch-details；Task 2 再加
collect_units 取材。风格契约与 db_wiki.py / db_artifacts.py 一致：TEXT id、
参数化 SQL、dict_row、多语句显式 ``conn.transaction()``、每方法自开连接
（一个事务）。错误统一 WikiError（routes/wiki._map_wiki_error 唯一翻译点）。

关键裁定（批次3计划 Task 1）：
- delete_source 软删 status='removed'（非物理 DELETE）：绑定历史/指纹保留
  供复活；UNIQUE(artifact_id, kb_id) 不会被软删行挡住复活（复活=UPDATE 回
  active，不重新 INSERT，无主键冲突面）；list_sources 只回 active 行；硬删
  只在制品级联（db_artifacts.delete_artifact 已含 sources 表）。
- upsert_source 旧行 status='removed' → 复活 'active'（重新绑定同一库）；
  fingerprint 保留——它是「上次编译语料」的账，KB 在解绑期间变化时 dirty
  对比（service 层）自然判脏，不需清零。
- current_fingerprint 口径 = kb/db.KbDB._CURRENT_SNAPSHOT_CTE 的单 kb 同构
  版（latest：每文档最近 validated/published build 快照 → cur：active），每
  快照一次 COUNT 聚合（LEFT JOIN 保零单元快照成行）；该查询同时是编译 start
  的量级护栏数据源（Σ unit_count，批次3 决策2）。
- kb 可读判定不另起谓词：组合 KbDB.is_visible（谓词唯一真相源——site admin
  全通 / 域绑定 + 域内 public / owner / 成员分支一个不少；防第三次判定不对
  称坑）。不可读由 service 层统一翻 404 反探测。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_wiki import WikiError

logger = logging.getLogger(__name__)

#: 单 kb 版「当前知识」CTE——与 kb/db.KbDB._CURRENT_SNAPSHOT_CTE 同构：
#: ``ANY(%(kb)s)`` 收窄为标量 ``%(kb)s``，latest→active 两段口径逐字保留。
_CURRENT_SNAPSHOT_CTE = """
WITH latest AS (
    SELECT DISTINCT ON (bs.document_id)
           bs.document_snapshot_id, bs.selection_status
    FROM asset_build_document_snapshots bs
    JOIN asset_builds b ON b.id = bs.build_id
    JOIN asset_documents d ON d.id = bs.document_id
    WHERE b.kb_id = %(kb)s
      AND b.kb_id = d.kb_id AND d.deleted_at IS NULL
      AND b.status IN ('validated', 'published')
    ORDER BY bs.document_id, b.created_at DESC, b.id DESC
), cur AS (
    SELECT document_snapshot_id
    FROM latest
    WHERE selection_status = 'active'
)"""


def _json(obj: Any) -> str:
    if obj is None:
        return "null"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _new_source_id() -> str:
    """src_ 前缀 + hex（对齐 art_/bat_ 前缀惯例）。"""
    return f"src_{uuid.uuid4().hex}"


class CompileInputsDB:
    """kb_artifact_sources 仓储 + kb_artifact_batches(details) 编译域读写
    + kb 可读/标题组合查询。

    构造入参为 row_factory=dict_row 的 psycopg AsyncConnectionPool。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        # 同池 KbDB——kb_readable 组合 is_visible（谓词唯一真相源，见模块
        # docstring；KbDB 构造无副作用，只是持 pool 引用）。
        self._kbdb = KbDB(pool)

    # ------------------------------------------------------------ 绑定 CRUD

    async def list_sources(self, artifact_id: str) -> list[dict[str, Any]]:
        """active 绑定行（软删行不可见）；ORDER BY created_at, kb_id。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, artifact_id, kb_id, scope, fingerprint, status,
                          created_by, created_at
                   FROM kb_artifact_sources
                   WHERE artifact_id = %s AND status = 'active'
                   ORDER BY created_at, kb_id""",
                (artifact_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_source(
        self, artifact_id: str, kb_id: str, scope: dict[str, Any],
        created_by: str, *, expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """绑定/更新：ON CONFLICT (artifact_id, kb_id) DO UPDATE scope；旧行
        status='removed' → 复活 'active'（裁定见模块 docstring）。
        expected_updated_at 权威比对（FOR UPDATE 全精度）+ touch kb_artifacts
        同事务（照 wdb.upsert_page 模式——绑定变更须失效编辑器令牌）。
        返回绑定行。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._assert_expected_updated_at(
                    conn, artifact_id, expected_updated_at)
                cur = await conn.execute(
                    """INSERT INTO kb_artifact_sources
                       (id, artifact_id, kb_id, scope, fingerprint, status,
                        created_by)
                       VALUES (%(id)s, %(a)s, %(k)s, %(scope)s::jsonb,
                               '{}'::jsonb, 'active', %(u)s)
                       ON CONFLICT (artifact_id, kb_id) DO UPDATE SET
                           scope = EXCLUDED.scope,
                           status = 'active',
                           created_by = EXCLUDED.created_by
                       RETURNING id, artifact_id, kb_id, scope, fingerprint,
                                 status, created_by, created_at""",
                    {"id": _new_source_id(), "a": artifact_id, "k": kb_id,
                     "scope": _json(scope or {}), "u": created_by},
                )
                row = dict(await cur.fetchone())  # type: ignore[arg-type]
                await self._touch_in_conn(conn, artifact_id)
                return row

    async def delete_source(
        self, artifact_id: str, kb_id: str, *, expected_updated_at: Any = None,
    ) -> bool:
        """软删（裁定见模块 docstring）：UPDATE status='removed'。无 active 行
        → False（service 翻 source_not_found）。权威比对+touch 同事务。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._assert_expected_updated_at(
                    conn, artifact_id, expected_updated_at)
                cur = await conn.execute(
                    """UPDATE kb_artifact_sources
                       SET status = 'removed'
                       WHERE artifact_id = %s AND kb_id = %s
                         AND status = 'active'
                       RETURNING id""",
                    (artifact_id, kb_id),
                )
                hit = await cur.fetchone() is not None
                if hit:
                    await self._touch_in_conn(conn, artifact_id)
                return hit

    async def update_fingerprint(
        self, artifact_id: str, kb_id: str, fingerprint: dict[str, Any],
    ) -> bool:
        """编译管线写「本次取材语料指纹」（批次3 _run_prepare 调用）。不
        touch——观测数据非用户可见变更（编译的页写自己会推进令牌）。绑定行
        不存在（含软删态）→ False。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_artifact_sources
                   SET fingerprint = %(fp)s::jsonb
                   WHERE artifact_id = %(a)s AND kb_id = %(k)s""",
                {"fp": _json(fingerprint), "a": artifact_id, "k": kb_id},
            )
            return int(cur.rowcount or 0) > 0

    # ------------------------------------------------------------ 指纹观测

    async def current_fingerprint(self, kb_id: str) -> dict[str, Any]:
        """kb 当前语料指纹（批次3 决策6 契约形状）：
        ``{"snapshots": [{"snapshot_id", "unit_count"}], "computed_at": iso}``。
        口径见模块 docstring（单 kb 当前快照 CTE + 每快照一次 COUNT 聚合，
        LEFT JOIN 保零单元快照成行）；kb 无就绪快照 → snapshots=[]。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""{_CURRENT_SNAPSHOT_CTE}
                    SELECT cur.document_snapshot_id AS snapshot_id,
                           COUNT(u.representation_id) AS unit_count
                    FROM cur
                    LEFT JOIN asset_retrieval_units_v2 u
                           ON u.snapshot_id = cur.document_snapshot_id
                    GROUP BY cur.document_snapshot_id
                    ORDER BY cur.document_snapshot_id""",
                {"kb": kb_id},
            )
            rows = [dict(r) for r in await cur.fetchall()]
        return {
            "snapshots": [
                {"snapshot_id": str(r["snapshot_id"]),
                 "unit_count": int(r["unit_count"])}
                for r in rows
            ],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def kb_snapshot_unit_count(self, kb_id: str) -> int:
        """Σ 当前快照检索单元数（编译 start 量级护栏数据源，批次3 决策2）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""{_CURRENT_SNAPSHOT_CTE}
                    SELECT COUNT(u.representation_id) AS n
                    FROM cur
                    LEFT JOIN asset_retrieval_units_v2 u
                           ON u.snapshot_id = cur.document_snapshot_id""",
                {"kb": kb_id},
            )
            return int((await cur.fetchone())["n"])  # type: ignore[index]

    # ------------------------------------------------------------ kb 元数据

    async def kb_readable(self, kb_id: str, user_id: str) -> bool:
        """kb 可读判定：组合 KbDB.is_visible（谓词唯一真相源，见模块
        docstring）。不可读一律由 service 层翻 404 反探测。"""
        return await self._kbdb.is_visible(kb_id=kb_id, user_id=user_id)

    async def kb_titles(self, kb_ids: list[str]) -> dict[str, str]:
        """kb 标题批量联查（list_sources 的 kb_title 附带；一次查询防 N+1）。"""
        if not kb_ids:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, name FROM knowledge_bases WHERE id = ANY(%s)",
                (kb_ids,),
            )
            return {str(r["id"]): str(r["name"]) for r in await cur.fetchall()}

    # ------------------------------------------------------------ 编译批次

    async def update_batch_details(
        self, batch_id: str, patch: dict[str, Any],
    ) -> bool:
        """编译状态机增量合并写（批次3 决策1 铁律）：details || patch——后台
        任务 / confirm_toc / cancel 并发写者只动自己的键，禁全量覆盖（COALESCE
        兜底迁移库 details 为 NULL 的旧行）。批次不存在 → False。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_artifact_batches
                   SET details = COALESCE(details, '{}'::jsonb) || %(patch)s::jsonb
                   WHERE batch_id = %s""",
                (_json(patch), batch_id),
            )
            return int(cur.rowcount or 0) > 0

    async def latest_compile_batch(
        self, artifact_id: str,
    ) -> dict[str, Any] | None:
        """最新 wiki_compile 批次（**含 details 列**——db_artifacts.list_batches
        不含 details，本方法是编译域专用读，status 端点数据源；batch_id 兜底
        序保证同秒多批确定性）。无 → None。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT batch_id, artifact_id, channel, file_name, sheet_name,
                          row_count, created_by, created_at, details
                   FROM kb_artifact_batches
                   WHERE artifact_id = %s AND channel = 'wiki_compile'
                   ORDER BY created_at DESC, batch_id DESC
                   LIMIT 1""",
                (artifact_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------ 内部共用

    async def _assert_expected_updated_at(
        self, conn: Any, artifact_id: str, expected_updated_at: Any,
    ) -> None:
        """写路径乐观锁权威校验（口径同 db_wiki / db_artifacts 同名方法）：
        FOR UPDATE 锁 kb_artifacts 行 → 全精度比对 updated_at（PG TIMESTAMPTZ
        微秒）→ 失配 stale_artifact / 制品不存在 artifact_not_found。None
        跳过（校验是否前置由 service 决定）。"""
        if expected_updated_at is None:
            return
        cur = await conn.execute(
            "SELECT updated_at FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
            (artifact_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise WikiError("artifact_not_found")
        if row["updated_at"] != expected_updated_at:
            raise WikiError("stale_artifact")

    @staticmethod
    async def _touch_in_conn(conn: Any, artifact_id: str) -> None:
        await conn.execute(
            "UPDATE kb_artifacts SET updated_at = now() WHERE artifact_id = %s",
            (artifact_id,),
        )
