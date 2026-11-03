"""Wiki 制品编译管线主体（63号 §四 / 批次3 Task 3）—— CompileService。

六步管线的 ②③④⑤⑥ 落点（①绑定在 WikiService.sources）：

    start → [后台 prepare：取材合并→指纹落库→聚类→目录 LLM]
        → awaiting_toc（人工确认步——目录错全书废，DeepWiki 最大实证）
    confirm_toc → [后台 writing：25 页/批簇级写作（批间约束注入）
        → 每批后全量链接重算 → 页级引用校验（确定性）
        → 质量 LLM 一次 → 矛盾/待解/术语专页 → completed]

关键口径（批次3 决策1-7）：
- **状态机入 batches.details**（增量合并写 + state 守卫——cancel 抢先不被
  覆写）；row_count 在 completed 时原子落页数。
- **generation_client 是 GenerationClient 协议形态的同步阻塞客户端**
  （生产=LLMServiceAsyncGenerationClient，任务通道+退避重试）；每次调用包
  ``await asyncio.to_thread``（同批次2 embedding 口径——DB 调用留主 loop）。
- **幂等键即重跑稳定器**（决策4）：``_generation_key(kind, prompt_version,
  expected_output_type, payload)`` 四参——prompt 版本冻结在
  wiki_compile_prompts（升级 prompt 必须升版本，详见该模块 docstring）。
- **origin 保护**（§3.2）：写前查 rev0 同键页，manual → 跳过+conflicts 账；
  machine → 覆盖（重编译语义）。编译写页**绕过 WikiService**（其 origin 恒
  manual）直接走 wdb，origin='machine'。
- **_PREPARE_CACHE 是进程内存**（prepare→writing 取材账）：进程重启即丢，
  awaiting_toc 批次作废——status 显示 failed 提示重编（MVP 可接受偏差）。
- **validate_pages_for_publish（决策7）** 在 wiki_compile_pages（本模块
  转出，WikiLifecycle.publish 复用；表格 publish 路径不受影响）。

800 行铁律拆分：prompts/版本 → wiki_compile_prompts；确定性加工函数与发布
校验 → wiki_compile_pages；本文件只留状态机与 LLM 编排。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from knowledge_mining.mining.kb.db_wiki import WikiError, normalize_page_key
from knowledge_mining.mining.kb.services.artifact_service import (
    ROLE_DESIGN,
    _check_freshness,
)
from knowledge_mining.mining.kb.services.wiki_compile_pages import (
    blockquote_of,
    constraint_text,
    expand_toc_pages,
    extract_src_refs,
    group_provenance,
    normalize_toc,
    page_flags,
    tags_from_label,
    toc_digest_text,
    toc_page_count,
    validate_pages_for_publish,
    write_derived_pages,
    write_machine_page,
)
from knowledge_mining.mining.kb.services.wiki_compile_prompts import (
    MAX_PAGES,
    PAGE_PROMPT_VERSION,
    PAGE_SYSTEM_HEAD,
    QUALITY_PROMPT_VERSION,
    QUALITY_SYSTEM,
    TOC_PROMPT_VERSION,
    TOC_RETRY_NOTE,
    TOC_SYSTEM,
)
from knowledge_mining.mining.kb.wiki_clustering import (
    Cluster,
    build_cluster_context,
    cluster_units,
)
from knowledge_mining.mining.retrieval_projection.llm_generation import (
    _generation_key,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CompileService", "validate_pages_for_publish", "drop_prepare_cache",
    "TOC_PROMPT_VERSION", "PAGE_PROMPT_VERSION", "QUALITY_PROMPT_VERSION",
    "MAX_PAGES", "MAX_UNITS",
]

#: 取材量级护栏（决策2：Σ scope 单元 > 30000 → 422，防 40 万向量库 OOM）。
MAX_UNITS = 30000
#: 写作批大小（63号 §4.6：25 页/批批间约束注入）。
PAGES_PER_BATCH = 25
#: 目录 LLM 输入取按成员数 top 簇数（决策3：输入有界）。
TOC_TOP_CLUSTERS = 150
#: 专页数量上限在 wiki_compile_pages（GLOSSARY_CAP/OPEN_QUESTION_CAP——模板
#: 生成与上限同域）。
#: 正文零合法引用时 provenance 回退簇成员数（决策6：取材完整）。
PROVENANCE_FALLBACK_IDS = 8
#: 簇代表句截断（目录 LLM 输入）与目录 top 目录数。
_REPRESENTATIVE_CHARS = 120
_TOP_DIRS = 3

#: 在飞状态集（start 守卫 / cancel 前置共用）。
_INFLIGHT_STATES = ("preparing", "awaiting_toc", "writing")

#: 后台编译任务强引用集（事件循环对 Task 只持弱引用——照批次2 _EMBED_TASKS /
#: onenet._bg_tasks 模式；done 回调释放）。在飞防重入由守卫事务权威承担
#: （审查 Minor：原 in-flight 键集合只写不读，死状态已删）。
_COMPILE_TASKS: set["asyncio.Task[None]"] = set()

#: prepare 产物缓存（artifact_id → 取材账，值携带 batch_id 归属——释放时
#: 归属判断防僵尸任务抹掉新轮缓存，审查 F1）。**进程重启即丢**：awaiting_toc
#: 批次作废（confirm 后 writing 首步发现缺缓存 → failed 提示重编）。写库持久
#: 30k 单元摘要不值当（MVP 裁定，见模块 docstring）。制品硬删路径经
#: drop_prepare_cache 兜底清理（审查 I-2）。
_PREPARE_CACHE: dict[str, "PreparedCorpus"] = {}


def drop_prepare_cache(artifact_id: str) -> None:
    """制品级联硬删路径的缓存清理（审查 I-2②）：无归属判断——制品已删，
    该 artifact 的一切编译缓存都是孤儿。幂等、纯内存操作（调用方包 try/except
    尽力而为，失败仅日志——删除主流程不受缓存清理影响）。"""
    _PREPARE_CACHE.pop(str(artifact_id), None)


@dataclass
class PreparedCorpus:
    """prepare 阶段产物（writing 阶段的取材账，内存缓存）。

    batch_id 是归属账（F1）：cancel/完结/失败释放缓存前比对该键——新一轮
    prepare 已覆盖同 artifact_id 缓存时旧任务不得动它。units 为摘要行
    （content_text 截 800 字——写作素材上限即 build_cluster_context 的
    per_unit_chars 口径）；clusters 保留 Cluster 全量（成员映射+digest
    取材）；glossary/open_questions 来自目录 LLM（专页生成源）。
    """

    batch_id: str
    units: list[dict[str, Any]]
    clusters: tuple[Cluster, ...]
    stats: dict[str, Any]
    glossary: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]


class CompileService:
    """编译管线编排（四入口 start/confirm_toc/status/cancel + 两后台阶段）。

    构造 ``(wdb, inputs_db, generation_client, wiki_service)``：wiki_service
    持有的 ArtifactService 提供角色前置（ROLE_DESIGN），其 _recompute_all_links
    提供全量链接重算（本服务不重复实现）。generation_client 为同步阻塞
    GenerationClient 协议实现（每次调用 asyncio.to_thread）。
    """

    def __init__(self, wdb: Any, inputs_db: Any, generation_client: Any,
                 wiki_service: Any) -> None:
        self._wdb = wdb
        self._inputs = inputs_db
        self._gen = generation_client
        self._wiki = wiki_service

    # ------------------------------------------------------ 公开入口

    async def start(
        self, user: dict[str, Any], artifact_id: str, *,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """启动编译：ROLE_DESIGN + 新鲜度 → 守卫事务（查在飞/护栏/建批——
        INSERT 原子携带 state=preparing，审查 I-1 消除裸窗口 TOCTOU）→ 后台
        _run_prepare。返回 {batch_id, state}。

        在飞判定**只在守卫事务**（审查 F1）：进程内集合不作前置——cancel 后
        立刻重编不被批边界徘徊的旧任务误拒（旧批 DB 态已 cancelled，守卫
        放行才是正确语义）。"""
        await self._gate(user, artifact_id, expected_updated_at)
        guard = await self._inputs.try_start_guard(
            artifact_id, str(user["id"]), max_units=MAX_UNITS)
        if guard.get("in_flight") is not None:
            raise WikiError("compile_in_progress")
        if not guard.get("sources"):
            raise WikiError("no_sources")
        batch = guard.get("batch")
        if batch is None:
            # 守卫内已过在飞/源检查，建批为空只剩护栏超限一种可能
            raise WikiError(
                f"too_many_units:{int(guard.get('unit_total') or 0)}")
        batch_id = str(batch["batch_id"])
        self._spawn(artifact_id, self._run_prepare(batch_id, artifact_id))
        return {"batch_id": batch_id, "state": "preparing"}

    async def confirm_toc(
        self, user: dict[str, Any], artifact_id: str, toc: Any, *,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """确认目录（可编辑版）：必须 awaiting_toc；展开页数 > MAX_PAGES →
        422 too_many_pages（杜绝「确认了却静默截断」）；守卫写 toc+state=
        writing → 后台 _run_writing。"""
        await self._gate(user, artifact_id, expected_updated_at)
        batch = await self._inputs.latest_compile_batch(artifact_id)
        if batch is None:
            raise WikiError("compile_not_found")
        batch_id = str(batch["batch_id"])
        details = batch.get("details") or {}
        if str(details.get("state") or "") != "awaiting_toc":
            raise WikiError("invalid_compile_state")
        normalized = normalize_toc({
            "toc": toc,
            "glossary": details.get("glossary") or [],
            "open_questions": details.get("open_questions") or [],
        })
        if normalized is None:
            raise WikiError("invalid_toc")
        page_total = toc_page_count(normalized["toc"])
        if page_total == 0:
            # 零页目录（节全空 hints）——writing 展开无页直接完结无意义，
            # 与形状不符同走 422（审查 Minor）
            raise WikiError("invalid_toc")
        if page_total > MAX_PAGES:
            raise WikiError("too_many_pages")
        ok = await self._inputs.update_batch_details(
            batch_id, {"toc": normalized["toc"], "state": "writing"},
            guard_states=["awaiting_toc"])
        if not ok:
            raise WikiError("invalid_compile_state")
        self._spawn(artifact_id, self._run_writing(batch_id, artifact_id))
        return {"state": "writing"}

    async def status(
        self, user: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        """编译进度（ROLE_DESIGN——编译过程含未发布内容，runtime 不可见）：
        {batch_id, state, progress, stats, conflicts, toc(awaiting_toc 时),
        error}。"""
        await self._gate(user, artifact_id)
        batch = await self._inputs.latest_compile_batch(artifact_id)
        if batch is None:
            raise WikiError("compile_not_found")
        details = batch.get("details") or {}
        out: dict[str, Any] = {
            "batch_id": str(batch.get("batch_id") or ""),
            "state": str(details.get("state") or ""),
            "progress": details.get("progress")
            or {"batch": "0/0", "pages_done": 0},
            "stats": details.get("stats") or {},
            "conflicts": details.get("conflicts") or [],
            "error": details.get("error"),
        }
        if out["state"] == "awaiting_toc":
            out["toc"] = details.get("toc") or []
        return out

    async def cancel(
        self, user: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        """取消：在飞态 → 守卫写 {state: cancelled}（审查 F2：守卫=在飞态
        白名单——读写间隙竞态翻到 completed/failed 时不覆写）；同时释放本批
        的 _PREPARE_CACHE（审查 F7：awaiting_toc 取消即弃语料账，带 batch_id
        归属判断——新一轮已覆盖缓存则不动）。writing 后台任务在批边界按
        **本批** batch_id 读 state 停止（F1）。终态 → 409
        invalid_compile_state。不做乐观锁校验——取消是安全操作，且编译页写
        持续推令牌，校验必假阳性。"""
        await self._gate(user, artifact_id)
        batch = await self._inputs.latest_compile_batch(artifact_id)
        if batch is None:
            raise WikiError("compile_not_found")
        batch_id = str(batch["batch_id"])
        state = str((batch.get("details") or {}).get("state") or "")
        if state not in _INFLIGHT_STATES:
            raise WikiError("invalid_compile_state")
        ok = await self._inputs.update_batch_details(
            batch_id, {"state": "cancelled"},
            guard_states=list(_INFLIGHT_STATES))
        if not ok:
            # 读写间隙状态已离在飞集（终态竞态）——对用户语义同 invalid
            raise WikiError("invalid_compile_state")
        self._pop_cache_if_mine(artifact_id, batch_id)
        return {"state": "cancelled"}

    # ------------------------------------------------------ prepare 阶段

    async def _run_prepare(self, batch_id: str, artifact_id: str) -> None:
        """后台①：取材合并 → 指纹同参落库 → 聚类 → 目录 LLM（一次+至多一次
        修复重试）→ 守卫写 awaiting_toc。"""
        try:
            sources = await self._inputs.list_sources(artifact_id)
            units: list[dict[str, Any]] = []
            source_fingerprint: dict[str, Any] = {}
            for src in sources:
                kb_id = str(src["kb_id"])
                dirs = [str(d) for d in
                        ((src.get("scope") or {}).get("directories") or [])]
                units.extend(await self._inputs.collect_units(kb_id, dirs))
                # 同参铁律（T2 契约）：落库指纹必须与 dirty 对比同参——scope
                # 外变化不误脏、scope 内变化必脏
                fingerprint = await self._inputs.current_fingerprint(
                    kb_id, dirs)
                await self._inputs.update_fingerprint(
                    artifact_id, kb_id, fingerprint)
                source_fingerprint[kb_id] = fingerprint
            if not units:
                await self._fail(batch_id, "no_units", ("preparing",))
                return
            result = cluster_units(units)
            units_lookup = {
                str(u["representation_id"]): u for u in units}
            toc_obj = await self._toc_llm(artifact_id, result, units_lookup)
            if toc_obj is None:
                await self._fail(batch_id, "toc_generation_failed",
                                 ("preparing",))
                return
            stats = {
                "unit_count": int(result.stats.get("unit_count") or 0),
                "cluster_count": int(result.stats.get("cluster_count") or 0),
                "embedded_ratio": float(result.stats.get("embedded_ratio")
                                        or 0.0),
            }
            _PREPARE_CACHE[artifact_id] = PreparedCorpus(
                batch_id=batch_id,
                units=[{
                    "representation_id": str(u["representation_id"]),
                    "kb_id": str(u.get("kb_id") or ""),
                    "snapshot_id": str(u.get("snapshot_id") or ""),
                    "content_text": str(u.get("content_text") or "")[:800],
                    "directory_path": str(u.get("directory_path") or ""),
                } for u in units],
                clusters=result.clusters,
                stats=dict(stats),
                glossary=toc_obj["glossary"],
                open_questions=toc_obj["open_questions"],
            )
            ok = await self._inputs.update_batch_details(batch_id, {
                "state": "awaiting_toc",
                "toc": toc_obj["toc"],
                "glossary": toc_obj["glossary"],
                "open_questions": toc_obj["open_questions"],
                "clusters_digest": [
                    {"cluster_id": c.cluster_id,
                     "label": c.heuristic_label,
                     "members": len(c.member_ids)}
                    for c in result.clusters
                ],
                "stats": stats,
                "source_fingerprint": source_fingerprint,
                "prompt_version": {
                    "toc": TOC_PROMPT_VERSION,
                    "page": PAGE_PROMPT_VERSION,
                    "quality": QUALITY_PROMPT_VERSION,
                },
            }, guard_states=["preparing"])
            if not ok:
                # 被 cancel 抢先：缓存随本批作废释放（归属判断——新一轮已
                # 覆盖则不动，审查 F1）
                self._pop_cache_if_mine(artifact_id, batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("wiki 编译 prepare 失败 artifact=%s", artifact_id)
            await self._fail(batch_id, f"prepare_failed:{type(exc).__name__}",
                             ("preparing", "writing"))
            self._pop_cache_if_mine(artifact_id, batch_id)

    async def _toc_llm(
        self, artifact_id: str, result: Any, units_lookup: dict[str, Any],
    ) -> dict[str, Any] | None:
        """目录 LLM：一次生成 + 至多一次修复重试（页数超限/解析失败重发带
        压缩注记——payload 含注记故幂等键不同，不命中旧缓存）。返回归一对象
        或 None（两次均不可用）。"""
        payload = {
            "clusters": self._toc_cluster_lines(result, units_lookup),
            "page_cap": MAX_PAGES,
        }
        for attempt in range(2):
            note = "" if attempt == 0 else "\n\n" + TOC_RETRY_NOTE
            idem = _generation_key(
                "wiki-toc", TOC_PROMPT_VERSION, "json_object",
                {"payload": payload, "retry_note": note},
            )
            messages = [
                {"role": "system", "content": TOC_SYSTEM},
                {"role": "user",
                 "content": "\n".join(payload["clusters"]) + note},
            ]
            try:
                raw = await asyncio.to_thread(
                    self._gen.execute, messages,
                    expected_output_type="json_object",
                    idempotency_key=idem, pipeline_stage="wiki_compile",
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "wiki TOC 生成调用失败（attempt=%s/2）artifact=%s",
                    attempt + 1, artifact_id)
                raw = None
            parsed = normalize_toc(raw)
            if parsed is not None \
                    and toc_page_count(parsed["toc"]) <= MAX_PAGES:
                return parsed
        return None

    @staticmethod
    def _toc_cluster_lines(
        result: Any, units_lookup: dict[str, Any],
    ) -> list[str]:
        """目录 LLM 输入：按成员数 top-TOC_TOP_CLUSTERS 簇（id/标签/代表句/
        目录 top3）+ 折叠统计行（决策3：输入有界）。"""
        ordered = sorted(
            result.clusters,
            key=lambda c: (-len(c.member_ids), c.cluster_id))
        lines: list[str] = []
        for cluster in ordered[:TOC_TOP_CLUSTERS]:
            rep = ""
            if cluster.digest_members:
                unit = units_lookup.get(cluster.digest_members[0])
                rep = str((unit or {}).get("content_text") or "")[
                    :_REPRESENTATIVE_CHARS]
            dir_counts: dict[str, int] = {}
            for rid in cluster.member_ids:
                dp = str((units_lookup.get(rid) or {}).get(
                    "directory_path") or "")
                dir_counts[dp] = dir_counts.get(dp, 0) + 1
            top_dirs = [d for d, _ in sorted(
                dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                [:_TOP_DIRS]]
            lines.append(
                f"- 簇 {cluster.cluster_id}（{len(cluster.member_ids)} 单元）："
                f"{cluster.heuristic_label}｜代表句：{rep}"
                f"｜目录：{'、'.join(top_dirs) or '（根）'}")
        folded = ordered[TOC_TOP_CLUSTERS:]
        if folded:
            folded_units = sum(len(c.member_ids) for c in folded)
            lines.append(f"-（另有 {len(folded)} 个小簇共 {folded_units} 单元，"
                         "已折叠，勿为其生成页面）")
        return lines

    # ------------------------------------------------------ writing 阶段

    async def _run_writing(self, batch_id: str, artifact_id: str) -> None:
        """后台②：确认目录展开 → 25 页/批簇级写作（批间约束注入）→ 每批后
        全量链接重算 → 页级引用校验 → 质量 LLM 一次 → 专页 → 守卫完结。

        停止位一律按**本批** batch_id 读 state（审查 F1——僵尸任务查 latest
        会误读新轮的在飞态）：本批 state ≠ writing 即停（cancel 抢先/任何
        状态漂移），缓存释放带归属判断（新一轮已覆盖则不动）。"""
        try:
            prepared = _PREPARE_CACHE.get(artifact_id)
            batch = await self._inputs.batch_state(batch_id)
            if batch is None:
                # 批行已消失（制品级联硬删）——孤儿缓存一并释放（审查 I-2①）
                self._pop_cache_if_mine(artifact_id, batch_id)
                return
            details = batch.get("details") or {}
            user = str(batch.get("created_by") or "wiki_compile")
            if str(details.get("state") or "") != "writing":
                # 启动即非在飞（cancel 在 spawn 后抢先）——静默停
                self._pop_cache_if_mine(artifact_id, batch_id)
                return
            if prepared is None or prepared.batch_id != batch_id:
                await self._fail(
                    batch_id, "prepare cache lost——服务可能重启，请重新编译",
                    ("writing",))
                return
            # 编译产物须可发布 + 重编译检出已发布人工页参与 origin 保护
            await self._wdb.ensure_draft(artifact_id)

            pages = expand_toc_pages(details.get("toc") or [])
            dropped_pages = max(0, len(pages) - MAX_PAGES)
            if dropped_pages:
                pages = pages[:MAX_PAGES]   # 防御性截断（confirm 已拦 >200）
            units_lookup = {
                str(u["representation_id"]): u for u in prepared.units}
            clusters_by_id = {c.cluster_id: c for c in prepared.clusters}
            digest = toc_digest_text(details.get("toc") or [])
            total_batches = max(
                1, (len(pages) + PAGES_PER_BATCH - 1) // PAGES_PER_BATCH)

            stats: dict[str, Any] = {
                "pages_written": 0, "failed_pages": 0,
                "dropped_pages": dropped_pages, "broken_links": 0,
                "dangling_pages": 0,
            }
            conflicts: list[dict[str, Any]] = []
            written: list[dict[str, Any]] = []
            provenance_by_key: dict[str, list[Any]] = {}
            constraint = ""

            for batch_idx in range(total_batches):
                # 批边界停止位（决策1+F1：本批 state 非 writing 即停——cancel
                # 抢先 / 僵尸批次的终态都在此感知）
                own = await self._inputs.batch_state(batch_id)
                if own is None or str(((own.get("details") or {})
                                       .get("state") or "")) != "writing":
                    self._pop_cache_if_mine(artifact_id, batch_id)
                    return
                chunk = pages[batch_idx * PAGES_PER_BATCH:
                              (batch_idx + 1) * PAGES_PER_BATCH]
                for page in chunk:
                    await self._write_one_topic_page(
                        artifact_id, page, pages, digest,
                        constraint, units_lookup,
                        clusters_by_id, user, stats, conflicts, written,
                        provenance_by_key)
                # 每批后：全量链接重算 + 本批断链注入下批约束（§4.2 ④）
                await self._wiki._recompute_all_links(artifact_id)
                edges = await self._wdb.list_links(artifact_id, revision=0)
                broken = [e for e in edges
                          if str(e.get("status")) == "broken"]
                stats["broken_links"] = len(broken)
                constraint = constraint_text(broken)
                await self._inputs.update_batch_details(batch_id, {
                    "progress": {
                        "batch": f"{batch_idx + 1}/{total_batches}",
                        "pages_done": int(stats["pages_written"]),
                    },
                    "stats": dict(stats),
                    "conflicts": [dict(c) for c in conflicts],
                })

            # 末批复查（审查 F8）：页写完到质量调用之间的窗口里 cancel 落地
            # → 跳过质量+专页直接停（不落 completed/row_count）。
            own = await self._inputs.batch_state(batch_id)
            if own is None or str(((own.get("details") or {})
                                   .get("state") or "")) != "writing":
                self._pop_cache_if_mine(artifact_id, batch_id)
                return

            # 质量 LLM（一次）+ 专页生成（模板在 wiki_compile_pages）
            quality = await self._quality_llm(artifact_id, written)
            derived = await write_derived_pages(
                self._wdb, artifact_id, user, quality,
                prepared.glossary, prepared.open_questions,
                provenance_by_key, written)
            stats.update(derived)
            stats["conflicts_n"] = len(conflicts)

            await self._inputs.complete_batch(
                batch_id,
                {
                    "state": "completed",
                    "stats": {**prepared.stats, **stats},
                    "progress": {
                        "batch": f"{total_batches}/{total_batches}",
                        "pages_done": int(stats["pages_written"]),
                    },
                    "conflicts": [dict(c) for c in conflicts],
                },
                row_count=int(stats["pages_written"]))
            self._pop_cache_if_mine(artifact_id, batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("wiki 编译 writing 失败 artifact=%s", artifact_id)
            await self._fail(batch_id,
                             f"writing_failed:{type(exc).__name__}",
                             ("preparing", "writing"))
            self._pop_cache_if_mine(artifact_id, batch_id)

    async def _write_one_topic_page(
        self, artifact_id: str, page: dict[str, Any],
        pages: list[dict[str, Any]], digest: str, constraint: str,
        units_lookup: dict[str, Any],
        clusters_by_id: dict[str, Cluster], user: str,
        stats: dict[str, Any], conflicts: list[dict[str, Any]],
        written: list[dict[str, Any]],
        provenance_by_key: dict[str, list[Any]],
    ) -> None:
        """单页写作子步（页生成→取材账→flags→origin 保护写），失败页跳过
        计数。参数多但均为热路径共享只读账——拆方法保 _run_writing 主干可读。"""
        try:
            key = normalize_page_key(str(page["title"]))
        except WikiError:
            stats["failed_pages"] += 1
            return
        try:
            body = await self._generate_page(
                artifact_id, page, digest, constraint, units_lookup,
                clusters_by_id,
                [normalize_page_key(p["title"]) for p in pages
                 if p is not page])
        except Exception:  # noqa: BLE001
            # 单页终态失败跳过计数（llm_service 任务通道内部已 3 次退避重试）
            stats["failed_pages"] += 1
            logger.warning("wiki 页写作失败（跳过）artifact=%s title=%s",
                           artifact_id, page.get("title"))
            return
        member_units, member_ids, tags = self._page_members(
            page, clusters_by_id, units_lookup)
        cited = extract_src_refs(str(body))
        valid_cited = [r for r in cited if r in member_ids]
        unit_ids = (valid_cited or member_ids[:PROVENANCE_FALLBACK_IDS])
        provenance = group_provenance(unit_ids, member_units)
        # 悬空判定口径=页簇成员并集（审查 Minor：与 provenance 记账同口径，
        # 跨簇引用编译期即捕获——「编译干净发布却 blocking」接缝消除）
        flags = page_flags(
            str(page.get("page_type") or "topic"),
            provenance, str(body), set(member_ids))
        if flags.get("dangling_refs"):
            stats["dangling_pages"] += 1
        outcome = await write_machine_page(
            self._wdb, artifact_id, page_key=key, title=str(page["title"]),
            page_type=str(page.get("page_type") or "topic"),
            frontmatter={
                "type": page.get("page_type") or "topic",
                "aliases": [str(page["title"])],
                "tags": tags,
            },
            body_md=str(body), provenance=provenance, flags=flags,
            ordinal=int(page["ordinal"]), user=user,
        )
        if outcome == "conflict":
            existing = await self._wdb.get_page(artifact_id, 0, key)
            conflicts.append({
                "page_key": key,
                "manual_title": str((existing or {}).get("title") or ""),
                "machine_title": str(page["title"]),
            })
            return
        stats["pages_written"] += 1
        provenance_by_key[key] = provenance
        written.append({
            "page_key": key, "title": str(page["title"]),
            "summary": blockquote_of(str(body)),
        })

    @staticmethod
    def _page_members(
        page: dict[str, Any], clusters_by_id: dict[str, Cluster],
        units_lookup: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
        """页的取材账：簇成员 rid→unit 映射、保序成员 id 清单、tags（簇标签
        词——多簇取首簇）。"""
        member_units: dict[str, dict[str, Any]] = {}
        member_ids: list[str] = []
        tags: list[str] = tags_from_label("", str(page.get("title") or ""))
        for cid in page.get("cluster_ids") or []:
            cluster = clusters_by_id.get(str(cid))
            if cluster is None:
                continue
            if not member_ids:
                tags = tags_from_label(
                    cluster.heuristic_label, str(page.get("title") or ""))
            for rid in cluster.member_ids:
                if rid in member_units:
                    continue
                unit = units_lookup.get(rid)
                if unit is not None:
                    member_units[rid] = unit
                    member_ids.append(rid)
        return member_units, member_ids, tags

    async def _generate_page(
        self, artifact_id: str, page: dict[str, Any], digest: str,
        constraint: str, units_lookup: dict[str, Any],
        clusters_by_id: dict[str, Cluster], other_keys: list[str],
    ) -> str:
        """单页 LLM 写作：system=四节模板+全局目录+可用页键+批间约束；user=
        页参数+簇取材素材（build_cluster_context 12×800 字/簇）。幂等键含
        全部有效载荷（约束变化 → 新键，同语料重跑命中缓存）。"""
        contexts: list[str] = []
        for cid in page.get("cluster_ids") or []:
            cluster = clusters_by_id.get(str(cid))
            if cluster is not None:
                contexts.append(
                    build_cluster_context(cluster, units_lookup))
        context_text = "\n\n".join(c for c in contexts if c)
        payload = {
            "title": str(page["title"]),
            "page_type": str(page.get("page_type") or "topic"),
            "toc": digest, "context": context_text,
            "page_keys": other_keys, "constraints": constraint,
        }
        system = PAGE_SYSTEM_HEAD + "\n\n全局目录：\n" + digest
        if other_keys:
            system += ("\n\n可用页键清单（相关页面双链只准用这些键）：\n"
                       + "、".join(other_keys))
        if constraint:
            system += "\n\n批间约束（前批产生的断链，避免重蹈）：\n" + constraint
        user_content = (
            f"页面标题：{page['title']}\n页面类型："
            f"{page.get('page_type') or 'topic'}\n\n"
            "取材素材（引用只准用其中 [[src:]] 标记的 unit id）：\n"
            + (context_text or "（无素材——只写目录性概述，不编造引用）")
        )
        idem = _generation_key(
            "wiki-page", PAGE_PROMPT_VERSION, "text", payload)
        return str(await asyncio.to_thread(
            self._gen.execute,
            [{"role": "system", "content": system},
             {"role": "user", "content": user_content}],
            expected_output_type="text",
            idempotency_key=idem, pipeline_stage="wiki_compile",
        ))

    async def _quality_llm(
        self, artifact_id: str, written: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """质量 LLM（一次）：全部已写页 {page_key,title,summary} → 矛盾/缺口。
        失败降级为空结果（质量页缺失不阻断整本完结）。"""
        if not written:
            return {"contradictions": [], "gaps": []}
        payload = [
            {"page_key": w["page_key"], "title": w["title"],
             "summary": w.get("summary") or ""}
            for w in written
        ]
        idem = _generation_key(
            "wiki-quality", QUALITY_PROMPT_VERSION, "json_object", payload)
        try:
            raw = await asyncio.to_thread(
                self._gen.execute,
                [{"role": "system", "content": QUALITY_SYSTEM},
                 {"role": "user",
                  "content": "已写页清单：\n"
                  + "\n".join(
                      f"- {w['page_key']}｜{w['title']}｜{w.get('summary')}"
                      for w in payload)}],
                expected_output_type="json_object",
                idempotency_key=idem, pipeline_stage="wiki_compile",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "wiki 质量审校调用失败（降级为空，不阻断完结）artifact=%s",
                artifact_id)
            return {"contradictions": [], "gaps": []}
        if not isinstance(raw, dict):
            return {"contradictions": [], "gaps": []}
        return {
            "contradictions": raw.get("contradictions")
            if isinstance(raw.get("contradictions"), list) else [],
            "gaps": raw.get("gaps")
            if isinstance(raw.get("gaps"), list) else [],
        }

    # ------------------------------------------------------ 内部共用

    async def _gate(
        self, user: dict[str, Any], artifact_id: str,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """ROLE_DESIGN + 可选新鲜度前置（复用 WikiService 持有的
        ArtifactService——本服务不另建权限面；status 也是 design-only：编译
        过程含未发布内容）。"""
        artifact, _ = await self._wiki._artifacts._assert_role(
            user, artifact_id, *ROLE_DESIGN)
        if expected_updated_at is not None:
            _check_freshness(artifact, expected_updated_at)
        return artifact

    def _spawn(self, artifact_id: str, coro: Any) -> None:
        """后台任务簿记：强引用集（事件循环对 Task 只持弱引用——照批次2
        _spawn_embed 模式；done 回调释放引用）。防重入由守卫事务权威承担。"""
        task = asyncio.create_task(coro)
        _COMPILE_TASKS.add(task)
        task.add_done_callback(_COMPILE_TASKS.discard)

    @staticmethod
    def _pop_cache_if_mine(artifact_id: str, batch_id: str) -> None:
        """释放 _PREPARE_CACHE（带归属判断，审查 F1）：缓存条目携带 batch_id
        ——仅当仍是本批的缓存才 pop；新一轮 prepare 已覆盖时（僵尸任务退出
        路径）不动新轮的账。"""
        cached = _PREPARE_CACHE.get(artifact_id)
        if cached is not None and cached.batch_id == batch_id:
            _PREPARE_CACHE.pop(artifact_id, None)

    async def _fail(
        self, batch_id: str, error: str, guard: tuple[str, ...],
    ) -> None:
        """守卫写 state=failed+error（决策1：state 不在守卫白名单——已被
        cancel 抢先——则不覆写）。"""
        try:
            await self._inputs.update_batch_details(
                batch_id, {"state": "failed", "error": error[:300]},
                guard_states=list(guard))
        except Exception:  # noqa: BLE001
            logger.exception(
                "wiki 编译 failed 态落账失败 batch=%s", batch_id)
