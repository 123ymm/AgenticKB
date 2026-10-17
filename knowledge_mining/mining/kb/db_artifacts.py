"""KB 知识制品（61号）—— kb_artifacts 六表异步仓储。

风格契约与 mining/kb/db.py 一致：TEXT id、参数化 SQL、dict_row、
多语句显式 ``conn.transaction()``、每方法自开连接（一个事务）。

机制权威 = docs/下一阶段/61号 §四（发布事务 / ensure_draft / diff）：
- 行地址 row_key = 业务主键（ordinal 仅展示）；jsonb 三字段分工：
  data=业务值 / provenance=来源账（数组） / conflicts=质量标记，永不混装。
- 发布事务三件事原子：rev0→revN 复制定格 + schema 快照 + 清 draft 拨指针；
  类型化表达式索引在事务成功后重建（失败不回滚发布本身，下次发布自愈）。
- ensure_draft 用 kb_artifacts.draft_open 显式状态（FALSE 且 current>0 才
  checkout 复制），避免「检出后删光」被误判需要复活。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from psycopg import sql

logger = logging.getLogger(__name__)

#: 单批多行 INSERT 的行数（10 万行上限 ÷ 1000/批；参数量 8×1000 远低于 65535）。
_ROW_INSERT_BATCH = 1000

#: list_rows 的页大小护栏。
_MAX_PAGE_SIZE = 500

#: diff 样例截断（counts 恒全量，added/removed/changed 各只回前 N 条）。
_DIFF_SAMPLE_LIMIT = 100

_VALID_KINDS = {"table", "wiki"}
_VALID_MEMBER_ROLES = {"owner", "editor", "viewer"}
_VALID_COLUMN_TYPES = {"text", "int", "float", "date", "enum", "bool"}

#: batch_assign / 行过滤允许的操作词表（列名恒为参数，无拼接注入面）。
_VALID_FILTER_OPS = {"eq", "ne", "in", "contains", "is_null", "not_null"}


def _new_artifact_id() -> str:
    """art_ 前缀 + hex——serving/MCP 侧按前缀解析 ref（61号 Task 7 契约）。"""
    return f"art_{uuid.uuid4().hex}"


def _json(obj: Any) -> str:
    if obj is None:
        return "null"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _row_key_value(data: dict[str, Any], column_ids: list[str]) -> str:
    """主键值拼接：None→空串；多列 '/' 连接；无主键由调用方生成 uuid。"""
    if not column_ids:
        return uuid.uuid4().hex
    return "/".join(
        "" if data.get(c) is None else str(data.get(c)) for c in column_ids
    )


class ArtifactError(RuntimeError):
    """仓储层不变量冲突（发布门禁 / 主键冲突 / 非法状态）。service 负责翻译。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ArtifactDB:
    """kb_artifacts / kb_artifact_revisions / kb_artifact_members /
    kb_artifact_batches / kb_table_columns / kb_table_rows 仓储。

    构造入参为 row_factory=dict_row 的 psycopg AsyncConnectionPool。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------ 制品登记

    async def create_artifact(
        self,
        *,
        artifact_id: str | None = None,
        domain_id: str,
        kind: str = "table",
        title: str,
        description: str = "",
        owner_user_id: str,
    ) -> dict[str, Any]:
        if kind not in _VALID_KINDS:
            raise ArtifactError("invalid_kind")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """INSERT INTO kb_artifacts
                       (artifact_id, domain_id, kind, title, description,
                        owner_user_id, status, current_revision, draft_open)
                       VALUES (%(id)s, %(dom)s, %(kind)s, %(title)s, %(desc)s,
                               %(owner)s, 'draft', 0, FALSE)
                       RETURNING artifact_id, domain_id, kind, title, description,
                                 usage_note, owner_user_id, status, current_revision,
                                 draft_open, created_at, updated_at""",
                    {
                        "id": artifact_id or _new_artifact_id(),
                        "dom": domain_id, "kind": kind, "title": title,
                        "desc": description, "owner": owner_user_id,
                    },
                )
                row = dict(await cur.fetchone())  # type: ignore[arg-type]
                await conn.execute(
                    """INSERT INTO kb_artifact_members (artifact_id, user_id, role)
                       VALUES (%s, %s, 'owner')""",
                    (row["artifact_id"], owner_user_id),
                )
                return row

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT artifact_id, domain_id, kind, title, description,
                          usage_note, owner_user_id, status, current_revision,
                          draft_open, created_at, updated_at
                   FROM kb_artifacts WHERE artifact_id = %s""",
                (artifact_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_artifact_meta(
        self,
        artifact_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        usage_note: str | None = None,
    ) -> dict[str, Any] | None:
        """PATCH 语义：None=未提供不动（显式清空传空串）。恒 bump updated_at。"""
        sets: list[str] = []
        params: dict[str, Any] = {"id": artifact_id}
        if title is not None:
            params["t"] = title
            sets.append("title = %(t)s")
        if description is not None:
            params["d"] = description
            sets.append("description = %(d)s")
        if usage_note is not None:
            params["u"] = usage_note
            sets.append("usage_note = %(u)s")
        if not sets:
            return await self.get_artifact(artifact_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE kb_artifacts SET " + ", ".join(sets)
                + ", updated_at = now() WHERE artifact_id = %(id)s"
                " RETURNING artifact_id, domain_id, kind, title, description,"
                " usage_note, owner_user_id, status, current_revision, draft_open,"
                " created_at, updated_at",
                params,
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def touch_updated_at(self, artifact_id: str) -> None:
        """设计态编辑后的乐观锁令牌推进（updated_at 即 ETag 源）。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE kb_artifacts SET updated_at = now() WHERE artifact_id = %s",
                (artifact_id,),
            )

    async def delete_artifact(self, artifact_id: str) -> None:
        """owner 硬删：显式级联（列约定无 FK，删除顺序自底向上）。级联后清
        本制品动态表达式索引（对抗审查修复⑤：idx_kbtr_{art}_* 不随行表清空）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kb_table_rows WHERE artifact_id = %s",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_table_columns WHERE artifact_id = %s",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_artifact_batches WHERE artifact_id = %s",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_artifact_members WHERE artifact_id = %s",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_artifact_revisions WHERE artifact_id = %s",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_artifacts WHERE artifact_id = %s",
                    (artifact_id,),
                )
        await self.drop_draft_expression_indexes(artifact_id)

    async def list_artifacts_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """成员视角列表（含 owner 不落 members 行的兜底）；site/domain admin 全量
        走 list_all_artifacts（service 层分派）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT a.artifact_id, a.domain_id, a.kind, a.title, a.description,
                          a.usage_note, a.owner_user_id, a.status, a.current_revision,
                          a.draft_open, a.created_at, a.updated_at,
                          CASE WHEN a.owner_user_id = %(u)s THEN 'owner'
                               ELSE m.role END AS my_role
                   FROM kb_artifacts a
                   LEFT JOIN kb_artifact_members m
                          ON m.artifact_id = a.artifact_id AND m.user_id = %(u)s
                   WHERE a.owner_user_id = %(u)s OR m.user_id IS NOT NULL
                   ORDER BY a.created_at""",
                {"u": user_id},
            )
            return [dict(r) for r in await cur.fetchall()]

    async def list_all_artifacts(
        self, domain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """admin 全量列表（可选按域收窄）。"""
        clause, params = "", []
        if domain_id is not None:
            clause = " WHERE a.domain_id = %s"
            params.append(domain_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT a.artifact_id, a.domain_id, a.kind, a.title, a.description,
                          a.usage_note, a.owner_user_id, a.status, a.current_revision,
                          a.draft_open, a.created_at, a.updated_at
                   FROM kb_artifacts a""" + clause + " ORDER BY a.created_at",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    # ---------------------------------------------------------------- 成员

    async def list_members(self, artifact_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT m.user_id, m.role, u.username,
                          COALESCE(NULLIF(u.display_name, ''), u.username) AS user_name
                   FROM kb_artifact_members m
                   LEFT JOIN kb_users u ON u.id = m.user_id
                   WHERE m.artifact_id = %s
                   ORDER BY m.role, u.username""",
                (artifact_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_member(
        self, artifact_id: str, user_id: str, role: str,
    ) -> dict[str, Any]:
        if role not in _VALID_MEMBER_ROLES:
            raise ArtifactError("invalid_member_role")
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_artifact_members (artifact_id, user_id, role)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (artifact_id, user_id) DO UPDATE SET role = EXCLUDED.role
                   RETURNING artifact_id, user_id, role""",
                (artifact_id, user_id, role),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def remove_member(self, artifact_id: str, user_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM kb_artifact_members WHERE artifact_id = %s AND user_id = %s",
                (artifact_id, user_id),
            )

    async def artifact_role(self, artifact_id: str, user_id: str) -> str | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT role FROM kb_artifact_members"
                " WHERE artifact_id = %s AND user_id = %s",
                (artifact_id, user_id),
            )
            row = await cur.fetchone()
            return str(row["role"]) if row else None

    async def get_user_status(self, user_id: str) -> str | None:
        """成员资格预检（对抗审查修复⑪）：kb_users 行存在且未软删 → status；
        不存在/已软删 → None。是否 active 由 service 判定。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status FROM kb_users WHERE id = %s AND deleted_at IS NULL",
                (user_id,),
            )
            row = await cur.fetchone()
            return str(row["status"]) if row else None

    async def is_domain_admin(self, user_id: str, domain_id: str) -> bool:
        """口径同 kb/db.py can_manage_domain：active site admin 全通 / 本域
        domain_role='admin'。以库为准（不信 user 对象缓存的域列表）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM kb_users u
                   WHERE u.id = %(uid)s AND u.status = 'active'
                     AND u.deleted_at IS NULL
                     AND (u.site_role = 'admin'
                          OR EXISTS (SELECT 1 FROM user_domains ud
                                     WHERE ud.user_id = u.id
                                       AND ud.domain = %(dom)s
                                       AND ud.domain_role = 'admin'))""",
                {"uid": user_id, "dom": domain_id},
            )
            return await cur.fetchone() is not None

    async def can_create_in_domain(self, user_id: str, domain_id: str) -> bool:
        """创建资格（61号创建域门禁）：active site admin 全通；其余须有该域
        user_domains 绑定（member / domain admin 任一即放行）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM kb_users u
                   WHERE u.id = %(uid)s AND u.status = 'active'
                     AND u.deleted_at IS NULL
                     AND (u.site_role = 'admin'
                          OR EXISTS (SELECT 1 FROM user_domains ud
                                     WHERE ud.user_id = u.id
                                       AND ud.domain = %(dom)s))""",
                {"uid": user_id, "dom": domain_id},
            )
            return await cur.fetchone() is not None

    async def first_domain_for_user(self, user_id: str) -> str | None:
        """用户首个绑定域（创建向导 domain_id 缺省回落；确定性=字典序）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT ud.domain FROM user_domains ud
                   JOIN kb_users u ON u.id = ud.user_id
                   WHERE ud.user_id = %s AND u.status = 'active'
                     AND u.deleted_at IS NULL
                   ORDER BY ud.domain LIMIT 1""",
                (user_id,),
            )
            row = await cur.fetchone()
            return str(row["domain"]) if row else None

    # ------------------------------------------------------------ 生命周期

    async def publish(
        self, artifact_id: str, change_summary: str, published_by: str,
    ) -> dict[str, Any]:
        """发布事务（61号 §4.2）：①rev0→revN 复制定格 ②schema 快照+版本记录
        ③清 draft+拨指针+状态。usage_note 非空由 service 先拦、此处兜底（竞态安全）。

        draft 门禁（对抗审查修复③）：draft_open=FALSE 且 current>0 = 干净态，
        无待发布变更 → no_pending_changes（service 翻 409）。首次发布
        （current=0）不受此门禁——rev0 即首发工作区。

        new_rev 取 max(current, 历史最大 revision)+1——回滚拨低指针后重发布
        不得与既有版本行撞主键（「数据不动」语义下历史版本仍留在表里）。
        返回 {"revision", "row_count"}；索引重建在事务外自愈式执行。
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise ArtifactError("artifact_not_found")
                if art["status"] not in ("draft", "published", "deprecated"):
                    raise ArtifactError("invalid_status")
                if not (art["usage_note"] or "").strip():
                    raise ArtifactError("usage_note_required")
                if not art["draft_open"] and int(art["current_revision"]) > 0:
                    raise ArtifactError("no_pending_changes")
                cur = await conn.execute(
                    """SELECT GREATEST(
                           %(cur)s,
                           COALESCE((SELECT MAX(revision) FROM kb_artifact_revisions
                                     WHERE artifact_id = %(a)s), 0),
                           COALESCE((SELECT MAX(revision) FROM kb_table_rows
                                     WHERE artifact_id = %(a)s AND revision > 0), 0)
                       ) + 1 AS new_rev""",
                    {"cur": art["current_revision"], "a": artifact_id},
                )
                new_rev = int((await cur.fetchone())["new_rev"])  # type: ignore[index]
                cur = await conn.execute(
                    """SELECT column_id, column_name, column_type, enum_values,
                              is_primary_key, is_indexed, ordinal, alias
                       FROM kb_table_columns
                       WHERE artifact_id = %s AND status = 'active'
                       ORDER BY ordinal, column_id""",
                    (artifact_id,),
                )
                columns = [dict(r) for r in await cur.fetchall()]
                cur = await conn.execute(
                    """INSERT INTO kb_table_rows
                       (artifact_id, revision, row_key, data, provenance, conflicts,
                        ordinal, updated_at, updated_by)
                       SELECT artifact_id, %(r)s, row_key, data, provenance,
                              conflicts, ordinal, now(), %(by)s
                       FROM kb_table_rows
                       WHERE artifact_id = %(a)s AND revision = 0""",
                    {"r": new_rev, "by": published_by, "a": artifact_id},
                )
                # INSERT...SELECT 无 RETURNING：rowcount 才是行数（fetchval 拿不到）
                row_count = int(cur.rowcount or 0)
                await conn.execute(
                    """INSERT INTO kb_artifact_revisions
                       (artifact_id, revision, schema_json, row_count,
                        change_summary, published_by)
                       VALUES (%s, %s, %s::jsonb, %s, %s, %s)""",
                    (artifact_id, new_rev, _json(columns), row_count,
                     change_summary, published_by),
                )
                await conn.execute(
                    "DELETE FROM kb_table_rows WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                await conn.execute(
                    """UPDATE kb_artifacts
                       SET current_revision = %s, status = 'published',
                           draft_open = FALSE, updated_at = now()
                       WHERE artifact_id = %s""",
                    (new_rev, artifact_id),
                )
        # 事务成功后（新连接）：重建类型化索引。draft 不建索引（编辑频繁）。
        await self.drop_draft_expression_indexes(artifact_id)
        indexed = [c for c in columns if c.get("is_indexed")]
        await self.create_expression_indexes(artifact_id, new_rev, indexed)
        return {"revision": new_rev, "row_count": row_count}

    async def get_revision(
        self, artifact_id: str, revision: int,
    ) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT artifact_id, revision, schema_json, row_count,
                          change_summary, published_by, published_at
                   FROM kb_artifact_revisions
                   WHERE artifact_id = %s AND revision = %s""",
                (artifact_id, revision),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_revisions(self, artifact_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT revision, row_count, change_summary, published_by,
                          published_at
                   FROM kb_artifact_revisions
                   WHERE artifact_id = %s ORDER BY revision DESC""",
                (artifact_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def rollback_to(self, artifact_id: str, revision: int) -> dict[str, Any]:
        """拨指针（数据不动）。仅校验目标版本存在于 kb_artifact_revisions——
        「数据不动」语义下拨任何历史版（含 > current，回滚往返）都安全；
        历史版本行留在 kb_table_rows / kb_artifact_revisions，之后重发布会
        接到 max+1（对抗审查修复④：删去 revision > current 拒绝判据）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise ArtifactError("artifact_not_found")
                revision = int(revision)
                if revision < 1:
                    raise ArtifactError("invalid_revision")
                cur = await conn.execute(
                    "SELECT 1 FROM kb_artifact_revisions"
                    " WHERE artifact_id = %s AND revision = %s",
                    (artifact_id, revision),
                )
                if await cur.fetchone() is None:
                    raise ArtifactError("invalid_revision")
                cur = await conn.execute(
                    """UPDATE kb_artifacts
                       SET current_revision = %s, status = 'published', updated_at = now()
                       WHERE artifact_id = %s
                       RETURNING artifact_id, domain_id, kind, title, description,
                                 usage_note, owner_user_id, status, current_revision,
                                 draft_open, created_at, updated_at""",
                    (revision, artifact_id),
                )
                return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def deprecate(self, artifact_id: str) -> dict[str, Any]:
        return await self._set_status(artifact_id, "deprecated")

    async def republish(self, artifact_id: str) -> dict[str, Any]:
        """deprecated → published（保持 current_revision，不产生新版本）。"""
        return await self._set_status(artifact_id, "published")

    async def _set_status(self, artifact_id: str, target: str) -> dict[str, Any]:
        source = "published" if target == "deprecated" else "deprecated"
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_artifacts SET status = %s, updated_at = now()
                   WHERE artifact_id = %s AND status = %s
                   RETURNING artifact_id, domain_id, kind, title, description,
                             usage_note, owner_user_id, status, current_revision,
                             draft_open, created_at, updated_at""",
                (target, artifact_id, source),
            )
            row = await cur.fetchone()
            if row is None:
                raise ArtifactError("invalid_status")
            return dict(row)

    async def discard_draft(self, artifact_id: str) -> dict[str, Any]:
        """丢弃 draft：DELETE rev0 全部行 + draft_open=FALSE（乱改后的逃生门）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kb_table_rows WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                cur = await conn.execute(
                    """UPDATE kb_artifacts
                       SET draft_open = FALSE, updated_at = now()
                       WHERE artifact_id = %s
                       RETURNING artifact_id, domain_id, kind, title, description,
                                 usage_note, owner_user_id, status, current_revision,
                                 draft_open, created_at, updated_at""",
                    (artifact_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ArtifactError("artifact_not_found")
                return dict(row)

    async def ensure_draft(self, artifact_id: str) -> dict[str, Any]:
        """设计态编辑统一入口（61号 §4.3）：draft_open=FALSE 且 current>0 →
        checkout（current→rev0 复制）再置 TRUE；draft_open=FALSE 且 current=0
        （从未发布）直接置 TRUE（rev0 即工作区）。幂等。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise ArtifactError("artifact_not_found")
                if not art["draft_open"]:
                    if int(art["current_revision"]) > 0:
                        await conn.execute(
                            """INSERT INTO kb_table_rows
                               (artifact_id, revision, row_key, data, provenance,
                                conflicts, ordinal, updated_at, updated_by)
                               SELECT artifact_id, 0, row_key, data, provenance,
                                      conflicts, ordinal, now(), ''
                               FROM kb_table_rows
                               WHERE artifact_id = %s AND revision = %s""",
                            (artifact_id, art["current_revision"]),
                        )
                    cur = await conn.execute(
                        """UPDATE kb_artifacts
                           SET draft_open = TRUE, updated_at = now()
                           WHERE artifact_id = %s
                           RETURNING artifact_id, domain_id, kind, title, description,
                                     usage_note, owner_user_id, status,
                                     current_revision, draft_open, created_at,
                                     updated_at""",
                        (artifact_id,),
                    )
                    art = await cur.fetchone()
                return dict(art)  # type: ignore[arg-type]

    # --------------------------------------------------------------- schema

    async def list_columns(
        self, artifact_id: str, include_dropped: bool = False,
    ) -> list[dict[str, Any]]:
        """active 列（含 alias——61号用例⑨旧列名兼容读取）。"""
        clause = "" if include_dropped else " AND status = 'active'"
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT artifact_id, column_id, column_name, column_type,
                          enum_values, is_primary_key, is_indexed, alias, status,
                          ordinal
                   FROM kb_table_columns WHERE artifact_id = %s""" + clause +
                " ORDER BY ordinal, column_id",
                (artifact_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_column(
        self, artifact_id: str, column_id: str, fields: dict[str, Any],
    ) -> dict[str, Any]:
        """PATCH 式列变更。主键变更必须走 set_primary_key（要重算 row_key），
        is_primary_key 不在白名单。"""
        allowed = {"column_name", "column_type", "enum_values", "is_indexed",
                   "ordinal", "alias"}
        sets: list[str] = []
        params: dict[str, Any] = {"a": artifact_id, "c": column_id}
        for col, val in fields.items():
            if col not in allowed:
                raise ArtifactError("invalid_column_field")
            if col == "column_type" and val not in _VALID_COLUMN_TYPES:
                raise ArtifactError("invalid_column_type")
            key = f"f{len(params)}"
            if col in ("enum_values", "alias"):
                sets.append(f"{col} = %({key})s::jsonb")
            else:
                sets.append(f"{col} = %({key})s")
            params[key] = val
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """INSERT INTO kb_table_columns
                       (artifact_id, column_id, column_name, column_type,
                        is_indexed, ordinal)
                       VALUES (%(a)s, %(c)s,
                               COALESCE(%(name)s, %(c)s), %(type)s, %(idx)s, %(ord)s)
                       ON CONFLICT (artifact_id, column_id) DO NOTHING""",
                    {
                        "a": artifact_id, "c": column_id,
                        "name": fields.get("column_name"),
                        "type": fields.get("column_type", "text"),
                        "idx": fields.get("is_indexed", False),
                        "ord": fields.get("ordinal", 0),
                    },
                )
                if sets:
                    cur = await conn.execute(
                        "UPDATE kb_table_columns SET " + ", ".join(sets)
                        + " WHERE artifact_id = %(a)s AND column_id = %(c)s",
                        params,
                    )
                cur = await conn.execute(
                    """SELECT artifact_id, column_id, column_name, column_type,
                              enum_values, is_primary_key, is_indexed, alias, status,
                              ordinal
                       FROM kb_table_columns
                       WHERE artifact_id = %s AND column_id = %s""",
                    (artifact_id, column_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ArtifactError("invalid_column_field")
                return dict(row)

    async def drop_column(self, artifact_id: str, column_id: str) -> None:
        """软删（单调扩展铁律：动存量数据=不动）。同时清 is_primary_key 标记。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                """UPDATE kb_table_columns
                   SET status = 'dropped', is_primary_key = FALSE
                   WHERE artifact_id = %s AND column_id = %s""",
                (artifact_id, column_id),
            )

    async def set_primary_key(
        self, artifact_id: str, column_ids: list[str],
    ) -> dict[str, Any]:
        """改主键 = 重算 rev0 全部 row_key（61号 §6.3：有数据时独立确认流程）。
        新键冲突（两行折到同键）→ ArtifactError("primary_key_conflict") 整体回滚。
        column_ids 为空 = 无主键：每行换新 uuid（Excel 一次性数据语义）。
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                if await cur.fetchone() is None:
                    raise ArtifactError("artifact_not_found")
                cur = await conn.execute(
                    """SELECT column_id, column_name FROM kb_table_columns
                       WHERE artifact_id = %s AND status = 'active'""",
                    (artifact_id,),
                )
                # data 键 = column_name（不是 column_id）——重算键必须先做 id→name
                # 映射（对抗审查修复②：曾直接拿 column_id 取 data，恒得空键）。
                id_to_name = {
                    str(r["column_id"]): str(r["column_name"])
                    for r in await cur.fetchall()
                }
                unknown = [c for c in column_ids if c not in id_to_name]
                if unknown:
                    raise ArtifactError("unknown_column")
                pk_names = [id_to_name[c] for c in column_ids]
                cur = await conn.execute(
                    """SELECT row_key, data FROM kb_table_rows
                       WHERE artifact_id = %s AND revision = 0 FOR UPDATE""",
                    (artifact_id,),
                )
                rows = [dict(r) for r in await cur.fetchall()]
                remap: dict[str, str] = {}
                seen: set[str] = set()
                for row in rows:
                    new_key = _row_key_value(row["data"], pk_names)
                    if new_key in seen:
                        raise ArtifactError("primary_key_conflict")
                    seen.add(new_key)
                    if new_key != row["row_key"]:
                        remap[row["row_key"]] = new_key
                # 先删后插：避免两行 row_key 互换时撞主键
                if remap:
                    await conn.execute(
                        """DELETE FROM kb_table_rows
                           WHERE artifact_id = %s AND revision = 0
                             AND row_key = ANY(%s)""",
                        (artifact_id, list(remap)),
                    )
                    await self._reinsert_remapped(conn, artifact_id, rows, remap)
                await conn.execute(
                    """UPDATE kb_table_columns
                       SET is_primary_key = (column_id = ANY(%s))
                       WHERE artifact_id = %s""",
                    (list(column_ids), artifact_id),
                )
                await conn.execute(
                    "UPDATE kb_artifacts SET updated_at = now() WHERE artifact_id = %s",
                    (artifact_id,),
                )
                return {"artifact_id": artifact_id,
                        "primary_key": list(column_ids),
                        "rekeyed_rows": len(remap)}

    @staticmethod
    async def _reinsert_remapped(
        conn: Any, artifact_id: str,
        rows: list[dict[str, Any]], remap: dict[str, str],
    ) -> None:
        """把被改键的行按新 row_key 原样重插（data/provenance 不动）。"""
        to_insert = [
            {
                "row_key": remap[r["row_key"]],
                "data": _json(r["data"]),
                "provenance": _json(r.get("provenance") or []),
                "conflicts": _json(r.get("conflicts") or {}),
                "ordinal": r.get("ordinal") or 0,
            }
            for r in rows if r["row_key"] in remap
        ]
        for start in range(0, len(to_insert), _ROW_INSERT_BATCH):
            chunk = to_insert[start:start + _ROW_INSERT_BATCH]
            flat: list[Any] = []
            for item in chunk:
                flat.extend([
                    artifact_id, item["row_key"], item["data"],
                    item["provenance"], item["conflicts"], item["ordinal"],
                ])
            await conn.execute(
                """INSERT INTO kb_table_rows
                   (artifact_id, revision, row_key, data, provenance, conflicts,
                    ordinal, updated_at, updated_by)
                   VALUES """ + ", ".join(
                       ["(%s, 0, %s, %s, %s, %s, %s, now(), '')"] * len(chunk),
                   ),
                flat,
            )

    async def replace_columns_for_artifact(
        self, artifact_id: str, columns: list[dict[str, Any]],
    ) -> None:
        """首次导入建列（删旧插新；draft 态，无历史列则等价全建）。"""
        for col in columns:
            if col.get("column_type") not in _VALID_COLUMN_TYPES:
                raise ArtifactError("invalid_column_type")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kb_table_columns WHERE artifact_id = %s",
                    (artifact_id,),
                )
                for col in columns:
                    await conn.execute(
                        """INSERT INTO kb_table_columns
                           (artifact_id, column_id, column_name, column_type,
                            enum_values, is_primary_key, is_indexed, alias, ordinal)
                           VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)""",
                        (artifact_id, col["column_id"], col["column_name"],
                         col["column_type"], _json(col.get("enum_values")),
                         bool(col.get("is_primary_key", False)),
                         bool(col.get("is_indexed", False)),
                         _json(col.get("alias") or []), int(col.get("ordinal", 0))),
                    )

    # ------------------------------------------------------------------ 行

    async def insert_rows_upsert(
        self, artifact_id: str, revision: int, rows: list[dict[str, Any]],
        updated_by: str = "",
    ) -> dict[str, int]:
        """按 row_key UPSERT（61号 §6 追加导入）：命中→data 覆盖+provenance
        数组拼接+conflicts 覆盖；未命中插入。多行 VALUES 分批 1000/批，
        RETURNING (xmax=0) 区分 inserted/updated。
        """
        inserted = updated = 0
        flat_rows = [
            {
                "row_key": r["row_key"],
                "data": _json(r.get("data") or {}),
                "provenance": _json(r.get("provenance") or []),
                "conflicts": _json(r.get("conflicts") or {}),
                "ordinal": int(r.get("ordinal", 0)),
            }
            for r in rows
        ]
        async with self._pool.connection() as conn:
            for start in range(0, len(flat_rows), _ROW_INSERT_BATCH):
                chunk = flat_rows[start:start + _ROW_INSERT_BATCH]
                # 8 占位符 = 8 列（artifact_id..updated_by）；与 flat.extend 一一对应
                # （对抗审查修复①：曾漏 2 个占位符，真库必炸——静态测试钉死）。
                values_sql = ", ".join(
                    ["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk),
                )
                flat: list[Any] = []
                for item in chunk:
                    flat.extend([
                        artifact_id, revision, item["row_key"], item["data"],
                        item["provenance"], item["conflicts"], item["ordinal"],
                        updated_by,
                    ])
                cur = await conn.execute(
                    """INSERT INTO kb_table_rows
                       (artifact_id, revision, row_key, data, provenance, conflicts,
                        ordinal, updated_by)
                       VALUES """ + values_sql + """
                       ON CONFLICT (artifact_id, revision, row_key) DO UPDATE SET
                           data = EXCLUDED.data,
                           provenance = kb_table_rows.provenance || EXCLUDED.provenance,
                           conflicts = EXCLUDED.conflicts,
                           ordinal = EXCLUDED.ordinal,
                           updated_at = now(),
                           updated_by = EXCLUDED.updated_by
                       RETURNING (xmax = 0) AS _inserted""",
                    flat,
                )
                for hit in await cur.fetchall():
                    if hit["_inserted"]:
                        inserted += 1
                    else:
                        updated += 1
        return {"inserted": inserted, "updated": updated}

    async def update_cell(
        self, artifact_id: str, row_key: str, column: str,
        value: Any, updated_by: str,
    ) -> dict[str, Any] | None:
        """单格更新（jsonb_set），只动 rev0。行不存在返回 None。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_table_rows
                   SET data = jsonb_set(data, ARRAY[%s], %s::jsonb),
                       updated_at = now(), updated_by = %s
                   WHERE artifact_id = %s AND revision = 0 AND row_key = %s
                   RETURNING row_key, data, provenance, conflicts, ordinal""",
                (column, _json(value), updated_by, artifact_id, row_key),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_cell_conflict(
        self, artifact_id: str, row_key: str, column: str,
        marker: str, updated_by: str,
    ) -> dict[str, Any] | None:
        """类型校验失败的单格更新（§7.2，与导入 tolerant 路径对称）：该格置
        null + conflicts[column]=marker，不阻断。行不存在返回 None。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_table_rows
                   SET data = jsonb_set(data, ARRAY[%s], 'null'::jsonb),
                       conflicts = conflicts || jsonb_build_object(%s, %s),
                       updated_at = now(), updated_by = %s
                   WHERE artifact_id = %s AND revision = 0 AND row_key = %s
                   RETURNING row_key, data, provenance, conflicts, ordinal""",
                (column, column, marker, updated_by, artifact_id, row_key),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_row(
        self, artifact_id: str, revision: int, row_key: str,
    ) -> dict[str, Any] | None:
        """单行读取（行增端点冲突预检与回读）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT row_key, data, provenance, conflicts, ordinal
                   FROM kb_table_rows
                   WHERE artifact_id = %s AND revision = %s AND row_key = %s""",
                (artifact_id, revision, row_key),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def batch_assign(
        self, artifact_id: str, column: str, value: Any,
        where_clauses: list[dict[str, Any]], updated_by: str,
    ) -> int:
        """批量赋值一条 SQL：data || '{"列":"值"}'（61号 §7.2）。返回命中行数。
        where_clauses=[{column, op, value}]，op 白名单见 _VALID_FILTER_OPS，
        列名与值全参数化。
        """
        conds, filter_params = self._build_row_filters(where_clauses)
        # 占位符顺序 = SQL 出现顺序：SET 的 jsonb / updated_by / WHERE 的 artifact_id
        params = [_json({column: value}), updated_by, artifact_id, *filter_params]
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE kb_table_rows
                   SET data = data || %s::jsonb, updated_at = now(), updated_by = %s
                   WHERE artifact_id = %s AND revision = 0""" + conds,
                params,
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _build_row_filters(where_clauses: list[dict[str, Any]]) -> tuple[str, list[Any]]:
        if not where_clauses:
            return "", []
        parts: list[str] = []
        params: list[Any] = []
        for clause in where_clauses:
            op = str(clause.get("op", "eq"))
            if op not in _VALID_FILTER_OPS:
                raise ArtifactError("invalid_filter_op")
            col = clause.get("column")
            if not col or not isinstance(col, str):
                raise ArtifactError("invalid_filter_column")
            val = clause.get("value")
            if op == "is_null":
                parts.append("NOT (data ? %s)")
                params.append(col)
            elif op == "not_null":
                parts.append("(data ? %s)")
                params.append(col)
            elif op == "in":
                if not isinstance(val, list):
                    raise ArtifactError("invalid_filter_value")
                parts.append("(data->>%s) = ANY(%s)")
                params.extend([col, [str(v) for v in val]])
            elif op == "contains":
                like = f"%{str(val).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%"
                parts.append("(data->>%s) ILIKE %s")
                params.extend([col, like])
            else:  # eq / ne
                parts.append(f"(data->>%s) {'=' if op == 'eq' else '<>'} %s")
                params.extend([col, str(val)])
        return " AND " + " AND ".join(parts), params

    async def delete_row(self, artifact_id: str, row_key: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """DELETE FROM kb_table_rows
                   WHERE artifact_id = %s AND revision = 0 AND row_key = %s
                   RETURNING row_key""",
                (artifact_id, row_key),
            )
            return await cur.fetchone() is not None

    async def list_rows(
        self, artifact_id: str, revision: int, page: int = 1, size: int = 50,
        sort_ordinal: bool = True,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        size = max(1, min(_MAX_PAGE_SIZE, int(size)))
        order = "ordinal, row_key" if sort_ordinal else "row_key"
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT row_key, data, provenance, conflicts, ordinal
                    FROM kb_table_rows
                    WHERE artifact_id = %s AND revision = %s
                    ORDER BY {order}
                    LIMIT %s OFFSET %s""",
                (artifact_id, revision, size, (page - 1) * size),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                """SELECT COUNT(*) AS n FROM kb_table_rows
                   WHERE artifact_id = %s AND revision = %s""",
                (artifact_id, revision),
            )
            total = int((await cur.fetchone())["n"])  # type: ignore[index]
        return {"rows": rows, "total": total, "page": page, "size": size}

    async def count_rows(self, artifact_id: str, revision: int) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT COUNT(*) AS n FROM kb_table_rows
                   WHERE artifact_id = %s AND revision = %s""",
                (artifact_id, revision),
            )
            return int((await cur.fetchone())["n"])  # type: ignore[index]

    async def diff_draft_vs_current(self, artifact_id: str) -> dict[str, Any]:
        """draft(rev0) vs current 的行级+列级差异（61号 §八，「本次发布将产生
        的变化」口径）：added=只在 draft（发布将新增）；removed=只在 current
        （发布将删除）；changed 列级 old=current 值 → new=draft 值。
        双侧 CTE + FULL JOIN，过滤全在 CTE 内——WHERE 过滤会丢 removed 行。
        列级 changed 在 Python 对比 jsonb。从未发布（current=0）时 current
        侧取 -1（空集）→ 全部计为新增。

        干净态投影（对抗审查修复③）：draft_open=FALSE 且 current>0 = 与已发布
        一致（list_rows 同口径），直接返回零差异，不进 CTE。样例截断（修复⑦）：
        added/removed/changed 各只回前 100 条，counts 恒全量——防 10 万行
        GB 级响应。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT current_revision, draft_open FROM kb_artifacts
                   WHERE artifact_id = %s""",
                (artifact_id,),
            )
            art = await cur.fetchone()
            if art is not None and not art["draft_open"] \
                    and int(art["current_revision"]) > 0:
                return self._empty_diff()
            current_rev = int(art["current_revision"]) if art else 0
            cur = await conn.execute(
                """WITH d AS (SELECT row_key, data FROM kb_table_rows
                              WHERE artifact_id = %s AND revision = 0),
                     c AS (SELECT row_key, data FROM kb_table_rows
                           WHERE artifact_id = %s AND revision = %s)
                SELECT COALESCE(d.row_key, c.row_key) AS row_key,
                       (c.row_key IS NULL) AS added,
                       (d.row_key IS NULL) AS removed,
                       c.data AS old_data, d.data AS new_data
                FROM d FULL JOIN c ON d.row_key = c.row_key
                WHERE d.row_key IS NULL OR c.row_key IS NULL
                   OR d.data IS DISTINCT FROM c.data""",
                (artifact_id, artifact_id, current_rev if current_rev > 0 else -1),
            )
            raw = [dict(r) for r in await cur.fetchall()]
        added: list[str] = []
        removed: list[str] = []
        changed: list[dict[str, Any]] = []
        for row in raw:
            key = str(row["row_key"])
            if row["added"]:
                added.append(key)
                continue
            if row["removed"]:
                removed.append(key)
                continue
            old = row["old_data"] or {}
            new = row["new_data"] or {}
            for col in sorted(set(old) | set(new)):
                if old.get(col) != new.get(col):
                    changed.append(
                        {"row_key": key, "column": col,
                         "old": old.get(col), "new": new.get(col)},
                    )
        return {
            "added": added[:_DIFF_SAMPLE_LIMIT], "removed": removed[:_DIFF_SAMPLE_LIMIT],
            "changed": changed[:_DIFF_SAMPLE_LIMIT],
            "added_count": len(added), "removed_count": len(removed),
            "changed_count": len(changed),
        }

    @staticmethod
    def _empty_diff() -> dict[str, Any]:
        return {
            "added": [], "removed": [], "changed": [],
            "added_count": 0, "removed_count": 0, "changed_count": 0,
        }

    # ---------------------------------------------------- 表达式索引（§4.5）

    async def create_expression_indexes(
        self, artifact_id: str, revision: int,
        indexed_columns: list[dict[str, Any]],
    ) -> list[str]:
        """按 is_indexed 列生成类型化表达式索引：int/float 加 ::numeric cast
        （空串先 NULLIF，防 cast 崩）；其余按文本。命名 idx_kbtr_{art}_{col}。
        revision 仅为调用方语义（61号：发布后建）；索引列前缀含 artifact_id+
        revision，本制品任意版本查询共用同一索引。返回创建的索引名列表。
        """
        created: list[str] = []
        if not indexed_columns:
            return created
        async with self._pool.connection() as conn:
            for col in indexed_columns:
                name = f"idx_kbtr_{artifact_id}_{col['column_id']}"
                col_ref = sql.Literal(str(col["column_name"]))
                if col.get("column_type") in ("int", "float"):
                    expr = sql.SQL("((NULLIF(data->>{}, ''))::numeric)").format(col_ref)
                else:
                    expr = sql.SQL("(data->{})").format(col_ref)
                stmt = sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON kb_table_rows"
                    " (artifact_id, revision, {})"
                ).format(sql.Identifier(name), expr)
                await conn.execute(stmt)
                created.append(name)
        return created

    async def drop_draft_expression_indexes(self, artifact_id: str) -> None:
        """清理本制品全部动态索引（重发布前——列类型/名字变了旧定义会残留，
        CREATE INDEX IF NOT EXISTS 不会替换既有索引）。"""
        pattern = (
            f"idx_kbtr_{artifact_id}_%"
            .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT indexname FROM pg_indexes
                   WHERE tablename = 'kb_table_rows' AND indexname LIKE %s
                   ESCAPE '\\'""",
                (pattern,),
            )
            names = [str(r["indexname"]) for r in await cur.fetchall()]
            for name in names:
                await conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(
                    sql.Identifier(name),
                ))

    # ------------------------------------------------------------ 导入批次

    async def create_batch(
        self, artifact_id: str, *, channel: str, file_name: str,
        sheet_name: str, row_count: int, created_by: str,
    ) -> dict[str, Any]:
        if channel not in ("upload", "kb_import"):
            raise ArtifactError("invalid_channel")
        batch_id = f"bat_{uuid.uuid4().hex}"
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO kb_artifact_batches
                   (batch_id, artifact_id, channel, file_name, sheet_name,
                    row_count, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING batch_id, artifact_id, channel, file_name,
                             sheet_name, row_count, created_by, created_at""",
                (batch_id, artifact_id, channel, file_name, sheet_name,
                 row_count, created_by),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def list_batches(self, artifact_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT batch_id, artifact_id, channel, file_name, sheet_name,
                          row_count, created_by, created_at
                   FROM kb_artifact_batches
                   WHERE artifact_id = %s ORDER BY created_at DESC""",
                (artifact_id,),
            )
            return [dict(r) for r in await cur.fetchall()]
