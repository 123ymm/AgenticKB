"""Wiki 制品 MVP 全链验收（63号 §七 1-7 的本地化五项）—— 真 PG + Fake LLM。

门控与跑法同 test_wiki_db.py：``KB_RUN_POSTGRES_ACCEPTANCE=1``（本地真库
容器 wiki-b5-pg:55441；tests/conftest.py 的 async_pool/清库护栏照常生效），
无门控时整个模块收集期跳过。

服务装配面全真：真 ArtifactDB/WikiDB/CompileInputsDB（同一 async_pool）+
真 ArtifactService(adb, wiki=lifecycle)/WikiLifecycle/WikiService/CompileService；
假件只有两枚——

- FakeGenerationClient（import 自 test_wiki_compile）：可编程页正文出队，
  按 CompileService._generate_page 的调用形态（to_thread + wiki-page 幂等键
  前缀 + pipeline_stage）模拟编译 LLM 产物；
- _FlatTaskClient：embedding 任务通道压平（提交即抛 LlmTaskError——发布
  后置的异步 embedding 立即终局、units 稳定保持 pending 且不向上抛）；向量
  由 wdb.update_unit_embeddings 直写真 1024 维（llm_service 异步通道回填
  属内网验收项，见批次5计划验收矩阵）。

**编译产页的边界裁定（批次5 Task 3）**：用例 1/4/5 的「Fake 编译产页」绕过
完整 CompileService（其全链已有 test_wiki_compile.py 39 用例覆盖），以
wdb.upsert_page(origin='machine') 直写 rev0 模拟编译产物（provenance/flags
按真管线口径构造）；**例外**——用例 2（origin 保护）与用例 3（引用规范）
必须打真实现：page_flags / write_machine_page / validate_pages_for_publish
（自 wiki_compile_pages import 直调，喂 Fake 语料）。乐观锁全程真实：每次
服务层写操作前取最新 updated_at 令牌（全精度微秒比对）。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip(
        "set KB_RUN_POSTGRES_ACCEPTANCE=1 to run real PostgreSQL tests",
        allow_module_level=True,
    )

from knowledge_mining.mining.infra.llm_task_client import LlmTaskError  # noqa: E402
from knowledge_mining.mining.kb.db import KbDB  # noqa: E402
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB  # noqa: E402
from knowledge_mining.mining.kb.db_compile_inputs import CompileInputsDB  # noqa: E402
from knowledge_mining.mining.kb.db_wiki import WikiDB, WikiError  # noqa: E402
from knowledge_mining.mining.kb.services.artifact_service import (  # noqa: E402
    ArtifactService,
    InvalidOperation,
)
from knowledge_mining.mining.kb.services.wiki_compile_pages import (  # noqa: E402
    page_flags,
    validate_pages_for_publish,
    write_machine_page,
)
from knowledge_mining.mining.kb.services.wiki_compile_service import (  # noqa: E402
    CompileService,
)
from knowledge_mining.mining.kb.services.wiki_service import (  # noqa: E402
    WikiLifecycle,
    WikiService,
    _EMBED_TASKS,
)
from knowledge_mining.tests.kb.test_wiki_compile import (  # noqa: E402
    FakeGenerationClient,
)

DOMAIN = "plant-a"
#: Fake 语料簇成员（kb-1 / snap-1 的检索单元 id——provenance 记账与引用
#: 校验的 valid_unit_ids 素材）。
CORPUS_UNITS = ("u0", "u1")
#: 真 1024 维向量（update_unit_embeddings 直写——生产由 llm_service 回填）。
VEC_1024 = [0.5] * 1024


class _FlatTaskClient:
    """embedding 任务通道压平桩：submit_embedding 即抛 LlmTaskError ——
    WikiLifecycle 发布后置的 _embed_units 走「提交瞬时失败 → 保持 pending
    待自愈」分支立即终局。验收用例的向量全部由 wdb.update_unit_embeddings
    直写（1024 维真列），llm_service 异步通道属内网验收项。"""

    def submit_embedding(self, texts, *, idempotency_key=None, max_attempts=3,
                         priority=100, knowledge_domain=None) -> str:
        raise LlmTaskError("acceptance: embedding 通道压平（向量走直写）")

    def wait_for_tasks(self, task_ids, *, timeout=None, poll_interval=None):
        return {}


# ---------------------------------------------------------------- 语料构造

def _body(title: str, refs: tuple[str, ...] = CORPUS_UNITS) -> str:
    """合法编译页正文（LLM-Wiki 四节风格）：每事实句带 [[src:]] 引用——
    密度零告警；refs 全在语料内——零悬空。"""
    lines = [f"# {title}", "", f"> {title} 的摘要一句话。", "", "## 核心事实"]
    for i, ref in enumerate(refs):
        lines.append(f"- {title} 事实{i} [[src:{ref}]]")
    lines += ["", "## 来源"]
    lines += [f"- [[src:{ref}]] -- 单元摘要一句话" for ref in refs]
    return "\n".join(lines)


def _provenance(unit_ids: tuple[str, ...] = CORPUS_UNITS) -> list[dict]:
    """真管线 provenance 口径：按 (kb_id, snapshot_id) 分组的取材账。"""
    return [{"kb_id": "kb-1", "snapshot_id": "snap-1",
             "unit_ids": list(unit_ids)}]


def _long_body(title: str) -> str:
    """超长页正文：详述节 5 段 ×~470 字 ≈ 2350 字 > 2000 上限——
    chunk_page_for_units 分节 + 段落贪心合并分块素材（§5.3）。"""
    paras = "\n\n".join(
        f"第{i}段：" + "云核切片参数与约束的事实陈述。" * 36 for i in range(5))
    return _body(title, ("u0",)) + f"\n\n## 详述\n\n{paras}\n"


# ---------------------------------------------------------------- 装配/工具

async def _drain_embeds() -> None:
    """等待全部后台 embed 任务收尾。只排空 _EMBED_TASKS 强引用集（精确），
    不做 all_tasks 一网打尽——真库 AsyncConnectionPool 的后台 worker 是常驻
    任务，gather 会挂死。"""
    for task in list(_EMBED_TASKS):
        try:
            await task
        except Exception:  # noqa: BLE001
            pass


async def _llm_page(env, title: str) -> str:
    """Fake GenerationClient 产页正文——按 CompileService._generate_page 的
    调用形态（to_thread + wiki-page 幂等键前缀 + pipeline_stage）出队一条
    预置正文。"""
    return str(await asyncio.to_thread(
        env.gen.execute,
        [{"role": "system", "content": "wiki 页写作（验收桩）"},
         {"role": "user", "content": f"页面标题：{title}"}],
        expected_output_type="text",
        idempotency_key=f"wiki-page:acceptance:{title}",
        pipeline_stage="wiki_compile",
    ))


async def _machine_page(env, aid: str, *, page_key: str, title: str,
                        ordinal: int, body: str | None = None,
                        unit_ids: tuple[str, ...] = CORPUS_UNITS) -> str:
    """模拟编译产物直写 rev0（用例 1/4/5 的裁定路径——绕过完整编译管线）：
    origin='machine' + provenance 记账 + 干净 flags。返回写入正文（快照
    比对用）。"""
    text = body if body is not None else _body(title, unit_ids)
    await env.wdb.upsert_page(
        aid, page_key=page_key, title=title, page_type="topic",
        frontmatter={"type": "topic", "aliases": [title], "tags": [title]},
        body_md=text, provenance=_provenance(unit_ids), flags={},
        ordinal=ordinal, origin="machine", updated_by="wiki_compile")
    return text


async def _compile_page(env, aid: str, *, page_key: str, title: str,
                        body: str, ordinal: int,
                        unit_ids: tuple[str, ...] = CORPUS_UNITS) -> str:
    """真编译产页路径（用例 2/3/5 例外裁定）：page_flags（真引用校验三件）
    + write_machine_page（真 origin 保护写）。返回 written/conflict。"""
    provenance = _provenance(unit_ids)
    flags = page_flags("topic", provenance, body, set(unit_ids))
    return await write_machine_page(
        env.wdb, aid, page_key=page_key, title=title, page_type="topic",
        frontmatter={"type": "topic", "aliases": [title], "tags": [title]},
        body_md=body, provenance=provenance, flags=flags,
        ordinal=ordinal, user="wiki_compile")


async def _token(env, aid):
    """取最新乐观锁令牌（全精度微秒）——「每次操作取最新 updated_at」的
    验收口径：令牌随每次页写/发布/回滚前进，旧令牌必被拒。"""
    return (await env.adb.get_artifact(aid))["updated_at"]


@pytest_asyncio.fixture
async def env(async_pool):
    """全真装配 + 两枚假件（Fake LLM / 压平任务通道）。每用例独立用户/制品
    （conftest 每测前清库）；teardown 精确排空后台 embed 任务。"""
    kbdb = KbDB(async_pool)
    adb = ArtifactDB(async_pool)
    wdb = WikiDB(async_pool)
    inputs = CompileInputsDB(async_pool)
    owner_row = await kbdb.create_user(
        username=f"wiki-acc-{uuid.uuid4().hex[:8]}")
    await kbdb.bind_domain(user_id=owner_row["id"], domain=DOMAIN)
    owner = {
        "id": str(owner_row["id"]), "username": str(owner_row["username"]),
        "site_role": str(owner_row.get("site_role") or "member"),
        "status": str(owner_row.get("status") or "active"),
    }
    lifecycle = WikiLifecycle(wdb, _FlatTaskClient())
    art_svc = ArtifactService(adb, wiki=lifecycle)
    wiki_svc = WikiService(art_svc, wdb, lifecycle, inputs_db=inputs)
    gen = FakeGenerationClient()
    compile_svc = CompileService(wdb, inputs, gen, wiki_svc)
    try:
        yield SimpleNamespace(
            adb=adb, wdb=wdb, inputs=inputs, owner=owner,
            art_svc=art_svc, wiki_svc=wiki_svc, gen=gen,
            compile_svc=compile_svc,
        )
    finally:
        await _drain_embeds()


async def _make_wiki(env) -> str:
    """建 wiki 制品 + usage_note（发布门禁）——owner 即当前用户。"""
    art = await env.art_svc.create_artifact(
        user=env.owner, domain_id=DOMAIN, kind="wiki", title="云核 Wiki 验收")
    await env.art_svc.update_meta(
        user=env.owner, artifact_id=art["artifact_id"],
        fields={"usage_note": "团队云核词条库（验收）"})
    return art["artifact_id"]


# ------------------------------------------------------------ 用例1 版本机制

async def test_acceptance_version_lifecycle(env):
    """§七4 版本机制回归：编译产 2 页→发布 rev1（页面清单快照/units 投影
    pending）→改一页+加一页→发布 rev2（rev1 快照不变/rev2 units 重建）→
    回滚（纯指针拨回）→discard（rev0 清空+draft_open=False）。"""
    aid = await _make_wiki(env)

    # CompileService 全真装配烟测：真 adb 角色面 + 真 inputs 批次查询
    with pytest.raises(WikiError, match="compile_not_found"):
        await env.compile_svc.status(env.owner, aid)

    # Fake 编译产 2 页落 rev0（裁定：绕过完整编译管线）
    await env.wdb.ensure_draft(aid)
    body_a = await _machine_page(env, aid, page_key="页A", title="页A",
                                 ordinal=1001)
    await _machine_page(env, aid, page_key="页B", title="页B", ordinal=1002)

    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1 and result["row_count"] == 2
    rev1 = await env.adb.get_revision(aid, 1)
    assert rev1 is not None and rev1["row_count"] == 2
    assert [p["page_key"] for p in rev1["schema_json"]["pages"]] == ["页A", "页B"]
    assert all(p["origin"] == "machine" for p in rev1["schema_json"]["pages"])
    art = await env.adb.get_artifact(aid)
    assert art["current_revision"] == 1 and art["status"] == "published"
    assert art["draft_open"] is False
    # 发布后置投影：2 条 pending（embedding 通道压平——稳定保持）
    await _drain_embeds()
    assert await env.wdb.units_status(aid, 1) == {
        "total": 2, "done": 0, "failed": 0, "pending": 2}

    # 改一页（人工——§3.2 origin 翻 manual）+ 加一页（machine）→ 3 页
    await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A",
        body_md=_body("页A", ("u0",)),
        expected_updated_at=await _token(env, aid))
    await _machine_page(env, aid, page_key="页C", title="页C", ordinal=1003)

    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev2",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 2 and result["row_count"] == 3
    # rev1 快照 2 页不变（双版本并存）；rev0 已清
    assert await env.wdb.count_pages(aid, 1) == 2
    assert (await env.wdb.get_page(aid, 1, "页A"))["body_md"] == body_a
    assert await env.wdb.count_pages(aid, 0) == 0
    # rev2 units 重建 3 条；rev1 units 按版本保留（回滚后检索直接用）
    await _drain_embeds()
    assert (await env.wdb.units_status(aid, 2))["total"] == 3
    assert (await env.wdb.units_status(aid, 1))["total"] == 2

    # 回滚到 1：纯指针拨回（rev0/rev2 数据不动）
    art = await env.art_svc.rollback_to(
        user=env.owner, artifact_id=aid, revision=1,
        expected_updated_at=await _token(env, aid))
    assert art["current_revision"] == 1
    assert await env.wdb.count_pages(aid, 0) == 0        # rev0 保持发布后清空态
    assert await env.wdb.count_pages(aid, 2) == 3        # rev2 快照仍可读

    # discard：开草稿乱改后丢弃 → rev0 清空 + draft_open=False
    await env.wdb.ensure_draft(aid)                      # checkout rev1 → 2 页
    await _machine_page(env, aid, page_key="页D", title="页D", ordinal=1004)
    assert await env.wdb.count_pages(aid, 0) == 3
    art = await env.art_svc.discard_draft(
        user=env.owner, artifact_id=aid,
        expected_updated_at=await _token(env, aid))
    assert art["draft_open"] is False
    assert await env.wdb.count_pages(aid, 0) == 0
    assert await env.wdb.list_links(aid, revision=0) == []


# ------------------------------------------------------------ 用例2 origin 保护

async def test_acceptance_origin_protection(env):
    """§七5：编译产页→人工改页（origin 翻 manual）→重编译（Fake LLM 对同键
    返回不同内容）→manual 页逐字不动 + conflicts 账记录 machine 被跳过；
    machine 页 B 正常覆盖更新（重编译语义）。真 write_machine_page。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)

    # 编译产页 A / 页 B（真编译写入口）
    assert await _compile_page(env, aid, page_key="页A", title="页A",
                               body=_body("页A"), ordinal=1001) == "written"
    body_b_v1 = _body("页B")
    assert await _compile_page(env, aid, page_key="页B", title="页B",
                               body=body_b_v1, ordinal=1002) == "written"

    # 人工改页 A（WikiService——§3.2 origin 翻 manual）
    manual_body = "> 人工定稿摘要。\n\n## 核心事实\n- 人工核查后的事实 [[src:u0]]"
    page = await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A", body_md=manual_body,
        expected_updated_at=await _token(env, aid))
    assert page["origin"] == "manual" and page["body_md"] == manual_body

    # 重编译：Fake LLM 对 A/B 各返回不同内容（真调用形态出队）
    env.gen.queue = [_body("页A-机2", ("u1",)), _body("页B-机2", ("u1",))]
    conflicts: list[dict] = []
    pages_written = 0
    for key, ordinal in (("页A", 1001), ("页B", 1002)):
        body = await _llm_page(env, key)
        outcome = await _compile_page(
            env, aid, page_key=key, title=key, body=body, ordinal=ordinal)
        if outcome == "conflict":
            # conflicts 账（记录形态对齐 _write_one_topic_page 真管线）
            existing = await env.wdb.get_page(aid, 0, key)
            conflicts.append({
                "page_key": key,
                "manual_title": str((existing or {}).get("title") or ""),
                "machine_title": key,
            })
        else:
            pages_written += 1

    # manual 页 A 逐字不动；machine 页 B 覆盖为新内容
    page_a = await env.wdb.get_page(aid, 0, "页A")
    assert page_a["origin"] == "manual"
    assert page_a["body_md"] == manual_body
    page_b = await env.wdb.get_page(aid, 0, "页B")
    assert page_b["origin"] == "machine"
    assert page_b["body_md"] != body_b_v1
    assert "页B-机2" in page_b["body_md"]
    # conflicts 账恰好一条（页A 被 machine 跳过）；B 正常写入
    assert conflicts == [{"page_key": "页A", "manual_title": "页A",
                          "machine_title": "页A"}]
    assert pages_written == 1
    # Fake LLM 调用形态：pipeline_stage 与幂等键前缀（真管线口径）
    assert env.gen.calls
    assert all(c["pipeline_stage"] == "wiki_compile" for c in env.gen.calls)
    assert all(c["idempotency_key"].startswith("wiki-page:")
               for c in env.gen.calls)


# ------------------------------------------------------------ 用例3 引用规范

async def test_acceptance_citation_rules(env):
    """§七6：编译期 flags（悬空计数正确/密度告警置位/合法页零告警——真
    page_flags）+ 发布前校验（真 validate_pages_for_publish：悬空 blocking →
    发布 publish_validation_failed（409 家族）；修掉悬空后放行、密度降级
    warning 不阻断）。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)

    # Fake LLM 预置两页正文：合法页（引用全命中+密度干净）/ 违规页（悬空×2
    # + 连续 4 句无引用）
    env.gen.queue = [
        _body("合法页"),
        ("# 违规页\n\n> 违规页 摘要。\n\n## 核心事实\n"
         "- 违规页 事实0 [[src:u0]]\n"
         "- 违规页 裸事实1（无引用）\n- 违规页 裸事实2（无引用）\n"
         "- 违规页 裸事实3（无引用）\n- 违规页 裸事实4（无引用）\n"
         "- 违规页 事实5 [[src:ghost-1]] [[src:ghost-2]]\n\n"
         "## 来源\n- [[src:u1]] -- 单元摘要\n"),
    ]
    legal_body = await _llm_page(env, "合法页")
    bad_body = await _llm_page(env, "违规页")

    # 编译期 flags（真 page_flags：悬空口径=页簇成员并集）
    provenance = _provenance()
    assert page_flags("topic", provenance, legal_body,
                      set(CORPUS_UNITS)) == {}            # 合法页零告警
    bad_flags = page_flags("topic", provenance, bad_body, set(CORPUS_UNITS))
    assert bad_flags["dangling_refs"] == ["ghost-1", "ghost-2"]   # 计数正确
    assert bad_flags["density_warning"] is True

    # 落 rev0（真 write_machine_page——flags 随页存储）
    assert await _compile_page(env, aid, page_key="合法页", title="合法页",
                               body=legal_body, ordinal=1001) == "written"
    assert await _compile_page(env, aid, page_key="违规页", title="违规页",
                               body=bad_body, ordinal=1002) == "written"
    stored = await env.wdb.get_page(aid, 0, "违规页")
    assert stored["flags"]["dangling_refs"] == ["ghost-1", "ghost-2"]
    assert stored["flags"]["density_warning"] is True

    # 发布前校验：悬空 blocking（口径=rev0 provenance 并集）；密度 warning
    blocking, warnings = await validate_pages_for_publish(env.wdb, aid)
    assert any("违规页" in b and "悬空" in b for b in blocking)
    assert not any("合法页" in b for b in blocking)
    assert any("违规页" in w and "密度" in w for w in warnings)
    assert not any("合法页" in w for w in warnings)

    # 发布 → publish_validation_failed（409 家族；校验在事务外——零副作用）
    with pytest.raises(InvalidOperation, match="publish_validation_failed"):
        await env.art_svc.publish(
            user=env.owner, artifact_id=aid, change_summary="rev1",
            expected_updated_at=await _token(env, aid))
    assert await env.adb.get_revision(aid, 1) is None     # 未落版本行
    assert await env.wdb.count_pages(aid, 0) == 2         # rev0 保留

    # 修掉悬空（重编译语义改页：ghost → 语料内引用）→ 发布成功；密度降 warning
    fixed_body = bad_body.replace("[[src:ghost-1]]", "[[src:u0]]") \
                         .replace("[[src:ghost-2]]", "[[src:u1]]")
    assert await _compile_page(env, aid, page_key="违规页", title="违规页",
                               body=fixed_body, ordinal=1002) == "written"
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1
    assert any("密度" in w for w in result["warnings"])


# ------------------------------------------------------------ 用例4 投影链

async def test_acceptance_units_projection_chain(env):
    """§七3 Python 侧：发布→投影 pending→直写 1024 维向量→done=2→超长页
    分节（>2000 字 → 多 unit）→重投影幂等→pending 单元不进 done 计数。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    await _machine_page(env, aid, page_key="页B", title="页B", ordinal=1002)

    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1
    await _drain_embeds()
    assert await env.wdb.units_status(aid, 1) == {
        "total": 2, "done": 0, "failed": 0, "pending": 2}

    # 直写真 1024 维向量（生产=llm_service 异步回填；验收绕通道直写）
    pending = await env.wdb.list_pending_units(aid, 1)
    keys = [u["unit_key"] for u in pending]
    assert sorted(keys) == ["页A", "页B"]                # 短页单块=页键
    await env.wdb.update_unit_embeddings(
        aid, 1, keys, [VEC_1024 for _ in keys])
    assert await env.wdb.units_status(aid, 1) == {
        "total": 2, "done": 2, "failed": 0, "pending": 0}

    # 超长页分节：页A 正文 >2000 字 → 再发布 rev2 → 多 unit（unit_key#sN）
    await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A",
        body_md=_long_body("页A"),
        expected_updated_at=await _token(env, aid))
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev2",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 2
    await _drain_embeds()
    units = await env.wdb.list_pending_units(aid, 2, limit=50)
    a_units = [u for u in units if u["page_key"] == "页A"]
    assert len(a_units) >= 2                             # 分节 → 多 unit
    assert any(u["unit_key"].startswith("页A#s") for u in a_units)
    assert len([u for u in units if u["page_key"] == "页B"]) == 1

    # 重投影幂等：同 revision 再投条数不变（先 DELETE 再 INSERT 的投影语义）
    count = await env.wdb.project_units(aid, 2)
    assert count == len(units)
    assert await env.wdb.project_units(aid, 2) == count

    # pending 单元不进 done 计数：只写首个单元向量
    first = (await env.wdb.list_pending_units(aid, 2, limit=1))[0]["unit_key"]
    await env.wdb.update_unit_embeddings(aid, 2, [first], [VEC_1024])
    status = await env.wdb.units_status(aid, 2)
    assert status["done"] == 1
    assert status["pending"] == status["total"] - 1
    assert status["failed"] == 0


# ------------------------------------------------------------ 用例5 编辑闭环

async def test_acceptance_manual_edit_loop(env):
    """§七7 人工编辑闭环：编译→发布→人工改页 X（origin manual）→重编译不丢
    （真 write_machine_page conflict）→再发布→rev2 units 的 X content_text
    含新内容（「改完能搜到」的存储层证明）。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页X", title="页X", ordinal=1001)
    await _machine_page(env, aid, page_key="页Y", title="页Y", ordinal=1002)
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1

    # 人工改页 X：新内容 + origin 翻 manual（服务层自动 checkout rev1→rev0）
    marker = "人工修订后的可检索事实"
    new_body = f"> 人工摘要。\n\n## 核心事实\n- {marker} [[src:u0]]"
    page = await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页X", title="页X", body_md=new_body,
        expected_updated_at=await _token(env, aid))
    assert page["origin"] == "manual" and page["body_md"] == new_body

    # 重编译不丢：machine 同键重写（Fake LLM 新内容）→ conflict，X 逐字保持
    env.gen.queue = [_body("页X", ("u1",))]
    machine_body = await _llm_page(env, "页X")
    assert await _compile_page(env, aid, page_key="页X", title="页X",
                               body=machine_body, ordinal=1001) == "conflict"
    assert (await env.wdb.get_page(aid, 0, "页X"))["body_md"] == new_body

    # 再发布 → rev2 units 的 X content_text 含新内容（改完能搜到）
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="rev2",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 2
    await _drain_embeds()
    units = await env.wdb.list_pending_units(aid, 2, limit=32)
    x_units = [u for u in units if u["page_key"] == "页X"]
    assert x_units and any(marker in u["content_text"] for u in x_units)
