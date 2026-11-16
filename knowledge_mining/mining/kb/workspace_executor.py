# -*- coding: utf-8 -*-
"""KB 知识制品工作台执行引擎（67号工作台 MVP Task 6）。

职责：start_run（守卫+后台执行）/ _execute（拓扑逐节点执行：source 取
冻结 manifest、确定性算子经 workspace_operators.run_operator、llm 算子
（kind="llm"）分派 workspace_llm_operator.run_full——Task 7 接线）/ 分支
partial 语义 / 快照 current 输入复用 / stale 传播 / 节点 pending·stale
标志定点清除 / trial 试跑不落库（llm 算子走 run_trial）/ cancel / 崩溃
恢复 reconcile。

设计决策（docstring 即决策记录，编号供审查引用）：

1. **单写者**：run 生命周期（mark_* / update_progress / write_outputs /
   set_stale）只有本执行器调用；DB 层的幂等守卫（终态 rowcount=0 →
   False 不抛）兜底并发窗口。
2. **执行集 vs 冻结全图**：graph_snapshot 随 run 冻结**全图**（审计/回放
   口径），实际执行集按 scope 裁剪——workspace=全图；node=仅目标节点，
   其输入取上游端口 current 输出（不重跑上游——67号 §4.1）。subgraph 词
   表预留（迁移 012 CHECK），执行语义未接线，start 直接 Validation。
3. **输入解析序**：本 run 已写的新输出优先，否则上游端口 current 输出
   （节点 quality 记 reused_snapshot=true）；current 也无 → 节点失败
   「输入未执行」。current 快照在 _execute 开头一次读取——执行中其他
   run 的写入不可见（快照语义）。
4. **stale 传播与标志清除适用于一切成功节点**（source 与算子同权——计划
   文档把两条机制写在算子节点的 bullet 里，但 source 重跑后下游同样需要
   失效账，source 自身的 pending 也同样需要清除；只对算子生效会让
   source 的 pending 永远清不掉）：节点写完新输出后，①对本图（快照）
   中其全部严格后代端口的 current 行 set_stale(since=本 run)——拓扑序
   保证后代尚未在本 run 写输出，「不是本 run 刚写的」排除是防御性双保
   险（与 db_workspaces 模块 docstring 的两标志分工一致：上游已变的账
   记在输出行上）；②jsonb_set 定点清本节点 pending/stale。
5. **标志清除竞态取舍**：仅当 graph_revision 仍 == run 启动值才清——
   运行中被用户改图（put_graph 推了 revision）则整次跳过，标志留给下次
   PUT diff / 执行收敛：既不撞用户端的乐观锁，也绝不把新图的 pending 抹
   掉。启动值取守卫锁内记录的 revision，且**必须等于快照读取时的
   revision**——读取与落锁之间被并发 put_graph 推过版本的话（窗口毫秒
   级但存在），快照数组下标可能对不上新图，启动值置 -1（永不匹配 → 全
   程跳过清除）。两值相等 ⟹ 读取后零次图写入 ⟹ 当前图与快照逐字节一
   致（一切图写路径都经 put_graph 推 revision），jsonb_set 的数组下标按
   快照计算因此安全。
6. **崩溃恢复不做启动钩子**：mining 多 worker 场景无法区分「谁崩了」——
   启动钩子会把别的 worker 正在跑的 run 误判残留。改为
   reconcile_stale_running（running 且 started_at 超过 60 分钟 →
   failed(interrupted)）在每次 start_run 入口先调：新 run 前清残，简单
   可靠；60 分钟远超任何确定性算子图（≤200 节点、单算子纯内存变换）的
   执行时长。残 run 只认账不复活（67号：不自动续跑）。
   阈值 60 的来源（Task 7 审查 INFO-1）：llm 算子双轮等待的理论上限
   2×900s=30min 恰等旧阈值 30——正常等待中的 run 会被并发 start_run 入口
   的 reconcile 误判 interrupted；60min 留出整倍余量。
7. **llm 算子分派（Task 7）**：OPERATOR_DEFS[op].kind=="llm"（MVP 唯一
   llm_extract）的算子节点不走 run_operator 同步执行——_exec_operator
   分派 workspace_llm_operator.run_full（正式跑，domain=工作区 domain_id）
   / run_trial（试跑）；LlmTaskClient 由 executor 惰性构造（构造式照
   kb/deps._get_task_client：base_url=MiningConfig().llm_service_url；llm_factory
   注入面供测试换 fake），write_outputs 路径与确定性算子完全一致。
8. **取消点=节点边界**：每节点执行前读 run 行，status≠'running' 即停
   （覆盖 cancel 与 reconcile 误伤等一切外部终态写入）；已写输出保留，
   行留终态。queued 期间被取消 → mark_running 返回 False，执行根本不
   开始。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceConflictError,
    WorkspaceDB,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.db_workspace_runs import WorkspaceRunDB
from knowledge_mining.mining.kb.workspace_dataset import (
    MAX_SERIALIZED_BYTES,
    _canonical_json,  # 包内复用 canonical 形态（先例：workspace_sources 复用 artifact_import 私有四件套）
    dataset_hash,
    slice_for_trial,
    validate_dataset,
)
from knowledge_mining.mining.kb.workspace_llm_operator import (
    LlmOperatorContext,
    run_full as llm_run_full,
    run_trial as llm_run_trial,
)
from knowledge_mining.mining.kb.workspace_operators import (
    OPERATOR_DEFS,
    run_operator,
    topological_order,
)
from knowledge_mining.mining.kb.workspace_sources import materialize_source

__all__ = [
    "STALE_RUNNING_MINUTES",
    "WorkspaceExecutor",
    "reconcile_stale_running",
]

logger = logging.getLogger(__name__)

#: running 残留判定阈值（分钟）——见模块 docstring 决策 6。60 = llm 算子双轮
#: 等待理论上限（2×900s=30min）的两倍：恰等或低于真实执行时长的阈值会把
#: 等待中的 run 误判 interrupted（Task 7 审查 INFO-1）。
STALE_RUNNING_MINUTES = 60

#: 后台执行任务引用池（asyncio 只持弱引用，模块级集合防 GC；done 即弃）。
_RUNNING_TASKS: set["asyncio.Task[None]"] = set()

_TERMINAL_STATUSES = ("completed", "partial", "failed", "cancelled")


def _log_and_discard(task: "asyncio.Task[None]") -> None:
    """后台任务 done 回调（Task 7 质量收口 A）：弃引用前取一次 exception——
    消化 asyncio「exception was never retrieved」告警噪音；异常理论上不可达
    （_execute 全函数体引擎级兜底，见模块 docstring 决策），到即记 error。"""
    _RUNNING_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "workspace run 后台任务未捕获异常退出（run 应已被兜底落"
            " failed）: %s: %s", type(exc).__name__, exc)


async def reconcile_stale_running(pool: Any) -> int:
    """崩溃恢复（决策 6；Task 7 质量收口 B：SQL 下沉
    WorkspaceRunDB.reconcile_stale_running，本入口保留既有调用面——模块级
    函数 + STALE_RUNNING_MINUTES 常量权威仍在执行器层）。"""
    return await WorkspaceRunDB(pool).reconcile_stale_running(
        stale_minutes=STALE_RUNNING_MINUTES)


def _descendants(nodes: list[dict[str, Any]], node_id: str) -> set[str]:
    """快照图中 node_id 的全部严格后代（沿 inputs 边正向可达；图已保证
    无环，BFS 自然终止）。图 ≤200 节点，每次调用重建邻接表可接受。"""
    consumers: dict[str, set[str]] = {}
    for n in nodes:
        for ref in n.get("inputs") or []:
            producer = str(ref).split(":", 1)[0]
            consumers.setdefault(producer, set()).add(n["id"])
    seen: set[str] = set()
    queue: list[str] = [node_id]
    while queue:
        for consumer in consumers.get(queue.pop(), ()):
            if consumer not in seen:
                seen.add(consumer)
                queue.append(consumer)
    return seen


def _default_llm_client() -> Any:
    """LlmTaskClient 惰性构造（Task 7；构造式照 kb/deps._get_task_client：
    base_url 取 MiningConfig().llm_service_url——执行器不持有 FastAPI
    request，无法复用其模块级单例，进程内自持一实例跨 run 复用连接池、
    永不 close；延迟导入避免模块加载期拉起配置读取）。"""
    from knowledge_mining.mining.infra.llm_task_client import LlmTaskClient
    from knowledge_mining.mining.infra.mining_config import MiningConfig
    return LlmTaskClient(base_url=MiningConfig().llm_service_url)


class WorkspaceExecutor:
    """工作台执行引擎（行为规格与决策见模块 docstring）。

    构造入参与两仓储一致：row_factory=dict_row 的 psycopg
    AsyncConnectionPool。route/service 层（Task 8）只应触碰 start_run /
    get_status / cancel_run / trial_node 四个入口。
    """

    def __init__(self, pool: Any, *, llm_factory: Any = None) -> None:
        self._pool = pool
        self._wdb = WorkspaceDB(pool)
        self._rdb = WorkspaceRunDB(pool)
        # llm 算子客户端（决策 7）：llm_factory 注入面供测试换 fake；
        # 缺省惰性构造真 LlmTaskClient（首个 llm 节点执行时才建）。
        self._llm_factory = llm_factory
        self._llm_client: Any = None

    def _llm_ctx(self) -> LlmOperatorContext:
        """llm 算子执行上下文（客户端单实例缓存——httpx 连接池跨节点/跨
        run 复用）。"""
        if self._llm_client is None:
            factory = self._llm_factory or _default_llm_client
            self._llm_client = factory()
        return LlmOperatorContext(llm=self._llm_client)

    # ------------------------------------------------------------ 启动

    async def start_run(
        self, workspace_id: str, *, scope: str,
        target_node_id: str | None = None, created_by: str,
        actor_kind: str = "user", actor_id: str = "",
    ) -> str:
        """读工作区 → 裁剪执行集 → 守卫建 run → 后台执行 → 返回 run_id。

        - 入口先 reconcile（决策 6：新 run 前清 running 残留）。
        - 工作区不存在 → NotFound；归档/空图/scope 未接线/node 缺目标 →
          Validation；在飞（queued/running）→ WorkspaceConflictError
          （service 翻 409 run_in_progress）。
        - graph_hash = 图 dict 的 canonical sha256（与 dataset 契约同款
          canonical json——键序无关、紧凑、非 ASCII 原样）。
        """
        await reconcile_stale_running(self._pool)
        ws = await self._wdb.get_workspace(workspace_id=workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace_id}")
        if ws["status"] != "active":
            raise WorkspaceValidationError(
                "workspace is archived, cannot start run:"
                f" {workspace_id}")
        graph = ws["graph_json"]
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not nodes:
            raise WorkspaceValidationError(
                "workspace graph has no nodes, cannot start run")
        node_ids = {n["id"] for n in nodes}
        if scope == "node":
            if not target_node_id or target_node_id not in node_ids:
                raise WorkspaceValidationError(
                    "scope='node' requires an existing target_node_id:"
                    f" {target_node_id!r}")
            exec_set = {target_node_id}
        elif scope == "workspace":
            exec_set = set(node_ids)
        else:
            raise WorkspaceValidationError(
                f"scope 未接线（仅 workspace/node，subgraph 预留）: {scope!r}")

        async with self._pool.connection() as conn:
            out = await self._rdb.try_start_guard(
                conn, workspace_id, created_by,
                graph_snapshot=graph, graph_hash=dataset_hash(graph),
                scope=scope, target_node_id=target_node_id,
                batch_no=int(ws["batch_no"]), actor_kind=actor_kind,
                actor_id=actor_id,
            )
        if out["in_flight"] is not None:
            raise WorkspaceConflictError(
                "run in progress (workspace 单飞):"
                f" {out['in_flight']['id']}")
        run = out["run"]
        # 启动 revision（决策 5）：锁内记录值 == 快照读取值才可用于标志
        # 清除的下标守卫；读取与落锁之间被并发 put_graph 推过版本 → 置
        # -1（永不匹配，清除全程跳过），杜绝「快照下标 × 新图」错位清除。
        run_revision = int(run["graph_revision"])
        started_revision = (run_revision
                            if run_revision == int(ws["graph_revision"])
                            else -1)
        task = asyncio.create_task(
            self._execute(str(run["id"]), workspace_id, graph, exec_set,
                          started_revision, str(ws["domain_id"])))
        _RUNNING_TASKS.add(task)
        task.add_done_callback(_log_and_discard)
        return str(run["id"])

    # ------------------------------------------------------------ 后台执行

    async def _execute(
        self, run_id: str, workspace_id: str, graph: dict[str, Any],
        exec_set: set[str], started_revision: int, domain: str = "",
    ) -> None:
        """后台执行主体（单写者；取消点=节点边界，决策 8）。

        终态判定：执行集全成 → completed；部分成 → partial（失败清单在
        progress.node_errors）；全败 → failed（error 摘要带首错节点）。
        引擎级异常兜底 mark_failed **覆盖全函数体**（Task 7 质量收口 A：
        含 mark_running/list_sources/get_current_outputs 等前置 DB 调用与
        终态落账本身——后台 task 不许带异常死掉把 run 挂在 running；reconcile
        是 60 分钟级的最后防线，这里是即时的；mark_failed 自身再失败才交给
        reconcile 收账）。"""
        try:
            if not await self._rdb.mark_running(run_id):
                return  # queued 期间已被取消/判残：终态已定，不再执行
            nodes: list[dict[str, Any]] = graph["nodes"]
            by_id = {n["id"]: n for n in nodes}
            order = [nid for nid in topological_order(nodes)
                     if nid in exec_set]
            sources = {row["node_id"]: row for row in
                       await self._wdb.list_sources(
                           workspace_id=workspace_id)}
            current = await self._rdb.get_current_outputs(workspace_id)
            run_new: dict[str, dict[str, Any]] = {}  # "node:port" -> dataset
            node_errors: dict[str, str] = {}

            for i, nid in enumerate(order):
                # 取消点（决策 8）：一切非 running 外部终态写入即停
                row = await self._rdb.get_run(run_id)
                if row is None or row["status"] != "running":
                    return
                node = by_id[nid]
                failed_upstream = sorted(
                    ref.split(":", 1)[0]
                    for ref in node.get("inputs") or []
                    if ref.split(":", 1)[0] in node_errors)
                if failed_upstream:
                    # 级联标败：不重复执行失败分支的下游（B.3 完整性铁律）
                    node_errors[nid] = f"上游失败: {','.join(failed_upstream)}"
                else:
                    try:
                        if node["op"] == "source":
                            written = await self._exec_source(
                                run_id, workspace_id, node,
                                sources.get(nid))
                        else:
                            written = await self._exec_operator(
                                run_id, workspace_id, node, run_new,
                                current, domain=domain)
                        run_new.update(
                            {f"{nid}:{port}": ds
                             for port, ds in written.items()})
                        await self._after_node_success(
                            run_id, workspace_id, nid, nodes,
                            started_revision, run_new)
                    except WorkspaceValidationError as exc:
                        node_errors[nid] = str(exc)
                    except Exception as exc:  # 节点级兜底：不炸 run
                        node_errors[nid] = f"{type(exc).__name__}: {exc}"
                await self._rdb.update_progress(
                    run_id, done_node=nid,
                    current_node=(order[i + 1]
                                  if i + 1 < len(order) else None),
                    node_error=(nid, node_errors[nid])
                    if nid in node_errors else None)

            failed = [nid for nid in order if nid in node_errors]
            if not failed:
                await self._rdb.mark_completed(run_id)
            elif len(failed) == len(order):
                await self._rdb.mark_failed(
                    run_id,
                    error=f"全部 {len(order)} 个节点失败；"
                          f"首个错误[{failed[0]}]: {node_errors[failed[0]]}")
            else:
                await self._rdb.mark_partial(run_id)
        except Exception as exc:  # 引擎级兜底（覆盖全函数体）
            try:
                await self._rdb.mark_failed(
                    run_id,
                    error=f"executor error: {type(exc).__name__}: {exc}")
            except Exception:  # DB 也不可用：run 残 running 由 reconcile 收账
                pass

    async def _exec_source(
        self, run_id: str, workspace_id: str, node: dict[str, Any],
        source_row: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        """source 节点：冻结 manifest 取数（materialize 内含契约校验）→
        序列化预算 → 写全部声明端口（MVP 惯例单口 main；多口同数据集落多
        行——下游引用按图声明永远可解析）。返回 {port: dataset}。"""
        if source_row is None:
            raise WorkspaceValidationError(
                f"source 节点未接入来源（kb_workspace_sources 无行）:"
                f" {node['id']}")
        dataset = materialize_source(source_row)
        port_ids = [p["id"] for p in node.get("ports") or []]
        if not port_ids:
            raise WorkspaceValidationError(
                f"source 节点无声明端口: {node['id']}")
        size = len(_canonical_json(dataset).encode("utf-8"))
        if size > MAX_SERIALIZED_BYTES:
            raise WorkspaceValidationError(
                f"port {port_ids[0]}: 序列化超预算 {MAX_SERIALIZED_BYTES}"
                f" 字节（实际 {size} 字节）")
        quality = {"rows": len(dataset.get("rows", []))}
        hashes = {port: dataset_hash(dataset) for port in port_ids}
        outputs = {port: {"dataset": dataset, "quality": quality}
                   for port in port_ids}
        await self._rdb.write_outputs(
            None, run_id=run_id, workspace_id=workspace_id,
            node_id=str(node["id"]), outputs=outputs, hashes=hashes)
        return {port: dataset for port in port_ids}

    async def _exec_operator(
        self, run_id: str, workspace_id: str, node: dict[str, Any],
        run_new: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]], *, domain: str = "",
    ) -> dict[str, dict[str, Any]]:
        """算子节点（决策 7：确定性 + llm 统一入口）：输入解析（决策 3：
        本 run 新输出优先→current→失败）→ 按 OPERATOR_DEFS kind 分派——
        deterministic 走 run_operator 同步执行；llm 走
        workspace_llm_operator.run_full（domain=工作区 domain_id，asyncio
        侧由该模块自行 to_thread）→ 逐 port 契约校验+序列化预算 →
        write_outputs（两分支完全一致）。返回 {port: dataset}。"""
        inputs: list[dict[str, Any]] = []
        reused = False
        for ref in node.get("inputs") or []:
            if ref in run_new:
                inputs.append(run_new[ref])
            elif ref in current:
                inputs.append(current[ref]["content_inline"])
                reused = True
            else:
                raise WorkspaceValidationError(f"输入未执行: {ref}")
        op = str(node["op"])
        if OPERATOR_DEFS.get(op, {}).get("kind") == "llm":
            if len(inputs) != 1:
                raise WorkspaceValidationError(
                    f"operator {op} expects 1 input(s), got {len(inputs)}")
            outputs, quality = await llm_run_full(
                self._llm_ctx(), node.get("config"), inputs[0],
                domain=domain, run_id=run_id, node_id=str(node["id"]))
        else:
            outputs, quality = run_operator(op, node.get("config"), inputs)
        if reused:
            quality = {**quality, "reused_snapshot": True}
        prepared: dict[str, dict[str, Any]] = {}
        hashes: dict[str, str] = {}
        written: dict[str, dict[str, Any]] = {}
        for port, ds in outputs.items():
            validate_dataset(ds)
            size = len(_canonical_json(ds).encode("utf-8"))
            if size > MAX_SERIALIZED_BYTES:
                raise WorkspaceValidationError(
                    f"port {port}: 序列化超预算 {MAX_SERIALIZED_BYTES}"
                    f" 字节（实际 {size} 字节）")
            prepared[port] = {"dataset": ds, "quality": quality}
            hashes[port] = dataset_hash(ds)
            written[port] = ds
        await self._rdb.write_outputs(
            None, run_id=run_id, workspace_id=workspace_id,
            node_id=str(node["id"]), outputs=prepared, hashes=hashes)
        return written

    async def _after_node_success(
        self, run_id: str, workspace_id: str, node_id: str,
        nodes: list[dict[str, Any]], started_revision: int,
        run_new: dict[str, dict[str, Any]],
    ) -> None:
        """节点成功后的两笔账（决策 4/5）：
        ① stale 传播——严格后代端口的 current 行（排除本 run 刚写的）打
        stale_since_run=本 run；
        ② pending/stale 定点清——Task 7 质量收口 B：SQL 下沉
        WorkspaceDB.clear_node_flags（jsonb_set 只动该节点两键，WHERE
        graph_revision=启动值守卫——被改图则整次跳过；返回 False 不视为
        节点失败）。"""
        by_id = {n["id"]: n for n in nodes}
        node_ports = [
            (d, p["id"]) for d in _descendants(nodes, node_id)
            for p in by_id[d].get("ports") or []
            if f"{d}:{p['id']}" not in run_new]
        if node_ports:
            await self._rdb.set_stale(
                None, workspace_id=workspace_id, node_ports=node_ports,
                since_run_id=run_id)
        await self._wdb.clear_node_flags(
            workspace_id, node_id, started_revision)

    # ------------------------------------------------------------ 试跑

    async def trial_node(self, workspace_id: str, node_id: str,
                         ) -> dict[str, Any]:
        """试跑（67号 §4.1）：不建 run 不写库。输入取 current（无 →
        Validation「输入未执行」）→ slice_for_trial(3) → run_operator；
        llm 算子（决策 7）分派 workspace_llm_operator.run_trial（模块内自
        行切前 3 行、单轮 ≤60s、不重试——executor 传完整 current 数据集，
        不预切片，避免整表 deepcopy 后丢弃）。input_rows=第一输入切片后的
        实际行数（≥3 行的输入即 3）。"""
        ws = await self._wdb.get_workspace(workspace_id=workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace_id}")
        graph = ws["graph_json"] if isinstance(ws["graph_json"], dict) \
            else {}
        node = next((n for n in graph.get("nodes", [])
                     if isinstance(n, dict) and n.get("id") == node_id),
                    None)
        if node is None:
            raise WorkspaceValidationError(f"node not found: {node_id}")
        if node.get("op") == "source":
            raise WorkspaceValidationError(
                "source 节点无算子可试跑（试跑仅对算子节点）")
        current = await self._rdb.get_current_outputs(workspace_id)
        op = str(node.get("op"))
        if OPERATOR_DEFS.get(op, {}).get("kind") == "llm":
            refs = node.get("inputs") or []
            if len(refs) != 1:
                raise WorkspaceValidationError(
                    f"operator {op} expects 1 input(s), got {len(refs)}")
            if refs[0] not in current:
                raise WorkspaceValidationError(f"输入未执行: {refs[0]}")
            outputs, quality, trial_meta = await llm_run_trial(
                self._llm_ctx(), node.get("config"),
                current[refs[0]]["content_inline"],
                domain=str(ws["domain_id"]), node_id=node_id)
            return {"outputs": outputs, "quality": quality, "trial": True,
                    "input_rows": int(trial_meta.get("input_rows", 0))}
        inputs: list[dict[str, Any]] = []
        for ref in node.get("inputs") or []:
            if ref not in current:
                raise WorkspaceValidationError(f"输入未执行: {ref}")
            inputs.append(slice_for_trial(current[ref]["content_inline"]))
        outputs, quality = run_operator(op, node.get("config"), inputs)
        first_rows = inputs[0].get("rows") if inputs else []
        return {"outputs": outputs, "quality": quality, "trial": True,
                "input_rows": (len(first_rows)
                               if isinstance(first_rows, list) else 0)}

    # ------------------------------------------------------------ 读与取消

    async def cancel_run(self, run_id: str) -> bool:
        """幂等取消（mark_cancelled 终态守卫：已终态 → False 不抛）；后台
        任务在节点边界自停（决策 8），已写输出保留。"""
        return await self._rdb.mark_cancelled(run_id)

    async def get_status(self, workspace_id: str, run_id: str,
                         ) -> dict[str, Any]:
        """薄读合成（供路由层轮询，Task 8 接线）：run 行（剥大快照
        graph_snapshot_json）+ 节点态（当前图 pending/stale 标志 + current
        输出行的行数/指纹/stale 账）。run 不属于该工作区 → NotFound（反
        探测口径同仓储层）。"""
        run = await self._rdb.get_run(run_id)
        if run is None or run["workspace_id"] != workspace_id:
            raise WorkspaceNotFoundError(
                f"run not found in workspace: {run_id}")
        ws = await self._wdb.get_workspace(workspace_id=workspace_id)
        graph = ws["graph_json"] if ws and isinstance(ws["graph_json"],
                                                      dict) else {}
        outputs = await self._rdb.get_current_outputs(workspace_id)
        nodes_out: list[dict[str, Any]] = []
        for n in graph.get("nodes", []):
            nid = str(n.get("id"))
            ports: dict[str, Any] = {}
            for key, row in outputs.items():
                p_node, _, p_port = key.partition(":")
                if p_node == nid:
                    ports[p_port] = {
                        "row_count": row["row_count"],
                        "content_hash": row["content_hash"],
                        "stale": row["stale_since_run"] is not None,
                        "stale_since_run": row["stale_since_run"],
                    }
            nodes_out.append({
                "id": nid, "op": n.get("op"), "name": n.get("name", ""),
                "pending": bool(n.get("pending")),
                "stale": bool(n.get("stale")),
                "outputs": ports,
            })
        run_slim = {k: v for k, v in run.items()
                    if k != "graph_snapshot_json"}
        return {"run": run_slim, "nodes": nodes_out}
