# -*- coding: utf-8 -*-
"""KB 知识制品工作台运行层（67号工作台 MVP Task 3）—— kb_workspace_runs /
kb_workspace_run_outputs / kb_workspace_output_bindings / kb_workspace_exports
仓储。

风格契约与 db_workspaces.py（Task 2）一致：TEXT id、参数化 SQL、dict_row、
多语句显式 ``conn.transaction()``、时间戳 TEXT ISO。conn 注入面按 Task 3
规格收窄到服务层组合事务点（try_start_guard / write_outputs / set_stale /
upsert_binding / insert_export 五个，首参可选 conn，注入时嵌套 transaction
退化为 savepoint 行为等价）；生命周期/进度/读/结果回写归 executor 单写者
直连 pool，不设 conn 参数。错误类不另起炉灶——直接复用登记层四件
（Conflict 属图乐观锁，本层无此语义，仅为服务层统一 import 面而
re-export）。

机制权威 = docs/superpowers/plans/2026-11-16-workbench-mvp.md Task 3
（Task 9 导出交付面增量：get_export_by_request / reset_failed_export /
get_binding / upsert_binding(exported_at) / mark_export_result
(target_artifact_id)——读与终态回写均直连 pool，导出编排归服务层）：
- try_start_guard 照 db_compile_inputs.try_start_guard 三段式：单事务
  ``pg_advisory_xact_lock(hashtext(workspace_id))`` 内 ①查在飞（queued/
  running）②工作区存在·active ③INSERT queued。**graph_snapshot_json 随
  INSERT 原子携带**——不留「提交后补写」裸窗口（审查 I-1 同源教训：裸窗口
  内并发第二个 start 会双建 run）。
- 输出 current 指针：uq_ws_output_current 部分唯一索引下，重写同
  (workspace,node,port) 必须**先翻转旧 current 再 INSERT**（先插会撞索引）
  ——两步同事务原子；同 run 重写走 PK ON CONFLICT 更新（幂等）。
- 行数护栏（MAX_ROWS_PER_PORT=50_000）：任一 port 超限 → 拒绝**整个**
  写入——半批落库会让 current 指针指向缺 port 的残态。
- content_hash 本任务**不实现**：缺省空串落库；write_outputs 接受可选
  hashes 参数，Task 4 数据集契约（dataset_hash）落地后由 executor 层传入。
- 导出幂等入口：request_id UNIQUE + ON CONFLICT DO NOTHING 回读——
  重放命中既有行原样返回（created=False），payload 差异不覆盖。
"""
from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,  # noqa: F401 —— re-export（服务层统一 import 面）
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)

__all__ = [
    "MAX_ROWS_PER_PORT",
    "WorkspaceConflictError",
    "WorkspaceNotFoundError",
    "WorkspaceValidationError",
    "WorkspaceRunDB",
]

#: run scope 合法值（封闭词表；trial 试跑不建 run——67号 §4.1）。
_VALID_SCOPES = ("node", "workspace", "subgraph")

#: 单 port 输出行数护栏（对齐 Task 4 workspace_dataset.MAX_ROWS；超限拒整批）。
MAX_ROWS_PER_PORT = 50_000

#: 导出结果回写的合法终态（pending 是初始态，不允许回写）。
_VALID_EXPORT_TERMINAL = ("succeeded", "failed")

#: run 行完整列清单（RETURNING/SELECT 共用，排布同迁移 012 DDL）。
_RUN_COLUMNS = """id, workspace_id, scope, target_node_id, graph_revision,
                  graph_hash, graph_snapshot_json, batch_no, status,
                  progress_json, error, actor_kind, actor_id, created_by,
                  created_at, started_at, finished_at"""

#: 输出行完整列清单（kb_workspace_run_outputs）。
_OUTPUT_COLUMNS = """run_id, workspace_id, node_id, port_id, kind,
                     content_inline, row_count, content_hash, quality_json,
                     is_current, stale_since_run, created_at"""

#: 绑定行完整列清单（kb_workspace_output_bindings）。
_BINDING_COLUMNS = """workspace_id, node_id, port_id, artifact_id,
                      artifact_kind, last_run_id, last_exported_at,
                      created_by, created_at"""

#: 导出行完整列清单（kb_workspace_exports）。
_EXPORT_COLUMNS = """id, request_id, workspace_id, node_id, port_id, mode,
                     target_artifact_id, snapshot_run_id, mapping_json,
                     status, result_json, error, actor_kind, actor_id,
                     created_by, created_at"""


def _new_run_id() -> str:
    """wsr_ + 16hex（012 DDL 注释契约；secrets 满足不可预测性）。"""
    return f"wsr_{secrets.token_hex(8)}"


def _new_export_id() -> str:
    return f"wse_{secrets.token_hex(8)}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _row_count(dataset: dict[str, Any]) -> int:
    """最小行数口径（Task 4 dataset 契约前的本层自带实现）：rows 非列表
    → Validation 拦在写库前（脏形态不进 content_inline）。"""
    rows = dataset.get("rows", [])
    if not isinstance(rows, list):
        raise WorkspaceValidationError(
            "dataset.rows must be a list")
    return len(rows)


class WorkspaceRunDB:
    """kb_workspace_runs / run_outputs / bindings / exports 仓储。

    构造入参为 row_factory=dict_row 的 psycopg AsyncConnectionPool；
    登记三表（workspaces/members/sources）归 db_workspaces.WorkspaceDB。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def _use_conn(self, conn: Any | None) -> AsyncIterator[Any]:
        """conn 注入则复用（服务层组合事务），否则自开 pool 连接。"""
        if conn is not None:
            yield conn
        else:
            async with self._pool.connection() as own:
                yield own

    # ------------------------------------------------------------ 启动守卫

    async def try_start_guard(
        self, conn: Any | None = None, workspace_id: str = "",
        created_by: str = "", *, graph_snapshot: dict[str, Any],
        graph_hash: str, scope: str, target_node_id: str | None,
        batch_no: int, actor_kind: str = "user", actor_id: str = "",
    ) -> dict[str, Any]:
        """run start 串行化门（照 db_compile_inputs.try_start_guard 三段式）：
        单事务 ``pg_advisory_xact_lock(hashtext(workspace_id))`` 内依次——

        ① 查在飞 run（status ∈ queued/running）→ 原样返回该行（service 翻
           409 run_in_progress，**不建新行**）；
        ② 工作区不存在 → NotFound；status != 'active'（归档）→ Validation；
           graph_revision 就地取自锁内工作区行（快照归快照——冻结的是调用方
           读到的图，revision 记录落 run 时刻工作区行的版本）；
        ③ INSERT queued，**graph_snapshot_json 随 INSERT 原子携带**（不留
           提交后补写裸窗口）。

        返回 ``{"in_flight": 行|None, "run": 新行|None}``——service 只做
        翻译不做判定，判定语义全部留在本方法（锁内原子）。"""
        # 锁外纯校验（照 put_graph：畸形在拿锁前拦下，不占锁也不进事务）
        if not isinstance(graph_snapshot, dict) \
                or not isinstance(graph_snapshot.get("nodes"), list) \
                or not graph_snapshot["nodes"]:
            raise WorkspaceValidationError(
                "graph_snapshot must be an object with a non-empty"
                " 'nodes' list")
        if not isinstance(graph_hash, str) or not graph_hash:
            raise WorkspaceValidationError(
                "graph_hash must be a non-empty string")
        if scope not in _VALID_SCOPES:
            raise WorkspaceValidationError(
                f"scope must be one of {list(_VALID_SCOPES)}: {scope!r}")
        if scope == "node" and (not isinstance(target_node_id, str)
                                or not target_node_id):
            raise WorkspaceValidationError(
                "scope='node' requires a non-empty target_node_id")
        if actor_kind not in ("user", "mcp_key"):
            raise WorkspaceValidationError(
                f"actor_kind must be 'user' or 'mcp_key': {actor_kind!r}")

        async with self._use_conn(conn) as c:
            async with c.transaction():
                await c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%(w)s))",
                    {"w": workspace_id},
                )
                cur = await c.execute(
                    f"""SELECT {_RUN_COLUMNS} FROM kb_workspace_runs
                        WHERE workspace_id = %s AND status IN ('queued',
                                                              'running')
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1""",
                    (workspace_id,),
                )
                row = await cur.fetchone()
                if row is not None:
                    return {"in_flight": dict(row), "run": None}
                cur = await c.execute(
                    """SELECT status, graph_revision FROM kb_workspaces
                       WHERE id = %s""",
                    (workspace_id,),
                )
                ws_row = await cur.fetchone()
                if ws_row is None:
                    raise WorkspaceNotFoundError(
                        f"workspace not found: {workspace_id}")
                if ws_row["status"] != "active":
                    raise WorkspaceValidationError(
                        "workspace is archived, cannot start run:"
                        f" {workspace_id}")
                cur = await c.execute(
                    f"""INSERT INTO kb_workspace_runs
                        (id, workspace_id, scope, target_node_id,
                         graph_revision, graph_hash, graph_snapshot_json,
                         batch_no, status, actor_kind, actor_id, created_by,
                         created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                                'queued', %s, %s, %s, %s)
                        RETURNING {_RUN_COLUMNS}""",
                    (_new_run_id(), workspace_id, scope, target_node_id,
                     int(ws_row["graph_revision"]), graph_hash,
                     _json(graph_snapshot), int(batch_no), actor_kind,
                     actor_id, created_by, _utcnow()),
                )
                run = dict(await cur.fetchone())  # type: ignore[arg-type]
        return {"in_flight": None, "run": run}

    # ------------------------------------------------------------ 生命周期

    async def mark_running(self, run_id: str) -> bool:
        """queued→running（仅此单步；重复调用 rowcount=0 → False 不抛，
        started_at 不被改写——单写者执行器的边界重试安全）。"""
        return await self._transition(run_id, "running")

    async def mark_completed(self, run_id: str) -> bool:
        return await self._transition(run_id, "completed")

    async def mark_partial(self, run_id: str) -> bool:
        """partial = 有节点失败的完结（分支语义由 executor 判定，本层只落
        状态机迁移）。"""
        return await self._transition(run_id, "partial")

    async def mark_failed(self, run_id: str, error: str = "") -> bool:
        if not isinstance(error, str):
            raise WorkspaceValidationError("error must be a string")
        return await self._transition(run_id, "failed", error=error)

    async def mark_cancelled(self, run_id: str) -> bool:
        return await self._transition(run_id, "cancelled")

    async def reconcile_stale_running(self, *, stale_minutes: int) -> int:
        """崩溃恢复账（67号 Task 6 决策 6；Task 7 质量收口 B 自 executor
        裸 SQL 下沉）：status='running' 且 started_at 超过 stale_minutes 的
        run 判 failed(error='interrupted: executor restart suspected')，
        返回判定行数。幂等（running 行判定后不复存在；残 run 只认账不复
        活）。started_at 是 TEXT ISO，比较经 ::timestamptz 显式转型（不同
        偏移写法的字典序不可靠）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                """UPDATE kb_workspace_runs
                   SET status = 'failed', finished_at = %s,
                       error = 'interrupted: executor restart suspected'
                   WHERE status = 'running'
                     AND started_at IS NOT NULL
                     AND started_at::timestamptz
                         < now() - (%s * interval '1 minute')""",
                (_utcnow(), int(stale_minutes)),
            )
            return int(cur.rowcount or 0)

    async def _transition(
        self, run_id: str, target: str, *, error: str | None = None,
    ) -> bool:
        """状态机单步：目标 running 仅 queued 可入；四个终态从任意非终态
        （queued/running/awaiting_review）可入。**终态不可再变**——守卫
        WHERE 不命中 → rowcount=0 → False（不抛，幂等口径）；run 不存在同
        样 False。"""
        if target == "running":
            sql = ("UPDATE kb_workspace_runs SET status = 'running',"
                   " started_at = %s"
                   " WHERE id = %s AND status = 'queued'")
            params: tuple[Any, ...] = (_utcnow(), run_id)
        else:
            sets = ["status = %s", "finished_at = %s"]
            values: list[Any] = [target, _utcnow()]
            if error is not None:
                sets.append("error = %s")
                values.append(error)
            sql = (f"UPDATE kb_workspace_runs SET {', '.join(sets)}"
                   " WHERE id = %s AND status IN ('queued', 'running',"
                   " 'awaiting_review')")
            params = (*values, run_id)
        async with self._use_conn(None) as c:
            cur = await c.execute(sql, params)
            return bool(cur.rowcount or 0)

    async def update_progress(
        self, run_id: str, *, done_node: str | None,
        current_node: str | None, failed_rows: list[str] | None = None,
        node_error: tuple[str, str] | None = None,
    ) -> bool:
        """progress_json 增量合并（FOR UPDATE 读改写——执行器单写者，行锁
        兜底并发）：done 追加去重；failed_rows 按节点并集去重（必须挂
        done_node——节点完结时点上报该节点的失败行账）；current_node 直写
        （None=清空表示执行间隙）；node_error=(node_id, msg) 合并进
        progress["node_errors"]（Task 6 执行器节点级失败账——分支 partial
        的失败清单口径，不进 run.error 摘要）。run 不存在 → False。"""
        if failed_rows is None:
            failed_rows = []
        if done_node is not None \
                and (not isinstance(done_node, str) or not done_node):
            raise WorkspaceValidationError(
                "done_node must be a non-empty string")
        if not isinstance(failed_rows, list) \
                or not all(isinstance(r, str) for r in failed_rows):
            raise WorkspaceValidationError(
                "failed_rows must be a list of strings")
        if failed_rows and done_node is None:
            raise WorkspaceValidationError(
                "failed_rows requires done_node (failures are accounted"
                " when the node completes)")
        if node_error is not None:
            err_nid, err_msg = node_error
            if not isinstance(err_nid, str) or not err_nid \
                    or not isinstance(err_msg, str) or not err_msg:
                raise WorkspaceValidationError(
                    "node_error must be a (node_id, message) tuple of"
                    " non-empty strings")

        async with self._use_conn(None) as c:
            async with c.transaction():
                cur = await c.execute(
                    """SELECT progress_json FROM kb_workspace_runs
                       WHERE id = %s FOR UPDATE""",
                    (run_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                progress: dict[str, Any] = dict(row["progress_json"] or {})
                if done_node is not None:
                    done = list(progress.get("done") or [])
                    if done_node not in done:
                        done.append(done_node)
                    progress["done"] = done
                    if failed_rows:
                        failed_map = dict(progress.get("failed_rows") or {})
                        merged = list(failed_map.get(done_node) or [])
                        for item in failed_rows:
                            if item not in merged:
                                merged.append(item)
                        progress["failed_rows"] = {
                            **failed_map, done_node: merged}
                if node_error is not None:
                    err_nid, err_msg = node_error
                    errors = dict(progress.get("node_errors") or {})
                    errors[err_nid] = err_msg
                    progress["node_errors"] = errors
                progress["current_node"] = current_node
                await c.execute(
                    """UPDATE kb_workspace_runs SET progress_json = %s::jsonb
                       WHERE id = %s""",
                    (_json(progress), run_id),
                )
                return True

    # ------------------------------------------------------------ 输出写入

    async def write_outputs(
        self, conn: Any | None = None, *, run_id: str, workspace_id: str,
        node_id: str, outputs: dict[str, dict[str, Any]],
        hashes: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """节点输出落库（单事务）：a) 锁外逐 port 校验+算 row_count（超
        MAX_ROWS_PER_PORT → 拒**整个**写入——半批落库会让 current 指向缺
        port 残态）；b) **先**翻转旧 current（排除本 run 行——幂等重写）；
        c) INSERT 新行 is_current=TRUE（同 run 重 port 走 PK ON CONFLICT
        更新）。先翻后插是 uq_ws_output_current 部分唯一索引下的唯一合法
        顺序——先插必撞索引。kind 固定 'table'（MVP 数据集契约唯一形态，
        documents/relations 预留 Task 4+）。返回落库行（port 序）。"""
        hashes = hashes or {}
        if not isinstance(outputs, dict) or not outputs:
            raise WorkspaceValidationError(
                "outputs must be a non-empty dict of"
                " port -> {'dataset': object, 'quality': object}")
        prepared: list[dict[str, Any]] = []
        for port_id, payload in outputs.items():
            if not isinstance(port_id, str) or not port_id:
                raise WorkspaceValidationError(
                    f"port id must be a non-empty string: {port_id!r}")
            if not isinstance(payload, dict) \
                    or not isinstance(payload.get("dataset"), dict):
                raise WorkspaceValidationError(
                    f"port {port_id}: payload must include a 'dataset'"
                    " object")
            quality = payload.get("quality") or {}
            if not isinstance(quality, dict):
                raise WorkspaceValidationError(
                    f"port {port_id}: quality must be an object")
            count = _row_count(payload["dataset"])
            if count > MAX_ROWS_PER_PORT:
                raise WorkspaceValidationError(
                    f"port {port_id}: rows exceed limit {MAX_ROWS_PER_PORT}:"
                    f" {count} (entire write rejected)")
            prepared.append({
                "port_id": port_id, "dataset": payload["dataset"],
                "row_count": count, "quality": quality,
                "content_hash": hashes.get(port_id, ""),
            })

        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    """SELECT 1 FROM kb_workspace_runs
                       WHERE id = %s AND workspace_id = %s""",
                    (run_id, workspace_id),
                )
                if await cur.fetchone() is None:
                    raise WorkspaceNotFoundError(
                        f"run not found in workspace: {run_id}")
                await c.execute(
                    """UPDATE kb_workspace_run_outputs SET is_current = FALSE
                       WHERE workspace_id = %s AND node_id = %s
                         AND port_id = ANY(%s) AND is_current
                         AND run_id <> %s""",
                    (workspace_id, node_id,
                     [p["port_id"] for p in prepared], run_id),
                )
                now = _utcnow()
                written: list[dict[str, Any]] = []
                for p in prepared:
                    cur = await c.execute(
                        f"""INSERT INTO kb_workspace_run_outputs
                            (run_id, workspace_id, node_id, port_id, kind,
                             content_inline, row_count, content_hash,
                             quality_json, is_current, created_at)
                            VALUES (%s, %s, %s, %s, 'table', %s::jsonb, %s,
                                    %s, %s::jsonb, TRUE, %s)
                            ON CONFLICT (run_id, node_id, port_id)
                            DO UPDATE SET
                                kind = 'table',
                                content_inline = EXCLUDED.content_inline,
                                row_count = EXCLUDED.row_count,
                                content_hash = EXCLUDED.content_hash,
                                quality_json = EXCLUDED.quality_json,
                                is_current = TRUE,
                                created_at = EXCLUDED.created_at
                            RETURNING {_OUTPUT_COLUMNS}""",
                        (run_id, workspace_id, node_id, p["port_id"],
                         _json(p["dataset"]), p["row_count"],
                         p["content_hash"], _json(p["quality"]), now),
                    )
                    written.append(
                        dict(await cur.fetchone()))  # type: ignore[arg-type]
                return written

    async def set_stale(
        self, conn: Any | None = None, workspace_id: str = "",
        node_ports: list[tuple[str, str]] | None = None,
        since_run_id: str = "",
    ) -> int:
        """输出级失效传播：当前行打 stale_since_run（67号 §5.2——与节点态
        stale 标志分工：上游已变的账记在输出行上，由 executor 在重跑上游后
        调用）。幂等：``stale_since_run IS DISTINCT FROM`` 守卫——已指向同
        一 run 的行不再更新（返回 0），改指更新的 run 才动。只打 is_current
        行（历史行无所谓 stale）。返回更新行数。"""
        if not node_ports:
            return 0
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                """UPDATE kb_workspace_run_outputs
                   SET stale_since_run = %(run)s
                   WHERE workspace_id = %(ws)s
                     AND (node_id, port_id) IN (
                         SELECT u.n, u.p
                         FROM unnest(%(nodes)s::text[], %(ports)s::text[])
                              AS u(n, p))
                     AND is_current
                     AND stale_since_run IS DISTINCT FROM %(run)s""",
                {"ws": workspace_id, "run": since_run_id,
                 "nodes": [n for n, _ in node_ports],
                 "ports": [p for _, p in node_ports]},
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------ 读

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"SELECT {_RUN_COLUMNS} FROM kb_workspace_runs"
                " WHERE id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_runs(
        self, workspace_id: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """created_at DESC（id DESC 兜底同微秒并列）；含全部状态（终态/
        在飞分列口径由调用方按 status 取）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"""SELECT {_RUN_COLUMNS} FROM kb_workspace_runs
                    WHERE workspace_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s""",
                (workspace_id, int(limit)),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_current_outputs(
        self, workspace_id: str,
    ) -> dict[str, dict[str, Any]]:
        """工作台当前输出面：dict["node:port" -> 行]（content_inline 为
        jsonb 解析后的数据集对象；含 quality_json/stale_since_run——前端
        画布消费与 Agent 读口径同一来源）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"""SELECT {_OUTPUT_COLUMNS} FROM kb_workspace_run_outputs
                    WHERE workspace_id = %s AND is_current
                    ORDER BY node_id, port_id""",
                (workspace_id,),
            )
            return {
                f"{r['node_id']}:{r['port_id']}": dict(r)
                for r in await cur.fetchall()
            }

    async def get_run_outputs(self, run_id: str) -> list[dict[str, Any]]:
        """单 run 的历史输出行（含已被后续 run 翻转 is_current=FALSE 的），
        审计/回放口径。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"""SELECT {_OUTPUT_COLUMNS} FROM kb_workspace_run_outputs
                    WHERE run_id = %s
                    ORDER BY node_id, port_id""",
                (run_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ 绑定与导出

    async def upsert_binding(
        self, conn: Any | None = None, workspace_id: str = "",
        node_id: str = "", port_id: str = "", artifact_id: str = "",
        artifact_kind: str = "", last_run_id: str | None = None,
        updated_by: str = "", exported_at: str | None = None,
    ) -> dict[str, Any]:
        """port→artifact 导出去处绑定：PK 冲突即更新 artifact/last_run_id/
        last_exported_at（保留首插 created_by——绑定关系不换人，只换目标）。
        updated_by 仅首插落 created_by（表无审计列，同 upsert_source 口径）。
        exported_at（Task 9）：显式传入时落 last_exported_at——导出事务内
        传 ``SELECT now()`` 值，与同事务制品行 updated_at（now() 写入）同钟
        等值，人工改动门禁（updated_at > last_exported_at）不因 Python/DB
        双钟偏差误伤。"""
        for label, value in (("workspace_id", workspace_id),
                             ("node_id", node_id), ("port_id", port_id),
                             ("artifact_id", artifact_id),
                             ("artifact_kind", artifact_kind)):
            if not isinstance(value, str) or not value:
                raise WorkspaceValidationError(
                    f"{label} must be a non-empty string")
        async with self._use_conn(conn) as c:
            cur = await c.execute(
                f"""INSERT INTO kb_workspace_output_bindings
                    (workspace_id, node_id, port_id, artifact_id,
                     artifact_kind, last_run_id, last_exported_at,
                     created_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, node_id, port_id)
                    DO UPDATE SET
                        artifact_id = EXCLUDED.artifact_id,
                        artifact_kind = EXCLUDED.artifact_kind,
                        last_run_id = EXCLUDED.last_run_id,
                        last_exported_at = EXCLUDED.last_exported_at
                    RETURNING {_BINDING_COLUMNS}""",
                (workspace_id, node_id, port_id, artifact_id, artifact_kind,
                 last_run_id, exported_at or _utcnow(), updated_by,
                 _utcnow()),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def get_binding(
        self, *, workspace_id: str, node_id: str, port_id: str,
    ) -> dict[str, Any] | None:
        """绑定行读（Task 9 导出门禁 c：artifact.updated_at 对比
        binding.last_exported_at 的入口）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"""SELECT {_BINDING_COLUMNS}
                    FROM kb_workspace_output_bindings
                    WHERE workspace_id = %s AND node_id = %s
                      AND port_id = %s""",
                (workspace_id, node_id, port_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def insert_export(
        self, conn: Any | None = None, *, request_id: str,
        workspace_id: str, node_id: str, port_id: str,
        target_artifact_id: str | None = None,
        snapshot_run_id: str | None = None,
        mapping_json: dict[str, Any] | None = None,
        created_by: str, actor_kind: str = "user", actor_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """导出任务登记（幂等入口）：request_id UNIQUE——重放命中既有行
        原样返回（created=False），**不覆盖任何字段**（DO NOTHING+回读，
        与 update_batch_details 的「后到者不覆写」同向）。mode 固定
        'create_draft'（DDL CHECK 只此一值：导出只建草稿绝不直接发布）。
        返回 (行, created)。"""
        if not isinstance(request_id, str) or not request_id:
            raise WorkspaceValidationError(
                "request_id must be a non-empty string")
        if not isinstance(mapping_json, dict):
            raise WorkspaceValidationError("mapping_json must be an object")
        if actor_kind not in ("user", "mcp_key"):
            raise WorkspaceValidationError(
                f"actor_kind must be 'user' or 'mcp_key': {actor_kind!r}")
        async with self._use_conn(conn) as c:
            async with c.transaction():
                cur = await c.execute(
                    f"""INSERT INTO kb_workspace_exports
                        (id, request_id, workspace_id, node_id, port_id,
                         mode, target_artifact_id, snapshot_run_id,
                         mapping_json, status, actor_kind, actor_id,
                         created_by, created_at)
                        VALUES (%s, %s, %s, %s, %s, 'create_draft', %s, %s,
                                %s::jsonb, 'pending', %s, %s, %s, %s)
                        ON CONFLICT (request_id) DO NOTHING
                        RETURNING {_EXPORT_COLUMNS}""",
                    (_new_export_id(), request_id, workspace_id, node_id,
                     port_id, target_artifact_id, snapshot_run_id,
                     _json(mapping_json or {}), actor_kind, actor_id,
                     created_by, _utcnow()),
                )
                row = await cur.fetchone()
                if row is not None:
                    return dict(row), True
                cur = await c.execute(
                    f"""SELECT {_EXPORT_COLUMNS} FROM kb_workspace_exports
                        WHERE request_id = %s""",
                    (request_id,),
                )
                row = await cur.fetchone()
                return dict(row), False  # type: ignore[arg-type]

    async def mark_export_result(
        self, export_id: str, status: str,
        result_json: dict[str, Any] | None = None,
        error: str | None = None,
        target_artifact_id: str | None = None,
    ) -> bool:
        """导出结果回写：pending → 终态一次性（守卫 WHERE status='pending'
        → 重放/并发回写 rowcount=0 → False）；succeeded 必带 result_json、
        failed 必带 error 文本（配对校验，不带即 Validation）。
        target_artifact_id（Task 9）：provided 才覆写——导出登记行先建
        （request_id 幂等占位），制品在逐 item 事务里创建，终态回写时补账。"""
        if status not in _VALID_EXPORT_TERMINAL:
            raise WorkspaceValidationError(
                f"status must be one of {list(_VALID_EXPORT_TERMINAL)}:"
                f" {status!r}")
        if status == "succeeded" and not isinstance(result_json, dict):
            raise WorkspaceValidationError(
                "status='succeeded' requires a result_json object")
        if status == "failed" and (not isinstance(error, str) or not error):
            raise WorkspaceValidationError(
                "status='failed' requires a non-empty error string")
        async with self._use_conn(None) as c:
            cur = await c.execute(
                """UPDATE kb_workspace_exports
                   SET status = %s,
                       result_json = COALESCE(%s::jsonb, result_json),
                       error = %s,
                       target_artifact_id = COALESCE(%s, target_artifact_id)
                   WHERE id = %s AND status = 'pending'""",
                (status, _json(result_json) if result_json else None,
                 error, target_artifact_id, export_id),
            )
            return bool(cur.rowcount or 0)

    async def get_export_by_request(
        self, *, request_id: str,
    ) -> dict[str, Any] | None:
        """request_id 幂等读（Task 9 导出入口）：命中 succeeded → 服务层
        原样回放 result_json（duplicate）；pending → 409；failed → 复用该行
        重跑（reset_failed_export 回 pending）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                f"""SELECT {_EXPORT_COLUMNS} FROM kb_workspace_exports
                    WHERE request_id = %s""",
                (request_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def reset_failed_export(
        self, *, export_id: str, workspace_id: str,
    ) -> bool:
        """failed 行复用重跑（67号 Task 9 拍板）：状态回 pending 并清
        error/result_json/target_artifact_id——同 request_id 重试不撞
        UNIQUE，账记在同一行。守卫 WHERE status='failed' AND workspace_id
        ——并发重试只有一个赢（False 方=已被翻 pending，由服务层翻 409）；
        **workspace_id 必带**（审查 Medium-1：request_id 全局唯一，不带归属
        条件则异工作区可把他区 failed 行翻 pending 并借行执行自己的 items）。"""
        async with self._use_conn(None) as c:
            cur = await c.execute(
                """UPDATE kb_workspace_exports
                   SET status = 'pending', error = NULL, result_json = NULL,
                       target_artifact_id = NULL
                   WHERE id = %s AND workspace_id = %s
                     AND status = 'failed'""",
                (export_id, workspace_id),
            )
            return bool(cur.rowcount or 0)
