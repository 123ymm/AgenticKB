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
  版（latest：每文档最近 validated/published build 快照 → cur：active）；
  **scope 感知（审查裁定）**：快照集恒为 kb 全量当前快照，unit_count 按
  绑定 directories 过滤（_DIR_PATH_LATERAL+_DIR_FILTER——空数组=全库）。
  语义 dirty=绑定范围内编译语料发生变化；该查询同时是编译 start 的量级
  护栏数据源（Σ unit_count，批次3 决策2）。
- kb 可读判定不另起谓词：组合 KbDB.is_visible（谓词唯一真相源——site admin
  全通 / 域绑定 + 域内 public / owner / 成员分支一个不少；防第三次判定不对
  称坑）。不可读由 service 层统一翻 404 反探测。
"""
from __future__ import annotations

import json
import logging
import uuid
from array import array
from datetime import datetime, timezone
from typing import Any

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_wiki import WikiError

logger = logging.getLogger(__name__)

#: 单 kb 版「当前知识」CTE——latest 段与 kb/db.KbDB._CURRENT_SNAPSHOT_CTE
#: 同构（``ANY(%(kb)s)`` 收窄为标量 ``%(kb)s``）；cur 段在单 kb 版加
#: **DISTINCT**（审查 I-3）：同 kb 两文档可共享同一快照（同内容 hash，
#: projector 一等支持路径），DISTINCT ON document_id 的 latest 会对同一
#: document_snapshot_id 产出多行——去重后 units JOIN 才不把共享快照单元
#: 翻倍（指纹计数/Σ 护栏/取材同源受益）。多 kb 版（KbDB）**不能**照搬：
#: 跨 kb 语义不同（每 kb 各自视角），保持原样。
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
    SELECT DISTINCT document_snapshot_id
    FROM latest
    WHERE selection_status = 'active'
)"""

#: 单元目录归属 LATERAL（**T2 collect_units 复用同一形态**）：unit.snapshot_id
#: → 所属文档的 directory_path（asset_document_snapshot_links⋈asset_documents）。
#: 快照可被多文档共享（同内容多文档，见 projector.py representation_id 锚定
#: 注释），ORDER BY d.id LIMIT 1 取确定性归属；快照无 link 行（孤儿，理论
#: 不可达）时 LATERAL 无行——目录过滤下该单元不计（无法证明在 scope 内）。
_DIR_PATH_LATERAL = """
    SELECT d.directory_path
    FROM asset_document_snapshot_links l
    JOIN asset_documents d ON d.id = l.document_id
    WHERE l.document_snapshot_id = u.snapshot_id
    ORDER BY d.id
    LIMIT 1
"""

#: scope 目录过滤谓词（directories 非空时拼入；空数组=全库不拼——**T2
#: collect_units 复用同一形态**）：目录精确命中或为其子目录（strpos=1 前缀
#: 判定而非 LIKE 'dir/%'——无 LIKE 元字符 %/_ 转义面，用户目录路径可含任意
#: 字符）。
_DIR_FILTER = """(
        dp.directory_path = ANY(%(dirs)s::text[])
        OR EXISTS (SELECT 1 FROM unnest(%(dirs)s::text[]) AS p(dir)
                   WHERE strpos(dp.directory_path, dir || '/') = 1)
    )"""

#: collect_units fetchmany 流式批大小（决策2 ③：分批物化防峰值）
_FETCH_BATCH = 2000


def decode_embedding_text(text: str | None) -> array | None:
    """pgvector 文本形态 ``"[0.1,0.2,...]"`` → ``array('f')``（决策2 ②：
    4KB/条而非 list[float] ~32KB/条——30k 单元护栏内的内存账）。None 直传
    （LEFT JOIN 无向量单元）。"""
    if text is None:
        return None
    return array("f", map(float, text.strip("[]").split(",")))


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

    @staticmethod
    def _norm_dirs(directories: list[str] | None) -> list[str]:
        """scope 目录归一：strip / 去空 / 去重排序（排序保幂等查询计划稳定）。"""
        return sorted({str(d).strip() for d in (directories or [])
                       if str(d).strip()})

    async def current_fingerprint(
        self, kb_id: str, directories: list[str],
    ) -> dict[str, Any]:
        """kb 当前语料指纹（批次3 决策6 契约形状，**scope 感知——T2 契约就此
        定死**）：``{"snapshots": [{"snapshot_id", "unit_count"}],
        "computed_at": iso}``。

        语义：**dirty = 绑定范围内编译语料发生变化**——快照集恒走
        _CURRENT_SNAPSHOT_CTE（kb 全量当前快照，含 scope 外的零计数行——
        dirty 对比两侧同形）；unit_count 按 directories 过滤（精确命中或子
        目录前缀，_DIR_PATH_LATERAL+_DIR_FILTER；空数组=全库不过滤）。
        编译侧（T3 _run_prepare）落库指纹必须用**同参调用**的结果——两侧
        同形，scope 外的库变化不误脏、scope 内变化必脏。
        kb 无就绪快照 → snapshots=[]。"""
        dirs = self._norm_dirs(directories)
        params: dict[str, Any] = {"kb": kb_id}
        if dirs:
            params["dirs"] = dirs
            # 标量子查询按 cur 行逐快照求值（idx_aru_v2_snapshot 收敛扫描面）；
            # JOIN LATERAL 内联——scope 外单元不计，快照无 scope 内单元得 0
            count_sql = f"""(
                    SELECT COUNT(*)
                    FROM asset_retrieval_units_v2 u
                    JOIN LATERAL ({_DIR_PATH_LATERAL}) dp ON TRUE
                    WHERE u.snapshot_id = cur.document_snapshot_id
                      AND {_DIR_FILTER})"""
        else:
            count_sql = """(
                    SELECT COUNT(*)
                    FROM asset_retrieval_units_v2 u
                    WHERE u.snapshot_id = cur.document_snapshot_id)"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""{_CURRENT_SNAPSHOT_CTE}
                    SELECT cur.document_snapshot_id AS snapshot_id,
                           {count_sql} AS unit_count
                    FROM cur
                    ORDER BY cur.document_snapshot_id""",
                params,
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
        """Σ 当前快照检索单元数——**全库口径**（编译 start 量级护栏的兜底
        数据源，批次3 决策2；scope 口径走 current_fingerprint(kb, dirs) 的
        Σ unit_count——start 护栏按各源 scope 求和更准）。"""
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

    # ------------------------------------------------------------ 编译取材

    async def collect_units(
        self, kb_id: str, directories: list[str],
    ) -> list[dict[str, Any]]:
        """编译取材（63号 §4.2 ①，Task 3 `_run_prepare` 唯一调用方）：kb
        当前快照范围内检索单元全量行。

        口径与 current_fingerprint **同源**（同一 _CURRENT_SNAPSHOT_CTE
        快照集——含共享快照 DISTINCT 去重；同一 _DIR_PATH_LATERAL/
        _DIR_FILTER scope 谓词），两处刻意差异（审查 M-2 修正原「完全同构」
        夸大表述）：
        ① 孤儿单元（快照无 link 行，理论不可达）：指纹的**空目录**分支不
        JOIN LATERAL（计数含孤儿），本方法恒 INNER——孤儿无目录归属、无法
        证明在 scope 内，**保守丢弃**（取材宁少勿错）；
        ② 指纹按快照计数，本方法展开到单元行。
        dirty 判定两侧同走 current_fingerprint，不受本方法差异影响。

        LEFT JOIN asset_retrieval_embeddings_v2（**LEFT** 保住无向量单元——
        聚类侧旁路到未聚类池，取材完整）；ORDER BY snapshot_id, ordinal,
        representation_id——**确定性铁律**（聚类种子取首遇，输入序固定输出
        才稳定）。

        每行：{representation_id, snapshot_id, content_text, structural_context,
        section_ref（015 迁移 ALTER 补列，现库恒在）, ordinal, dense_eligible,
        directory_path, embedding(array('f')|None), **kb_id**（Python 侧回填
        ——多源合并时归属账，决策6 provenance 的 (kb_id, snapshot_id) 分组键）}。
        向量解码 decode_embedding_text（array('f')，决策2 ②）。

        量级：cursor.fetchmany(_FETCH_BATCH) 流式聚合（决策2 ③）；最终仍全量
        进内存——护栏在编译 start（Σ unit_count > 30000 → 422），本方法信任
        调用方。
        """
        dirs = self._norm_dirs(directories)
        params: dict[str, Any] = {"kb": kb_id}
        where = ""
        if dirs:
            params["dirs"] = dirs
            where = f"WHERE {_DIR_FILTER}"
        rows: list[dict[str, Any]] = []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""{_CURRENT_SNAPSHOT_CTE}
                    SELECT u.representation_id, u.snapshot_id, u.content_text,
                           u.structural_context, u.section_ref, u.ordinal,
                           u.dense_eligible, dp.directory_path,
                           e.embedding_vector_vec::text AS embedding_text
                    FROM asset_retrieval_units_v2 u
                    JOIN cur ON cur.document_snapshot_id = u.snapshot_id
                    JOIN LATERAL ({_DIR_PATH_LATERAL}) dp ON TRUE
                    LEFT JOIN asset_retrieval_embeddings_v2 e
                           ON e.representation_id = u.representation_id
                          AND e.snapshot_id = u.snapshot_id
                          AND e.dimension = 1024
                    {where}
                    ORDER BY u.snapshot_id, u.ordinal, u.representation_id""",
                params,
            )
            while True:
                batch = await cur.fetchmany(_FETCH_BATCH)
                if not batch:
                    break
                for r in batch:
                    d = dict(r)
                    d["kb_id"] = kb_id
                    d["embedding"] = decode_embedding_text(
                        d.pop("embedding_text"))
                    rows.append(d)
        return rows

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
