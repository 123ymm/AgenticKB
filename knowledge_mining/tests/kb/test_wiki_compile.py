"""Wiki 制品编译管线（63号批次3 Task 3）—— CompileService 全链 Fake 测试。

真实 CompileService/WikiService/ArtifactService/WikiLifecycle + 批次2 Fake 家族
（FakeArtifactDB/FakeWikiDB）+ 本文件 FakeCompileInputsDB（扩展 try_start_guard/
守卫 details/complete_batch/collect_units）+ FakeGenerationClient（同步记录桩，
可编程队列）。覆盖：状态机全迁移（preparing→awaiting_toc→writing→completed /
failed / cancelled 含 writing 批边界停止）；量级护栏 422 + 二次 start 409；
TOC 超限/解析失败各重试一次；confirm 编辑生效 + 超 200 页 422；25/批分批 +
批间约束注入（第二批 system 含第一批断链文本）；origin 保护（manual 撞键
跳过+conflicts、machine 覆盖）；引用校验三件 + draft 宽免；provenance 按
(kb,snapshot) 分组；质量页（contradiction/open_question/glossary）frontmatter
严格性；发布校验（blocking 409 / warning 附带 / 通过零告警）；row_count 填充；
status design-only（viewer 403）。
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from array import array
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import WikiError
from knowledge_mining.mining.kb.deps import (
    get_artifact_service,
    get_compile_service,
    get_wiki_service,
)
from knowledge_mining.mining.kb.routes.artifacts import router as artifacts_router
from knowledge_mining.mining.kb.routes.artifacts import (
    _map_error as _map_artifact_error,
)
from knowledge_mining.mining.kb.routes.wiki import router as wiki_router
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactService,
    InvalidOperation,
    StaleArtifact,
)
from knowledge_mining.mining.kb.services.wiki_compile_service import (
    MAX_PAGES,
    MAX_UNITS,
    PAGE_PROMPT_VERSION,
    TOC_PROMPT_VERSION,
    _PREPARE_CACHE,
    CompileService,
    validate_pages_for_publish,
)
from knowledge_mining.mining.kb.services.wiki_service import (
    WikiLifecycle,
    WikiService,
)
from knowledge_mining.tests.kb.test_artifacts_service import (
    FakeArtifactDB,
    _user,
)
from knowledge_mining.tests.kb.test_wiki_service import (
    FakeTaskClient,
    FakeWikiDB,
)

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
WID = "art_wiki_compile"
NOW = datetime(2026, 10, 22, 9, 0, 0, 500000, tzinfo=timezone.utc)

_VEC = array("f", (1.0, 0.0, 0.0, 0.0))   # 同向向量 → 全部单元聚成单簇


# ---------------------------------------------------------------- fakes

class FakeGenerationClient:
    """GenerationClient 协议桩：同步 execute（服务层 asyncio.to_thread 调用），
    顺序出队可编程结果（str=dict 解析产物/文本；Exception → raise）。可选
    gate_at：第 N 次调用（1 起）阻塞在 threading.Event 上——模拟慢 LLM 供
    cancel 在 writing 中途穿插。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queue: list = []
        self.gate_at: int | None = None
        self._gate = threading.Event()

    def release_gate(self) -> None:
        self._gate.set()

    def execute(self, messages, *, expected_output_type: str = "text",
                idempotency_key=None, pipeline_stage: str = "mining_enrichment"):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "expected_output_type": expected_output_type,
            "idempotency_key": idempotency_key,
            "pipeline_stage": pipeline_stage,
        })
        if self.gate_at is not None and len(self.calls) == self.gate_at:
            self._gate.wait(timeout=5)   # 测试线程 cancel 后 release
        if not self.queue:
            raise AssertionError("FakeGenerationClient 队列耗尽")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeCompileInputsDB:
    """CompileInputsDB 内存实现（T1 版扩展编译域四方法）。守卫语义与真库
    同构：update_batch_details 按 state 白名单拦截；complete_batch 守卫
    'writing' + row_count 同笔；try_start_guard 查在飞→查源→Σ护栏→建批
    （超限不建批——不留孤儿批次行；**建批 INSERT 原子携带
    state=preparing**（审查 I-1），_guard_lock 模拟 pg_advisory_xact_lock
    的跨协程串行化，guard_yield 在「检查后 INSERT 前」让位制造竞态窗口）。"""

    def __init__(self, artifacts: dict[str, dict]) -> None:
        self.artifacts = artifacts
        self.sources: dict[tuple[str, str], dict] = {}
        self.kb_fingerprints: dict[str, dict] = {}
        self.fingerprint_calls: list[tuple[str, tuple[str, ...]]] = []
        self.readable: set[str] = set()
        self.titles: dict[str, str] = {}
        self.batch_details: dict[str, dict] = {}
        self.batches: list[dict] = []
        self.touches: list[str] = []
        # kb_id -> 取材单元行（directory_path 过滤镜像 _DIR_FILTER）
        self.collected: dict[str, list[dict]] = {}
        self.guard_calls: int = 0
        # 模拟 advisory lock 的守卫串行化 + 竞态窗口注入（审查 I-1 测试）
        self._guard_lock = asyncio.Lock()
        self.guard_yield = False

    def _touch(self, artifact_id: str) -> None:
        self.touches.append(artifact_id)
        art = self.artifacts.get(artifact_id)
        if art is not None:
            art["updated_at"] = datetime.now(timezone.utc)

    def _assert_expected(self, artifact_id: str, expected_updated_at) -> None:
        if expected_updated_at is None:
            return
        art = self.artifacts.get(artifact_id)
        if art is None:
            raise WikiError("artifact_not_found")
        if art["updated_at"] != expected_updated_at:
            raise WikiError("stale_artifact")

    # ---- 绑定 CRUD -------------------------------------------------------

    async def list_sources(self, artifact_id: str) -> list[dict]:
        rows = [dict(r) for r in self.sources.values()
                if r["artifact_id"] == artifact_id
                and r["status"] == "active"]
        rows.sort(key=lambda r: (r["created_at"], r["kb_id"]))
        return rows

    async def upsert_source(self, artifact_id: str, kb_id: str, scope: dict,
                            created_by: str, *, expected_updated_at=None) -> dict:
        self._assert_expected(artifact_id, expected_updated_at)
        row = self.sources.get((artifact_id, kb_id))
        if row is None:
            row = {
                "id": f"src_{uuid.uuid4().hex[:12]}",
                "artifact_id": artifact_id, "kb_id": kb_id,
                "scope": dict(scope or {}), "fingerprint": {},
                "status": "active", "created_by": created_by,
                "created_at": datetime.now(timezone.utc),
            }
            self.sources[(artifact_id, kb_id)] = row
        else:
            row["scope"] = dict(scope or {})
            row["status"] = "active"
            row["created_by"] = created_by
        self._touch(artifact_id)
        return dict(row)

    async def delete_source(self, artifact_id: str, kb_id: str, *,
                            expected_updated_at=None) -> bool:
        self._assert_expected(artifact_id, expected_updated_at)
        row = self.sources.get((artifact_id, kb_id))
        if row is None or row["status"] != "active":
            return False
        row["status"] = "removed"
        self._touch(artifact_id)
        return True

    async def update_fingerprint(self, artifact_id: str, kb_id: str,
                                 fingerprint: dict) -> bool:
        row = self.sources.get((artifact_id, kb_id))
        if row is None:
            return False
        row["fingerprint"] = dict(fingerprint or {})
        return True

    # ---- 指纹观测 / kb 元数据 --------------------------------------------

    async def current_fingerprint(self, kb_id: str, directories) -> dict:
        dirs = tuple(sorted({str(d).strip() for d in (directories or [])
                             if str(d).strip()}))
        self.fingerprint_calls.append((kb_id, dirs))
        fp = self.kb_fingerprints.get(kb_id)
        if fp is None:
            return {"snapshots": [], "computed_at": "2026-10-22T00:00:00+00:00"}
        snaps = []
        for snap in fp.get("snapshots", []):
            dp = str(snap.get("directory_path") or "")
            in_scope = (not dirs or dp in dirs
                        or any(dp.startswith(d + "/") for d in dirs))
            snaps.append({
                "snapshot_id": snap["snapshot_id"],
                "unit_count": (int(snap.get("unit_count") or 0)
                               if in_scope else 0),
            })
        return {"snapshots": snaps, "computed_at": fp.get("computed_at")}

    async def kb_readable(self, kb_id: str, user_id: str) -> bool:
        return kb_id in self.readable

    async def kb_titles(self, kb_ids: list[str]) -> dict[str, str]:
        return {k: self.titles[k] for k in kb_ids if k in self.titles}

    # ---- 编译取材 --------------------------------------------------------

    async def collect_units(self, kb_id: str, directories) -> list[dict]:
        dirs = sorted({str(d).strip() for d in (directories or [])
                       if str(d).strip()})
        rows = []
        for u in self.collected.get(kb_id, []):
            dp = str(u.get("directory_path") or "")
            if not dirs or dp in dirs or any(dp.startswith(d + "/")
                                             for d in dirs):
                rows.append({**u, "kb_id": kb_id})
        return rows

    # ---- 编译批次 --------------------------------------------------------

    async def try_start_guard(self, artifact_id: str, created_by: str, *,
                              max_units: int) -> dict:
        self.guard_calls += 1
        async with self._guard_lock:   # 模拟 pg_advisory_xact_lock 串行化
            latest = await self.latest_compile_batch(artifact_id)
            if latest is not None:
                state = str((latest.get("details") or {}).get("state") or "")
                if state in ("preparing", "awaiting_toc", "writing"):
                    return {"in_flight": latest, "batch": None,
                            "sources": [], "unit_total": 0}
            sources = await self.list_sources(artifact_id)
            if not sources:
                return {"in_flight": None, "batch": None,
                        "sources": [], "unit_total": 0}
            total = 0
            for src in sources:
                dirs = [str(d) for d in
                        ((src.get("scope") or {}).get("directories") or [])]
                fp = await self.current_fingerprint(str(src["kb_id"]), dirs)
                total += sum(int(s["unit_count"]) for s in fp["snapshots"])
            if total > int(max_units):
                return {"in_flight": None, "batch": None,
                        "sources": sources, "unit_total": total}
            if self.guard_yield:
                # 竞态窗口注入：检查已过、INSERT 未落——并发的第二个 start
                # 在真库会被 advisory lock 挡在检查前，此处由 _guard_lock
                # 同构保证；yield 验证建批原子携带 preparing 后此窗口无害。
                await asyncio.sleep(0)
            batch = {
                "batch_id": f"bat_{uuid.uuid4().hex[:12]}",
                "artifact_id": artifact_id, "channel": "wiki_compile",
                "file_name": "", "sheet_name": "", "row_count": 0,
                "created_by": created_by,
                "created_at": datetime.now(timezone.utc),
            }
            self.batches.append(batch)
            # INSERT 原子携带 preparing（审查 I-1——与真库同构）
            self.batch_details[batch["batch_id"]] = {"state": "preparing"}
            return {"in_flight": None, "batch": dict(batch),
                    "sources": sources, "unit_total": total}

    async def update_batch_details(self, batch_id: str, patch: dict, *,
                                   guard_states=None) -> bool:
        details = self.batch_details.get(batch_id)
        if details is None:
            return False
        if guard_states is not None \
                and details.get("state") not in guard_states:
            return False
        self.batch_details[batch_id] = {**details, **patch}
        return True

    async def complete_batch(self, batch_id: str, patch: dict, *,
                             row_count: int) -> bool:
        details = self.batch_details.get(batch_id)
        if details is None or details.get("state") != "writing":
            return False
        self.batch_details[batch_id] = {**details, **patch}
        for b in self.batches:
            if b["batch_id"] == batch_id:
                b["row_count"] = int(row_count)
        return True

    async def latest_compile_batch(self, artifact_id: str) -> dict | None:
        rows = [b for b in self.batches
                if b["artifact_id"] == artifact_id
                and b["channel"] == "wiki_compile"]
        if not rows:
            return None
        latest = max(rows, key=lambda b: (b["created_at"], b["batch_id"]))
        return {**dict(latest),
                "details": dict(self.batch_details.get(latest["batch_id"], {}))}

    async def batch_state(self, batch_id: str) -> dict | None:
        """单批读取（含 details）——F1 僵尸停止位数据源。"""
        for b in self.batches:
            if b["batch_id"] == batch_id:
                return {**dict(b),
                        "details": dict(self.batch_details.get(batch_id, {}))}
        return None


# ---------------------------------------------------------------- helpers

def _units(prefix: str, snapshot: str, n: int = 6, *,
           directory: str = "方案") -> list[dict]:
    return [
        {"representation_id": f"{prefix}{i}", "snapshot_id": snapshot,
         "content_text": f"{prefix}{i} 核心网切片配置事实陈述，含参数与约束。",
         "structural_context": "", "section_ref": None, "ordinal": i,
         "dense_eligible": True, "directory_path": directory,
         "embedding": array("f", _VEC)}
        for i in range(n)
    ]


def _cluster_id(units: list[dict]) -> str:
    """聚类确定性 id 镜像（成员排序 sha256[:12]）——测试侧预计算真实簇 id。"""
    ids = sorted(u["representation_id"] for u in units)
    payload = "\n".join(ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _toc_json(titles: list[str], cluster_id: str, *, sections=None,
              glossary=(), open_questions=()) -> dict:
    if sections is None:
        sections = [{"section_title": "核心网", "page_hints": [
            {"title": t, "cluster_ids": [cluster_id], "page_type": "topic"}
            for t in titles]}]
    return {"toc": sections, "glossary": list(glossary),
            "open_questions": list(open_questions)}


def _page_body(title: str, refs: tuple[str, ...] = ("u0", "v0"),
               *, broken: bool = True, bare_run: int = 0) -> str:
    lines = [f"# {title}", "", f"> {title} 的摘要一句话。", "", "## 核心事实"]
    for i in range(3):
        ref = refs[i % len(refs)]
        lines.append(f"- {title} 事实{i} [[src:{ref}]]")
    for i in range(bare_run):   # 密度告警素材：连续无引用事实句
        lines.append(f"- {title} 裸事实{i}（无引用）")
    if broken:
        lines += ["", "## 相关页面", "- [[不存在页]] -- 参照"]
    lines += ["", "## 来源", f"- [[src:{refs[0]}]] -- 单元摘要一句话"]
    return "\n".join(lines)


_QUALITY_OK = {
    "contradictions": [{
        "page_a": "", "page_b": "",   # 由 helper 按 aio 页键填充
        "claim_a": "切片上限 100", "claim_b": "切片上限 200",
        "severity": "medium"}],
    "gaps": [{"question": "如何灰度扩容？", "context": "文档未覆盖灰度路径"}],
}


def _quality(a: str, b: str) -> dict:
    out = {"contradictions": [dict(_QUALITY_OK["contradictions"][0])],
           "gaps": [dict(_QUALITY_OK["gaps"][0])]}
    out["contradictions"][0]["page_a"] = a
    out["contradictions"][0]["page_b"] = b
    return out


@pytest_asyncio.fixture
async def env():
    adb = FakeArtifactDB()
    adb.artifacts[WID] = {
        "artifact_id": WID, "domain_id": DOMAIN, "kind": "wiki",
        "title": "云核 Wiki", "description": "", "usage_note": "团队词条库",
        "owner_user_id": "u-alice", "visibility": "private",
        "status": "draft", "current_revision": 0, "draft_open": False,
        "created_at": NOW, "updated_at": NOW,
    }
    adb.members[WID] = {
        "u-alice": "owner", "u-bob": "editor", "u-viewer": "viewer",
    }
    adb.domain_bindings.update(
        (uid, DOMAIN) for uid in ("u-alice", "u-bob", "u-viewer"))
    wdb = FakeWikiDB(adb.artifacts)
    tc = FakeTaskClient()
    lifecycle = WikiLifecycle(wdb, tc)
    art_svc = ArtifactService(adb, wiki=lifecycle)
    inputs = FakeCompileInputsDB(adb.artifacts)
    inputs.readable = {"kb-1", "kb-2"}
    inputs.titles = {"kb-1": "云核知识库", "kb-2": "割接手册库"}
    wiki_svc = WikiService(art_svc, wdb, lifecycle, inputs_db=inputs)
    gen = FakeGenerationClient()
    compile_svc = CompileService(wdb, inputs, gen, wiki_svc)

    app = FastAPI()
    app.include_router(wiki_router)
    app.include_router(artifacts_router)
    holder = {"user": _user("u-alice")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_wiki_service] = lambda: wiki_svc
    app.dependency_overrides[get_compile_service] = lambda: compile_svc
    # artifacts publish 路由（发布校验 409 路由面）也走同一 ArtifactService
    app.dependency_overrides[get_artifact_service] = lambda: art_svc
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield SimpleNamespace(
                adb=adb, wdb=wdb, tc=tc, art_svc=art_svc, wiki_svc=wiki_svc,
                inputs=inputs, gen=gen, compile_svc=compile_svc, app=app,
                client=client, holder=holder,
            )
        finally:
            # 排空残留后台编译任务：模块级 _PREPARE_CACHE 跨测试共享，任务
            # 退出路径不跑会污染下一用例（loop 关闭时任务销毁时机不确定）。
            others = [t for t in asyncio.all_tasks()
                      if t is not asyncio.current_task()]
            if others:
                await asyncio.gather(*others, return_exceptions=True)


async def _bind(env, kb_id: str = "kb-1", directories=None) -> None:
    await env.wiki_svc.upsert_source(
        _user("u-alice"), WID, kb_id,
        directories if directories is not None else [])


def _seed_corpus(env, *, kb_ids=("kb-1", "kb-2"), per_kb: int = 6) -> list[dict]:
    """语料+指纹观测两件套（绑定另走 _bind_all）：返回全部取材单元（跨 kb
    合并序——kb-1 前缀 u、kb-2 前缀 v，同向向量聚成单簇）。"""
    all_units: list[dict] = []
    for kb in kb_ids:
        prefix = {"kb-1": "u", "kb-2": "v"}[kb]
        snap = {"kb-1": "snap-1", "kb-2": "snap-2"}[kb]
        units = _units(prefix, snap, per_kb)
        env.inputs.collected[kb] = units
        env.inputs.kb_fingerprints[kb] = {
            "snapshots": [{"snapshot_id": snap, "unit_count": per_kb,
                           "directory_path": "方案"}],
            "computed_at": "X"}
        all_units.extend(units)
    return all_units


async def _bind_all(env, kb_ids=("kb-1", "kb-2")):
    for kb in kb_ids:
        await _bind(env, kb)


async def _start(env) -> dict:
    return await env.compile_svc.start(_user("u-alice"), WID)


async def _wait_state(env, *states: str, timeout: float = 5.0) -> dict:
    """轮询最新编译批次直到命中目标态（fake client 同步返回，任务快速收敛）。

    sleep(0.001) 而非 sleep(0)：纯自旋会饿死 executor 线程（to_thread 的完成
    回调需要 GIL 让步），1ms 实睡让后台任务真正推进。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        batch = await env.inputs.latest_compile_batch(WID)
        if batch is not None:
            state = str((batch.get("details") or {}).get("state") or "")
            if state in states:
                return batch
        await asyncio.sleep(0.001)
    batch = await env.inputs.latest_compile_batch(WID)
    raise AssertionError(
        f"等待状态 {states} 超时，当前 details={batch and batch.get('details')}")


async def _confirm(env, toc) -> None:
    await env.compile_svc.confirm_toc(_user("u-alice"), WID, toc)


def _details(batch: dict) -> dict:
    return batch.get("details") or {}


# ------------------------------------------------------------ 状态机全迁移

async def test_full_state_machine_happy_path(env):
    """start→preparing→（后台）awaiting_toc→confirm→writing→completed：stats/
    conflicts/progress/row_count/fingerprint 落账齐全。"""
    titles = ["页A", "页B", "页C"]
    all_units = _seed_corpus(env)
    await _bind_all(env, ("kb-1", "kb-2"))
    cid = _cluster_id(all_units)
    env.gen.queue = [
        _toc_json(titles, cid,
                  glossary=[{"term": "网络切片",
                             "definition_hint": "物理网络的多租户切分"}],
                  open_questions=[{"question": "切片隔离强度？",
                                   "why": "文档未覆盖"}]),
        _page_body("页A"), _page_body("页B"), _page_body("页C"),
        _quality("页A", "页B"),
    ]

    result = await _start(env)
    assert result["state"] == "preparing" and result["batch_id"]
    # start 同步落 preparing（并发 start 的 DB 侧判据）
    batch = await env.inputs.latest_compile_batch(WID)
    assert _details(batch)["state"] == "preparing"

    batch = await _wait_state(env, "awaiting_toc")
    det = _details(batch)
    assert det["state"] == "awaiting_toc"
    assert [p["title"] for p in det["toc"][0]["page_hints"]] == titles
    stats = det["stats"]
    assert stats["unit_count"] == 12 and stats["cluster_count"] == 1
    assert 0 < stats["embedded_ratio"] <= 1
    # 指纹已按 scope 同参落库（每源一份）
    assert env.inputs.sources[(WID, "kb-1")]["fingerprint"]["snapshots"] == [
        {"snapshot_id": "snap-1", "unit_count": 6}]
    assert env.inputs.sources[(WID, "kb-2")]["fingerprint"]["snapshots"] == [
        {"snapshot_id": "snap-2", "unit_count": 6}]
    assert det["source_fingerprint"]["kb-1"]["snapshots"][0]["unit_count"] == 6

    await _confirm(env, det["toc"])
    batch = await _wait_state(env, "completed")
    det = _details(batch)
    assert det["state"] == "completed"
    assert det["stats"]["pages_written"] == 3
    assert det["stats"]["failed_pages"] == 0
    assert det["stats"]["conflicts_n"] == 0
    assert det["stats"]["broken_links"] >= 1      # [[不存在页]] 断链入账
    assert det["stats"]["dangling_pages"] == 0
    assert det["stats"]["glossary_pages"] == 1
    assert det["stats"]["contradiction_pages"] == 1
    assert det["stats"]["open_question_pages"] == 2   # 质量缺口+prepare 问题
    assert det["progress"] == {"batch": "1/1", "pages_done": 3}
    # row_count = 主题页数
    assert batch["row_count"] == 3
    # rev0 落页：origin=machine + 页键归一
    page = await env.wdb.get_page(WID, 0, "页A")
    assert page is not None and page["origin"] == "machine"
    assert page["ordinal"] == 1001                 # 节1 页1（1000 基数）
    # draft 已开（编译产物可发布）
    assert env.adb.artifacts[WID]["draft_open"] is True


async def test_failed_path_no_units(env):
    """绑定源取材为空 → failed + error=no_units（指纹有快照但零单元）。"""
    env.inputs.collected["kb-1"] = []
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "snap-1", "unit_count": 0,
                       "directory_path": "方案"}], "computed_at": "X"}
    await _bind(env)
    await _start(env)
    batch = await _wait_state(env, "failed")
    assert _details(batch)["error"] == "no_units"


async def test_cancel_awaiting_toc(env):
    """awaiting_toc 取消（无后台任务，直接改 state）；随后 confirm → 409
    invalid_compile_state。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")

    out = await env.compile_svc.cancel(_user("u-alice"), WID)
    assert out["state"] == "cancelled"
    batch = await env.inputs.latest_compile_batch(WID)
    assert _details(batch)["state"] == "cancelled"

    with pytest.raises(WikiError, match="invalid_compile_state"):
        await _confirm(env, _details(batch)["toc"])
    # 终态再取消 → 409 invalid_compile_state
    with pytest.raises(WikiError, match="invalid_compile_state"):
        await env.compile_svc.cancel(_user("u-alice"), WID)


async def test_cancel_writing_stops_at_batch_boundary(env):
    """writing 中途取消：批边界读 details.state=cancelled 即停——第 2 批首页
    调用被 gate 卡住时 cancel，放行后第 2 批写完、第 3 批边界退出；终态
    cancelled 且无 completed 覆写。"""
    titles = [f"页{i}" for i in range(51)]        # 3 批：25/25/1
    all_units = _seed_corpus(env, per_kb=26)       # 12+... 需 ≥ 簇成员引用
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = (
        [_toc_json(titles, cid)]
        + [_page_body(t) for t in titles]
        + [_quality("页0", "页1")])
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]

    # gate：第 27 次调用 = TOC(1) + 25 页后第 2 批首页
    env.gen.gate_at = 27
    await _confirm(env, toc)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and len(env.gen.calls) < 27:
        await asyncio.sleep(0.001)
    assert len(env.gen.calls) >= 27, \
        f"第 2 批首页调用未出现（calls={len(env.gen.calls)}）"
    assert str(_details(await env.inputs.latest_compile_batch(WID))
               .get("state")) == "writing"

    await env.compile_svc.cancel(_user("u-alice"), WID)
    assert _details(await env.inputs.latest_compile_batch(WID))["state"] \
        == "cancelled"
    env.gen.release_gate()
    # 等第 2 批写完（pages_written 稳定 50）——批 3 边界读到 cancelled 即退出
    settle = loop.time() + 5.0
    det = {}
    while loop.time() < settle:
        det = _details(await env.inputs.latest_compile_batch(WID))
        if det.get("stats", {}).get("pages_written") == 50:
            break
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)   # 让批 3 边界检查与 finally 收尾跑完
    batch = await env.inputs.latest_compile_batch(WID)
    det = _details(batch)
    assert det["state"] == "cancelled", det         # completed 未覆写
    assert det["stats"]["pages_written"] == 50, det  # 批1+批2；批3 未开写
    assert det["progress"] == {"batch": "2/3", "pages_done": 50}, det
    assert batch["row_count"] == 0                  # complete_batch 未执行


async def test_zombie_writing_cross_round_stops_and_keeps_new_cache(env):
    """审查 F1：cancel 后立刻 start 新轮——旧 writing 僵尸在批边界按**本批**
    batch_id 读 state（不读 latest，否则误读新轮 preparing/awaiting_toc 续写）
    停止；退出路径不抹新轮 cache（归属判断）；新轮可正常 confirm→completed。"""
    titles = [f"页{i}" for i in range(51)]        # 旧轮 3 批
    all_units = _seed_corpus(env, per_kb=26)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = (
        [_toc_json(titles, cid)]
        + [_page_body(t) for t in titles]
        + [_quality("页0", "页1")])
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    old_batch = await env.inputs.latest_compile_batch(WID)
    old_id = str(old_batch["batch_id"])
    toc = _details(old_batch)["toc"]

    env.gen.gate_at = 27                    # 旧轮批 2 首页卡住
    await _confirm(env, toc)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and len(env.gen.calls) < 27:
        await asyncio.sleep(0.001)
    assert len(env.gen.calls) >= 27

    await env.compile_svc.cancel(_user("u-alice"), WID)
    assert _details(await env.inputs.latest_compile_batch(WID))["state"] \
        == "cancelled"
    assert WID not in _PREPARE_CACHE       # F7：cancel 即释放本批缓存

    # 立刻开新轮（旧僵尸仍在飞——守卫按 DB 在飞判定=旧批已 cancelled，放行；
    # 进程内 in-flight 键不作前置）。队列：新 TOC 在前（call 28 消费），随后
    # 25 条僵尸素材供旧任务批 2 放行后消费，最后新轮写作素材。
    env.gen.queue = (
        [_toc_json(["新页A"], cid)]
        + [_page_body("僵尸页", broken=False) for _ in range(25)]
        + [_page_body("新页A", broken=False), _quality("新页A", "新页A")])
    r2 = await _start(env)
    new_id = str(r2["batch_id"])
    assert new_id != old_id
    await _wait_state(env, "awaiting_toc")   # 新轮 prepare（TOC=call 28）
    assert _PREPARE_CACHE[WID].batch_id == new_id

    env.gen.release_gate()
    # 旧僵尸：写完批 2（25 条僵尸素材）→ 批 3 边界读**本批** state=cancelled 停
    settle = loop.time() + 5.0
    while loop.time() < settle:
        old_det = env.inputs.batch_details[old_id]
        if old_det.get("stats", {}).get("pages_written") == 50:
            break
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.02)               # 让批 3 边界检查与 finally 收尾跑完

    old_now = await env.inputs.batch_state(old_id)
    assert _details(old_now)["state"] == "cancelled"   # 未被 completed 覆写
    assert old_now["row_count"] == 0
    assert _PREPARE_CACHE[WID].batch_id == new_id      # 僵尸未抹新轮缓存

    # 新轮照常完结（缓存完好可用）
    new_toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, new_toc)
    await _wait_state(env, "completed")
    assert (await env.inputs.batch_state(new_id))["row_count"] == 1


async def test_cancel_after_last_batch_skips_quality(env):
    """审查 F8：末批页写完后、质量 LLM 前的停止位复查——cancelled 则跳过
    质量+专页，不落 completed/row_count（质量调用未发生）。"""
    titles = ["页A", "页B", "页C"]          # 单批 3 页
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [
        _toc_json(titles, cid, glossary=[
            {"term": "术语X", "definition_hint": "d"}]),
        _page_body("页A", broken=False), _page_body("页B", broken=False),
        _page_body("页C", broken=False),
        _quality("页A", "页B")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]

    env.gen.gate_at = 4                     # TOC(1)+3 页 → 第 4 次调用=末页
    await _confirm(env, toc)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and len(env.gen.calls) < 4:
        await asyncio.sleep(0.001)
    assert len(env.gen.calls) >= 4

    await env.compile_svc.cancel(_user("u-alice"), WID)
    assert _details(await env.inputs.latest_compile_batch(WID))["state"] \
        == "cancelled"
    env.gen.release_gate()

    settle = loop.time() + 5.0              # 等末页写完+批后处理+复查退出
    while loop.time() < settle:
        det = _details(await env.inputs.latest_compile_batch(WID))
        if det.get("stats", {}).get("pages_written") == 3:
            break
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.02)

    batch = await env.inputs.latest_compile_batch(WID)
    det = _details(batch)
    assert det["state"] == "cancelled"      # F8 复查拦下 completed
    assert det["stats"]["pages_written"] == 3
    assert det["progress"] == {"batch": "1/1", "pages_done": 3}
    assert batch["row_count"] == 0
    assert len(env.gen.calls) == 4          # 质量 LLM 调用未发生
    assert det["stats"].get("glossary_pages") is None   # 专页未生成


# ------------------------------------------------------------ start 护栏

async def test_start_no_sources_422(env):
    """未绑定任何来源 → 422 no_sources（不建批）。"""
    with pytest.raises(WikiError, match="no_sources"):
        await _start(env)
    assert await env.inputs.latest_compile_batch(WID) is None


async def test_start_too_many_units_422(env):
    """Σ scope 单元 > 30000 → 422 too_many_units（不建批、可重复触发）。"""
    env.inputs.collected["kb-1"] = _units("u", "snap-1", 6)
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "snap-1", "unit_count": MAX_UNITS + 1,
                       "directory_path": "方案"}], "computed_at": "X"}
    await _bind(env)
    with pytest.raises(WikiError, match="too_many_units"):
        await _start(env)
    assert await env.inputs.latest_compile_batch(WID) is None
    # 恰好在上限内（30000）放行到 preparing
    env.inputs.kb_fingerprints["kb-1"]["snapshots"][0]["unit_count"] = MAX_UNITS
    result = await _start(env)
    assert result["state"] == "preparing"
    await _wait_state(env, "failed")     # 取材为 6 单元 → 语料照常失败无关护栏


async def test_second_start_while_inflight_409(env):
    """在飞（awaiting_toc）二次 start → 409 compile_in_progress。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    with pytest.raises(WikiError, match="compile_in_progress"):
        await _start(env)


async def test_start_race_concurrent_second_rejected(env):
    """审查 I-1 TOCTOU：守卫在「检查后 INSERT 前」让位（guard_yield），并发
    第二个 start 仍被拒——建批 INSERT 原子携带 state=preparing，B 的在飞
    检查（被 _guard_lock 挡到 A 提交后，同构真库 advisory lock）看到
    preparing 即拒；全库只建一个批次。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [
        _toc_json(["页A"], _cluster_id(all_units)),
        _page_body("页A"), _quality("页A", "页A")]
    env.inputs.guard_yield = True   # 检查与 INSERT 之间让位一次
    results = await asyncio.gather(
        env.compile_svc.start(_user("u-alice"), WID),
        env.compile_svc.start(_user("u-alice"), WID),
        return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errs) == 1, results
    assert "compile_in_progress" in str(errs[0])
    wiki_batches = [b for b in env.inputs.batches
                    if b["channel"] == "wiki_compile"]
    assert len(wiki_batches) == 1                  # 只建一个批次
    assert env.inputs.batch_details[wiki_batches[0]["batch_id"]]["state"] \
        == "preparing"                             # INSERT 原子携带


async def test_start_stale_token_409(env):
    """乐观锁失配 → StaleArtifact（409 家族，路由翻 stale_artifact）。"""
    env.inputs.collected["kb-1"] = _units("u", "snap-1", 6)
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "snap-1", "unit_count": 6,
                       "directory_path": "方案"}], "computed_at": "X"}
    await _bind(env)
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(StaleArtifact):
        await env.compile_svc.start(
            _user("u-alice"), WID, expected_updated_at=stale)


# ------------------------------------------------------------ TOC 生成

async def test_toc_over_limit_retries_once(env):
    """TOC 首版 201 页超限 → 带压缩注记重发一次 → 采用第二版；两次调用幂等键
    不同（payload 含注记）。"""
    titles = ["页A", "页B"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    over = _toc_json([f"页{i}" for i in range(MAX_PAGES + 1)], cid)
    ok = _toc_json(titles, cid)
    env.gen.queue = [over, ok] + [_page_body(t) for t in titles] + \
        [_quality("页A", "页B")]
    await _start(env)
    batch = await _wait_state(env, "awaiting_toc")
    assert [p["title"] for p in _details(batch)["toc"][0]["page_hints"]] \
        == titles
    assert len(env.gen.calls) >= 2
    assert env.gen.calls[0]["idempotency_key"] != \
        env.gen.calls[1]["idempotency_key"]
    # 重发版带压缩注记
    assert "压缩" in env.gen.calls[1]["messages"][1]["content"]
    assert env.gen.calls[0]["expected_output_type"] == "json_object"
    assert env.gen.calls[0]["idempotency_key"].startswith("wiki-toc:")


async def test_toc_parse_failure_retry_then_failed(env):
    """TOC 两次解析失败 → state=failed（error=toc_generation_failed）。"""
    _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [RuntimeError("json parse failed"),
                     RuntimeError("json parse failed again")]
    await _start(env)
    batch = await _wait_state(env, "failed")
    assert _details(batch)["error"] == "toc_generation_failed"


async def test_toc_retry_recovers_from_parse_failure(env):
    """TOC 首次异常、重试成功 → awaiting_toc 正常。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [
        RuntimeError("boom"),
        _toc_json(["页A"], _cluster_id(all_units)),
        _page_body("页A"), _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")


# ------------------------------------------------------------ confirm_toc

async def test_confirm_edited_toc_takes_effect(env):
    """confirm 的用户编辑版生效：改标题/删页 → 写作按编辑版展开。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [_toc_json(["页A", "页B", "页C"], cid)]
    await _start(env)
    await _wait_state(env, "awaiting_toc")

    edited = [{"section_title": "人工改编节",
               "page_hints": [
                   {"title": "页A-改", "cluster_ids": [cid],
                    "page_type": "topic"},
                   {"title": "页C", "cluster_ids": [cid],
                    "page_type": "topic"}]}]
    env.gen.queue = [_page_body("页A-改"), _page_body("页C"), _quality(
        "页A-改", "页C")]
    await _confirm(env, edited)
    batch = await _wait_state(env, "completed")
    det = _details(batch)
    assert det["toc"] == edited                     # details 存确认版
    assert det["stats"]["pages_written"] == 2
    assert await env.wdb.get_page(WID, 0, "页A-改") is not None
    assert await env.wdb.get_page(WID, 0, "页B") is None


async def test_confirm_over_200_pages_422(env):
    """confirm 展开页数 > 200 → 422 too_many_pages（不进 writing）。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [_toc_json(["页A"], cid)]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    big = [{"section_title": "s", "page_hints": [
        {"title": f"页{i}", "cluster_ids": [cid], "page_type": "topic"}
        for i in range(MAX_PAGES + 1)]}]
    with pytest.raises(WikiError, match="too_many_pages"):
        await _confirm(env, big)
    assert _details(await env.inputs.latest_compile_batch(WID))["state"] \
        == "awaiting_toc"


async def test_confirm_zero_pages_422(env):
    """审查 Minor：零页目录（空列表 / 节全空 hints）→ 422 invalid_toc
    （writing 展开无页直接完结无意义）。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    with pytest.raises(WikiError, match="invalid_toc"):
        await _confirm(env, [])                    # 空列表（normalize 拒）
    with pytest.raises(WikiError, match="invalid_toc"):
        await _confirm(env, [{"section_title": "空节",
                              "page_hints": []}])  # 节在页零
    assert _details(await env.inputs.latest_compile_batch(WID))["state"] \
        == "awaiting_toc"


async def test_confirm_wrong_state_409_and_not_found(env):
    """非 awaiting_toc 的 confirm → 409 invalid_compile_state；无批次 → 404
    compile_not_found。"""
    with pytest.raises(WikiError, match="compile_not_found"):
        await _confirm(env, [])
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    # 写作进行中不可重复确认：先造一个 writing 态
    env.gen.queue = [_page_body("页A"), _quality("页A", "页A")]
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    with pytest.raises(WikiError, match="invalid_compile_state"):
        await _confirm(env, toc)


async def test_prepare_cache_lost_fails_writing(env):
    """进程重启丢 _PREPARE_CACHE → confirm 后 writing 即 failed（error 提示
    重新编译）。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    _PREPARE_CACHE.pop(WID, None)   # 模拟重启丢缓存（模块级缓存）
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "failed")
    assert "cache" in str(_details(batch)["error"])


async def test_artifact_delete_drops_prepare_cache(env):
    """审查 I-2②：制品级联硬删 → drop_prepare_cache 兜底清理（awaiting_toc
    期的缓存不再成孤儿——延迟 import 接线在 ArtifactService.delete_artifact）。"""
    all_units = _seed_corpus(env)
    await _bind_all(env)
    env.gen.queue = [_toc_json(["页A"], _cluster_id(all_units))]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    assert WID in _PREPARE_CACHE

    # 两级确认：先 deprecate 再删（delete_artifact 门禁）
    env.adb.artifacts[WID]["status"] = "deprecated"
    await env.art_svc.delete_artifact(user=_user("u-alice"), artifact_id=WID)
    assert WID not in _PREPARE_CACHE


# ------------------------------------------------------------ 写作分批

async def test_batching_25_and_inter_batch_constraint_injection(env):
    """51 页 → 3 批（25/25/1）；第 2 批 system 含第 1 批断链文本与「无据不写」
    约束；第 1 批 system 不含断链。"""
    titles = [f"页{i}" for i in range(51)]
    all_units = _seed_corpus(env, per_kb=26)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = (
        [_toc_json(titles, cid)]
        + [_page_body(t) for t in titles]
        + [_quality("页0", "页1")])
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)
    assert det["progress"] == {"batch": "3/3", "pages_done": 51}
    # calls[0]=TOC；calls[1..25]=批1；calls[26]=批2 首页
    b1_system = env.gen.calls[1]["messages"][0]["content"]
    b2_system = env.gen.calls[26]["messages"][0]["content"]
    b3_system = env.gen.calls[51]["messages"][0]["content"]
    assert "不存在页" not in b1_system
    assert "不存在页" in b2_system           # 第 1 批断链注入第 2 批
    assert "不存在页" in b3_system
    assert "无据不写" in b1_system           # 全批常驻约束
    # 页写作幂等键与 prompt 版本冻结
    assert env.gen.calls[1]["idempotency_key"].startswith("wiki-page:")
    assert PAGE_PROMPT_VERSION
    # 相关页面页键清单在 prompt 中（全局目录上下文）
    assert "页50" in b1_system
    assert det["stats"]["pages_written"] == 51


async def test_failed_page_skipped_and_counted(env):
    """单页 LLM 终态失败 → 跳过计数（failed_pages），不阻断整本。"""
    titles = ["页A", "页B", "页C"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [
        _toc_json(titles, cid),
        _page_body("页A"), RuntimeError("task failed"), _page_body("页C"),
        _quality("页A", "页C")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)
    assert det["stats"]["failed_pages"] == 1
    assert det["stats"]["pages_written"] == 2
    assert await env.wdb.get_page(WID, 0, "页B") is None
    assert batch["row_count"] == 2


# ------------------------------------------------------------ origin 保护

async def test_origin_protection_manual_skip_machine_overwrite(env):
    """manual 撞键 → 跳过 + conflicts 账；machine 同键 → 覆盖（重编译语义）。"""
    titles = ["页A", "页B", "页C"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    # 预置：manual 页（撞 页A）+ 旧 machine 页（撞 页B）
    await env.wdb.upsert_page(
        WID, page_key="页A", title="人工维护页A", body_md="人工正文",
        origin="manual", updated_by="u-alice")
    await env.wdb.upsert_page(
        WID, page_key="页B", title="旧机器页B", body_md="旧正文",
        origin="machine", updated_by="system")
    env.gen.queue = [
        _toc_json(titles, cid),
        _page_body("页A"), _page_body("页B"), _page_body("页C"),
        _quality("页A", "页C")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)

    # manual 页未被触碰
    page_a = await env.wdb.get_page(WID, 0, "页A")
    assert page_a["origin"] == "manual" and page_a["body_md"] == "人工正文"
    # machine 页被覆盖
    page_b = await env.wdb.get_page(WID, 0, "页B")
    assert page_b["origin"] == "machine" and page_b["body_md"] != "旧正文"
    # conflicts 账
    assert det["conflicts"] == [
        {"page_key": "页A", "manual_title": "人工维护页A",
         "machine_title": "页A"}]
    assert det["stats"]["conflicts_n"] == 1
    assert det["stats"]["pages_written"] == 2   # 页A 跳过


# ------------------------------------------------------------ 引用校验

async def test_citation_flags_three_kinds(env):
    """provenance 空（无簇成员可回退）/ 悬空 src / 密度告警 三件入 flags；
    正常页零 flags。"""
    titles = ["正常页", "悬空页", "密度页", "无簇页"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    bodies = {
        "正常页": _page_body("正常页", refs=("u0", "v0")),
        "悬空页": _page_body("悬空页", refs=("uZZZ", "u0")),
        "密度页": _page_body("密度页", refs=("u0",), bare_run=3),
    }
    toc = [{"section_title": "s", "page_hints": [
        {"title": "正常页", "cluster_ids": [cid], "page_type": "topic"},
        {"title": "悬空页", "cluster_ids": [cid], "page_type": "topic"},
        {"title": "密度页", "cluster_ids": [cid], "page_type": "topic"},
        # 无簇页：cluster_ids 指向不存在的簇 → provenance 回退集为空
        {"title": "无簇页", "cluster_ids": ["nonexistent"], "page_type": "topic"},
    ]}]
    env.gen.queue = [
        _toc_json([], cid, sections=[
            {"section_title": "s", "page_hints": []}]),
        bodies["正常页"], bodies["悬空页"], bodies["密度页"],
        _page_body("无簇页", refs=("u0",)),
        _quality("正常页", "密度页")]
    # 上面 TOC 空页会被 normalize 拒 → 直接给带 4 页的 TOC
    env.gen.queue[0] = {"toc": toc, "glossary": [], "open_questions": []}
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)

    normal = await env.wdb.get_page(WID, 0, "正常页")
    assert normal["flags"] == {}
    dangling = await env.wdb.get_page(WID, 0, "悬空页")
    assert dangling["flags"]["dangling_refs"] == ["uZZZ"]
    density = await env.wdb.get_page(WID, 0, "密度页")
    assert density["flags"]["density_warning"] is True
    noclu = await env.wdb.get_page(WID, 0, "无簇页")
    assert noclu["flags"]["provenance_missing"] is True
    assert noclu["provenance"] == []
    # 无簇页的 u0 引用也悬空（valid=页簇成员并集=空集——口径收窄后跨「无簇」
    # 引用同样编译期捕获）
    assert noclu["flags"]["dangling_refs"] == ["u0"]
    assert det["stats"]["dangling_pages"] == 2


async def test_cross_cluster_ref_dangling_at_compile(env):
    """审查 Minor：悬空口径第三接缝收窄——valid_unit_ids=页簇成员并集（与
    provenance 记账同口径），引用**他簇的真实 unit id** 编译期即 dangling
    （「编译干净发布却 blocking」的接缝消除）。"""
    # 两簇语料：u 簇同向 A 向量、w 簇同向 B 向量（余弦 0 < 0.62 → 两簇）
    u_units = _units("u", "snap-1", 3)
    w_units = _units("w", "snap-9", 3)
    for unit in w_units:
        unit["embedding"] = array("f", (0.0, 1.0, 0.0, 0.0))
    env.inputs.collected["kb-1"] = u_units + w_units
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [
            {"snapshot_id": "snap-1", "unit_count": 3,
             "directory_path": "方案"},
            {"snapshot_id": "snap-9", "unit_count": 3,
             "directory_path": "方案"}],
        "computed_at": "X"}
    await _bind(env)
    cid_u = _cluster_id(u_units)   # 页只挂 u 簇

    env.gen.queue = [
        _toc_json(["页A"], cid_u),
        _page_body("页A", refs=("u0", "w0")),   # w0 真实存在但跨簇
        _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    await _wait_state(env, "completed")

    page = await env.wdb.get_page(WID, 0, "页A")
    assert page["flags"]["dangling_refs"] == ["w0"]
    # provenance 记账同口径：跨簇引用不入账（只有簇内 u0）
    assert page["provenance"] == [
        {"kb_id": "kb-1", "snapshot_id": "snap-1", "unit_ids": ["u0"]}]


async def test_draft_exempt_manual_pages_not_flagged(env):
    """draft 宽免：manual 页不经编译管线改 flags（预置 flags 原样保留）。"""
    titles = ["页A"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    await env.wdb.upsert_page(
        WID, page_key="人工页", title="人工草稿页", body_md="> s\n\n- 裸1\n- 裸2\n- 裸3",
        frontmatter={"draft": True}, flags={"draft": True},
        origin="manual", updated_by="u-alice")
    env.gen.queue = [_toc_json(titles, cid), _page_body("页A"),
                     _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    await _wait_state(env, "completed")
    page = await env.wdb.get_page(WID, 0, "人工页")
    assert page["flags"] == {"draft": True}     # 编译未碰 manual 页 flags
    assert page["origin"] == "manual"


async def test_provenance_grouped_by_kb_and_snapshot(env):
    """provenance 按 (kb_id, snapshot_id) 分组：正文实际引用 ∩ 簇成员，kb 序
    稳定。"""
    titles = ["页A"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [
        _toc_json(titles, cid),
        _page_body("页A", refs=("u0", "u1", "v0")),
        _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    await _wait_state(env, "completed")
    page = await env.wdb.get_page(WID, 0, "页A")
    assert page["provenance"] == [
        {"kb_id": "kb-1", "snapshot_id": "snap-1", "unit_ids": ["u0", "u1"]},
        {"kb_id": "kb-2", "snapshot_id": "snap-2", "unit_ids": ["v0"]},
    ]
    # frontmatter：type/aliases/tags
    assert page["frontmatter"]["type"] == "topic"
    assert page["frontmatter"]["aliases"] == ["页A"]
    assert isinstance(page["frontmatter"].get("tags"), list)


async def test_provenance_fallback_first8_cluster_members(env):
    """正文零合法引用 → provenance 回退簇成员前 8（截 800 字摘要仍能取材）。"""
    titles = ["页A"]
    all_units = _seed_corpus(env, per_kb=6)   # 12 成员单簇
    await _bind_all(env)
    cid = _cluster_id(all_units)
    body = "# 页A\n\n> 摘要。\n\n## 核心事实\n- 事实一 [[src:uZZZ]]\n\n## 来源\n"
    env.gen.queue = [
        _toc_json(titles, cid), body, _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    await _wait_state(env, "completed")
    page = await env.wdb.get_page(WID, 0, "页A")
    ids = [uid for entry in page["provenance"] for uid in entry["unit_ids"]]
    assert ids == ["u0", "u1", "u2", "u3", "u4", "u5", "v0", "v1"]
    # 悬空引用仍入 flags
    assert page["flags"]["dangling_refs"] == ["uZZZ"]


# ------------------------------------------------------------ 质量页

async def test_quality_pages_frontmatter_strict(env):
    """contradiction/open_question/glossary 专页 frontmatter 与正文三节严格；
    open_question 合并 prepare 问题；glossary 截 30/问题截 20 记 stats。"""
    titles = ["页A", "页B"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    glossary = [{"term": f"术语{i}", "definition_hint": f"定义{i}"}
                for i in range(32)]
    open_questions = [{"question": f"预备问题{i}？", "why": "w"}
                      for i in range(21)]
    env.gen.queue = [
        _toc_json(titles, cid, glossary=glossary, open_questions=open_questions),
        _page_body("页A"), _page_body("页B"),
        _quality("页A", "页B")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)

    # contradiction 专页
    contra = await env.wdb.get_page(WID, 0, "矛盾-页A-与-页B")
    assert contra is not None
    assert contra["page_type"] == "contradiction"
    fm = contra["frontmatter"]
    assert fm["claim_a"] == "切片上限 100"
    assert fm["claim_b"] == "切片上限 200"
    assert fm["source_a"] == "页A" and fm["source_b"] == "页B"
    assert fm["severity"] == "medium"
    for section in ("## 双方主张", "## 分析", "## 裁决"):
        assert section in contra["body_md"]
    assert contra["provenance"]                # 来源账=双方页 provenance 并集

    # open_question：质量缺口 1 + prepare 21 → cap 20（缺口优先）
    assert det["stats"]["open_question_pages"] == 20
    assert det["stats"]["open_questions_dropped"] == 2
    oq_pages = [p for p in await env.wdb.list_pages(WID, revision=0)
                if p["page_type"] == "open_question"]
    assert len(oq_pages) == 20
    gap_page = await env.wdb.get_page(WID, 0, "待解-如何灰度扩容")
    assert gap_page is not None
    gap_fm = gap_page["frontmatter"]
    assert gap_fm["question"].endswith("？")
    assert gap_fm["priority"] == "medium"
    assert gap_fm["suggested_sources"] == []
    for section in ("## 问题", "## 上下文", "## 建议补材"):
        assert section in gap_page["body_md"]

    # glossary：cap 30
    assert det["stats"]["glossary_pages"] == 30
    assert det["stats"]["glossary_dropped"] == 2
    term0 = await env.wdb.get_page(WID, 0, "术语-术语0")
    assert term0 is not None
    assert term0["page_type"] == "glossary"
    assert term0["frontmatter"]["term"] == "术语0"
    assert term0["frontmatter"]["definition_source"] == "compile"
    for section in ("## 定义", "## 用法", "## 来源"):
        assert section in term0["body_md"]


async def test_quality_invalid_entries_skipped(env):
    """质量输出中 page_a/page_b 不在已写页 → 该矛盾条目跳过（不生成页）。"""
    titles = ["页A"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    quality = {"contradictions": [
        {"page_a": "不存在的页", "page_b": "页A", "claim_a": "x",
         "claim_b": "y", "severity": "high"}],
        "gaps": []}
    env.gen.queue = [_toc_json(titles, cid), _page_body("页A"), quality]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    batch = await _wait_state(env, "completed")
    det = _details(batch)
    assert det["stats"]["contradiction_pages"] == 0
    assert det["stats"]["open_question_pages"] == 0


# ------------------------------------------------------------ 发布校验（决策7）

async def _compile_clean(env) -> None:
    """干净语料编译到 completed（全部页带引用与 provenance）。"""
    titles = ["页A", "页B"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [
        _toc_json(titles, cid, glossary=[
            {"term": "网络切片", "definition_hint": "切分"}]),
        _page_body("页A", broken=False), _page_body("页B", broken=False),
        {"contradictions": [], "gaps": []}]
    await _start(env)
    await _wait_state(env, "awaiting_toc")
    toc = _details(await env.inputs.latest_compile_batch(WID))["toc"]
    await _confirm(env, toc)
    await _wait_state(env, "completed")


async def test_publish_validation_clean_zero_warnings(env):
    """干净编译产物发布：通过且 warnings 空。"""
    await _compile_clean(env)
    blocking, warnings = await validate_pages_for_publish(env.wdb, WID)
    assert blocking == [] and warnings == []
    result = await env.art_svc.publish(
        user=_user("u-alice"), artifact_id=WID, change_summary="rev1")
    assert result["revision"] == 1
    assert result["warnings"] == []


async def test_publish_validation_machine_missing_provenance_blocks(env):
    """machine topic 页 provenance 空 → 发布阻断：service 抛
    publish_validation_failed；路由特判 409。"""
    await _compile_clean(env)
    await env.wdb.upsert_page(
        WID, page_key="裸机页", title="裸机页", body_md="> s\n\n- 事实 [[src:u0]]",
        provenance=[], origin="machine", updated_by="system")

    blocking, warnings = await validate_pages_for_publish(env.wdb, WID)
    assert any("裸机页" in b and "provenance" in b for b in blocking)
    with pytest.raises(InvalidOperation) as exc_info:
        await env.art_svc.publish(
            user=_user("u-alice"), artifact_id=WID, change_summary="rev1")
    assert str(exc_info.value).startswith("publish_validation_failed")

    # 路由面：artifacts publish → 409 + code 信封
    token = env.adb.artifacts[WID]["updated_at"].isoformat()
    r = await env.client.post(f"/api/artifacts/{WID}/publish",
                              json={"change_summary": "x",
                                    "expected_updated_at": token})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "publish_validation_failed"


async def test_publish_validation_dangling_blocks_and_manual_warns(env):
    """悬空 src 引用 → blocking（任何页）；manual 页 provenance 空/密度 →
    warnings（不阻断发布）。"""
    await _compile_clean(env)
    # machine 页悬空引用
    await env.wdb.upsert_page(
        WID, page_key="悬空机页", title="悬空机页",
        body_md="> s\n\n- 事实 [[src:ghost-id]]",
        provenance=[{"kb_id": "kb-1", "snapshot_id": "snap-1",
                     "unit_ids": ["u0"]}],
        origin="machine", updated_by="system")
    blocking, _ = await validate_pages_for_publish(env.wdb, WID)
    assert any("悬空机页" in b for b in blocking)

    # 修复悬空后：manual 页问题只 warning
    await env.wdb.upsert_page(
        WID, page_key="悬空机页", title="悬空机页", body_md="> s",
        provenance=[{"kb_id": "kb-1", "snapshot_id": "snap-1",
                     "unit_ids": ["u0"]}],
        origin="machine", updated_by="system")
    await env.wdb.upsert_page(
        WID, page_key="人工裸页", title="人工裸页",
        body_md="> s\n\n- 裸1\n- 裸2\n- 裸3",
        provenance=[], origin="manual", updated_by="u-alice")
    blocking, warnings = await validate_pages_for_publish(env.wdb, WID)
    assert blocking == []
    assert any("人工裸页" in w and "provenance" in w for w in warnings)
    assert any("人工裸页" in w and "密度" in w for w in warnings)
    result = await env.art_svc.publish(
        user=_user("u-alice"), artifact_id=WID, change_summary="rev1")
    assert result["revision"] == 1 and len(result["warnings"]) == 2


async def test_publish_validation_exempt_derived_types(env):
    """专页（open_question/glossary machine 页）无直接取材 → provenance 检查
    豁免（不 blocking 不 warning）；contradiction 页带真实 provenance。"""
    await env.wdb.upsert_page(
        WID, page_key="待解-Q", title="待解-Q", page_type="open_question",
        frontmatter={"question": "Q？", "priority": "medium"},
        body_md="> s\n\n## 问题\n- Q", provenance=[],
        origin="machine", updated_by="system")
    await env.wdb.upsert_page(
        WID, page_key="术语-T", title="术语-T", page_type="glossary",
        frontmatter={"term": "T", "definition_source": "compile"},
        body_md="> s", provenance=[], origin="machine", updated_by="system")
    await env.wdb.upsert_page(
        WID, page_key="矛盾-M", title="矛盾-M", page_type="contradiction",
        frontmatter={"claim_a": "a", "claim_b": "b", "source_a": "p1",
                     "source_b": "p2", "severity": "low"},
        body_md="> s", provenance=[{"kb_id": "kb-1",
                                    "snapshot_id": "s", "unit_ids": ["u0"]}],
        origin="machine", updated_by="system")
    blocking, warnings = await validate_pages_for_publish(env.wdb, WID)
    assert blocking == [] and warnings == []


async def test_map_error_publish_validation_failed_409():
    """_map_artifact_error 特判：publish_validation_failed（含详情后缀）→ 409
    {code, message} 信封。"""
    exc = InvalidOperation("publish_validation_failed:页X: machine 页 provenance 缺失")
    http = _map_artifact_error(exc)
    assert http.status_code == 409
    assert http.detail["code"] == "publish_validation_failed"
    assert "provenance" in http.detail["message"]


# ------------------------------------------------------------ status 端点

async def test_status_endpoint_design_only_and_shape(env):
    """status：viewer 403（编译含未发布内容，design-only）；design 返回
    state/progress/stats/conflicts/toc(awaiting_toc)/error；无批次 404。"""
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/compile/status")
    assert r.status_code == 404 and "compile_not_found" in r.text

    titles = ["页A"]
    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [_toc_json(titles, cid), _page_body("页A"),
                     _quality("页A", "页A")]
    await _start(env)
    await _wait_state(env, "awaiting_toc")

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/compile/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "awaiting_toc"
    assert body["toc"][0]["page_hints"][0]["title"] == "页A"
    assert body["progress"] == {"batch": "0/0", "pages_done": 0}
    assert body["conflicts"] == [] and body["error"] is None

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/compile/status")
    assert r.status_code == 403


async def test_routes_four_endpoints_happy_and_errors(env):
    """四端点路由信封：POST compile → {batch_id, state}；status；toc 确认 →
    {state}；cancel → {state}；错误码 no_sources 422 / too_many_units 422 /
    compile_in_progress 409 / invalid_compile_state 409 / too_many_pages 422。"""
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 422 and "no_sources" in r.text

    all_units = _seed_corpus(env)
    await _bind_all(env)
    cid = _cluster_id(all_units)
    env.gen.queue = [_toc_json(["页A"], cid), _page_body("页A"),
                     _quality("页A", "页A")]
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "preparing" and r.json()["batch_id"]

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    body = {}
    while loop.time() < deadline:
        body = (await env.client.get(
            f"/api/artifacts/{WID}/wiki/compile/status")).json()
        if body.get("state") == "awaiting_toc":
            break
        await asyncio.sleep(0.001)
    assert body.get("state") == "awaiting_toc", body

    # 在飞二次 start → 409
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 409 and "compile_in_progress" in r.text

    # 超 200 页确认 → 422
    big = [{"section_title": "s", "page_hints": [
        {"title": f"页{i}", "cluster_ids": [cid], "page_type": "topic"}
        for i in range(MAX_PAGES + 1)]}]
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile/toc",
                              json={"toc": big})
    assert r.status_code == 422 and "too_many_pages" in r.text

    r = await env.client.post(
        f"/api/artifacts/{WID}/wiki/compile/toc", json={"toc": body["toc"]})
    assert r.status_code == 200 and r.json()["state"] == "writing"
    st = {}
    while loop.time() < deadline:
        st = (await env.client.get(
            f"/api/artifacts/{WID}/wiki/compile/status")).json()
        if st["state"] == "completed":
            break
        await asyncio.sleep(0.001)
    assert st.get("state") == "completed", st
    assert st["stats"]["pages_written"] == 1

    # 终态 cancel → 409 invalid_compile_state
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile/cancel",
                              json={})
    assert r.status_code == 409 and "invalid_compile_state" in r.text

    # 重新编译（completed 后可再 start）：machine 页覆盖路径
    env.gen.queue = [_toc_json(["页A"], cid), _page_body("页A"),
                     _quality("页A", "页A")]
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 200
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        st = (await env.client.get(
            f"/api/artifacts/{WID}/wiki/compile/status")).json()
        if st["state"] in ("awaiting_toc", "failed", "completed"):
            break
        await asyncio.sleep(0.001)
    assert st.get("state") in ("awaiting_toc", "failed", "completed"), st


async def test_status_error_state_surfaced(env):
    """failed 态 status 携带 error 字段。"""
    env.inputs.collected["kb-1"] = []
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "snap-1", "unit_count": 0,
                       "directory_path": "方案"}], "computed_at": "X"}
    await _bind(env)
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 200
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    body = {}
    while loop.time() < deadline:
        body = (await env.client.get(
            f"/api/artifacts/{WID}/wiki/compile/status")).json()
        if body.get("state") in ("failed", "completed"):
            break
        await asyncio.sleep(0.001)
    assert body.get("state") == "failed", body
    assert body["error"] == "no_units"


async def test_viewer_cannot_start_compile(env):
    """viewer start/confirm/cancel → 403（design 写面）。"""
    _seed_corpus(env)
    await _bind_all(env)
    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile", json={})
    assert r.status_code == 403
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile/toc",
                              json={"toc": []})
    assert r.status_code == 403
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/compile/cancel",
                              json={})
    assert r.status_code == 403
