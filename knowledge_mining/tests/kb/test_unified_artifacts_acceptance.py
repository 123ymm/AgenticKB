"""统一界面五闭环全链验收（66号 §16）—— 真 PG + Fake LLM/转发桩。

门控与跑法同 test_wiki_acceptance.py：``KB_RUN_POSTGRES_ACCEPTANCE=1``，
无门控时整个模块收集期跳过。装配面照 test_wiki_acceptance（真
KbDB/ArtifactDB/WikiDB/CompileInputsDB/ArtifactService/WikiLifecycle/
WikiService/CompileService 同一 async_pool + _FlatTaskClient 压平 embedding
通道），另挂 ASGI 路由面（artifacts + wiki 两 router）供状态码口径断言。

**去重盘点（写之前先盘点——目标是用例有增量价值，不是重复绿）：**

已被既有验收覆盖、本文件**不重写**（引用）：

- 表格闭环（创建→Excel 首导→编辑→diff→发布→追加映射→回滚→再发布不撞号）：
  test_artifact_acceptance.py 九用例（61号 §十二，路由级）+ 真库
  test_artifacts_db.py。统一界面未新增表格服务端能力，回归以该两文件为准。
- Wiki 版本机制/编译 origin 保护/引用规范/投影链/人工编辑闭环：本 worktree
  合并前的 test_wiki_acceptance.py 五用例（真库）——63号 §七 验收面。
- toc 路由级权限矩阵/行级乐观锁/发布 warnings、unit-source 三门、reproject
  权限面：test_wiki_routes.py（fake 路由级，Task 1.4/5.2/5.3 TDD 产物）。
- unit-source DB JOIN（provenance 门+原文退役）：test_wiki_db.py。
- 页级 diff 的 DB 层形状（old/new+hunks+截断）：test_wiki_db.py。

本文件**新写**（统一界面新增能力的真库服务级串联）：

1. Wiki 闭环：toc 全链——completed 落 draft toc（真 derive_compile_toc 派生）
   →人工改页→save_toc 编辑→页级 diff 内容（changes old/new+body hunks）→
   发布 schema_json.toc 定版（节标题+页键+无死键）→运行态 toc=快照→
   搜索转发桩（artifact_ref 口径）→断链（links broken）。
2. 权限闭环：owner/editor/viewer×(toc/pages/links draft/发布/回滚) 路由级；
   editor 不可改可见性；public+钥匙开放子集（mcp_key_open_artifacts 写入
   +key_open_artifact_ids 谓词断言——serving 侧滤除由 Java IT 覆盖）；
   停用→恢复；artifact_ref 进 web 响应。
3. 域撤销（66号 §11）：成员被移出域后制品读写全拒（直调服务不能绕过），
   钥匙开放子集随撤域静默收缩（子集不放大）。
4. 保存闭环：toc PUT 乐观锁（stale→409/行存在缺令牌→409）；元信息 PATCH
   即时生效；discard 不动 toc 行（设计决策：discard 只弃页草稿）。
5. 异常闭环：并发发布冲突(409)、来源无权 404、编译失败态（status 可辨识）、
   投影 failed→reproject 幂等重建、vector 未就绪 status 口径（pending 计数）。
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

if os.environ.get("KB_RUN_POSTGRES_ACCEPTANCE") != "1":
    pytest.skip(
        "set KB_RUN_POSTGRES_ACCEPTANCE=1 to run real PostgreSQL tests",
        allow_module_level=True,
    )

from knowledge_mining.mining.kb.auth import current_user  # noqa: E402
from knowledge_mining.mining.kb.db import KbDB  # noqa: E402
from knowledge_mining.mining.kb.db_artifacts import ArtifactDB  # noqa: E402
from knowledge_mining.mining.kb.db_compile_inputs import CompileInputsDB  # noqa: E402
from knowledge_mining.mining.kb.db_wiki import WikiDB  # noqa: E402
from knowledge_mining.mining.kb.db_wiki_toc import derive_compile_toc  # noqa: E402
from knowledge_mining.mining.kb.deps import (  # noqa: E402
    get_artifact_db,
    get_artifact_service,
    get_wiki_service,
)
from knowledge_mining.mining.kb.routes.artifacts import (  # noqa: E402
    router as artifacts_router,
)
from knowledge_mining.mining.kb.routes.wiki import router as wiki_router  # noqa: E402
from knowledge_mining.mining.kb.services.artifact_service import (  # noqa: E402
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
    StaleArtifact,
)
from knowledge_mining.mining.kb.services.mcp_key_service import (  # noqa: E402
    McpKeyError,
    McpKeyService,
)
from knowledge_mining.mining.kb.services.wiki_compile_service import (  # noqa: E402
    CompileService,
)
from knowledge_mining.mining.kb.services.wiki_service import (  # noqa: E402
    WikiLifecycle,
    WikiService,
)
from knowledge_mining.tests.kb.test_wiki_acceptance import (  # noqa: E402
    _FlatTaskClient,
    _body,
    _drain_embeds,
    _provenance,
)
from knowledge_mining.tests.kb.test_wiki_compile import (  # noqa: E402
    FakeGenerationClient,
)

DOMAIN = "cloud_core_network"
OTHER_DOMAIN = "odn"
REF_PREFIX = "art_"


class _StubForward:
    """搜索转发桩（serving internal 通道的 Python 侧替身）：只记参数、回
    serving 契约同形响应。断言点=artifact_ref 拼法与 domain/username 透传。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, *, artifact_ref: str, q: str, mode: str, top_k: int,
                     domain: str, username: str) -> dict:
        self.calls.append({
            "artifact_ref": artifact_ref, "q": q, "mode": mode,
            "top_k": top_k, "domain": domain, "username": username,
        })
        return {
            "view": "wiki_search", "revision": 1, "degraded_dense": False,
            "results": [{"page_key": "页A", "title": "页A",
                         "unit_key": "页A", "snippet": q, "score": 1.0,
                         "channels": ["keyword"]}],
        }


async def _new_user(kbdb: KbDB, prefix: str, *domains: str) -> dict:
    row = await kbdb.create_user(username=f"{prefix}-{uuid.uuid4().hex[:8]}")
    for domain in (domains or (DOMAIN,)):
        await kbdb.bind_domain(user_id=row["id"], domain=domain)
    return {
        "id": str(row["id"]), "username": str(row["username"]),
        "site_role": str(row.get("site_role") or "member"),
        "status": str(row.get("status") or "active"),
    }


async def _make_wiki(env, user: dict | None = None) -> str:
    """建 wiki 制品 + usage_note（发布门禁）。缺省 owner。"""
    art = await env.art_svc.create_artifact(
        user=user or env.owner, domain_id=DOMAIN, kind="wiki",
        title="统一界面 Wiki 验收")
    await env.art_svc.update_meta(
        user=user or env.owner, artifact_id=art["artifact_id"],
        fields={"usage_note": "统一界面五闭环验收用"})
    return art["artifact_id"]


async def _machine_page(env, aid: str, *, page_key: str, title: str,
                        ordinal: int, body: str | None = None) -> str:
    """模拟编译产物直写 rev0（裁定同 test_wiki_acceptance：绕过完整编译
    管线——其全链已有 test_wiki_compile.py 覆盖）。"""
    text = body if body is not None else _body(title)
    await env.wdb.upsert_page(
        aid, page_key=page_key, title=title, page_type="topic",
        frontmatter={"type": "topic", "aliases": [title]},
        body_md=text, provenance=_provenance(), flags={},
        ordinal=ordinal, origin="machine", updated_by="wiki_compile")
    return text


async def _seed_compile_toc(env, aid: str, confirmed: list[dict]) -> dict:
    """编译 completed 钩子的目录首落（Task 1.3 同款口径：真 derive_compile_toc
    派生 + upsert_draft_toc 跳锁整体替换——照 _seed_draft_toc 逐行语义）。"""
    manifest = await env.wdb.list_pages(aid, revision=0)
    toc = derive_compile_toc(manifest, confirmed)
    return await env.wdb.upsert_draft_toc(aid, toc, updated_by="wiki_compile")


async def _token(env, aid) -> str:
    return (await env.adb.get_artifact(aid))["updated_at"]


@pytest_asyncio.fixture
async def env(async_pool):
    """全真装配（同 test_wiki_acceptance）+ 三协作真用户 + ASGI 路由面 +
    钥匙服务（权限闭环用）。teardown 排空后台 embed 任务。"""
    kbdb = KbDB(async_pool)
    adb = ArtifactDB(async_pool)
    wdb = WikiDB(async_pool)
    inputs = CompileInputsDB(async_pool)
    owner = await _new_user(kbdb, "uni-own")
    editor = await _new_user(kbdb, "uni-edt")
    viewer = await _new_user(kbdb, "uni-viw")
    key_owner = await _new_user(kbdb, "uni-key")          # 钥匙主人（撤域用例）
    foreign = await _new_user(kbdb, "uni-fgn", OTHER_DOMAIN)  # 跨域制品 owner
    lifecycle = WikiLifecycle(wdb, _FlatTaskClient())
    art_svc = ArtifactService(adb, wiki=lifecycle)
    wiki_svc = WikiService(art_svc, wdb, lifecycle, inputs_db=inputs)
    compile_svc = CompileService(wdb, inputs, FakeGenerationClient(), wiki_svc)
    key_svc = McpKeyService(kbdb)
    app = FastAPI()
    app.include_router(artifacts_router)
    app.include_router(wiki_router)
    holder = {"user": owner}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_artifact_db] = lambda: adb
    app.dependency_overrides[get_artifact_service] = lambda: art_svc
    app.dependency_overrides[get_wiki_service] = lambda: wiki_svc
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
                transport=transport, base_url="http://test") as client:
            yield SimpleNamespace(
                kbdb=kbdb, adb=adb, wdb=wdb, inputs=inputs,
                owner=owner, editor=editor, viewer=viewer,
                key_owner=key_owner, foreign=foreign,
                art_svc=art_svc, wiki_svc=wiki_svc, compile_svc=compile_svc,
                key_svc=key_svc, client=client, holder=holder,
            )
    finally:
        await _drain_embeds()


# --------------------------------------------------- 闭环2：Wiki toc 全链

async def test_loop_wiki_toc_chain(env):
    """Wiki 闭环（66号 §16 行2）：创建→绑源（真 kb）→completed 落 draft toc
    →人工改页→save_toc 编辑→页级 diff 内容→发布 r1（schema_json.toc 定版：
    节标题+页键+无死键）→运行态 toc=快照→搜索转发桩（artifact_ref）→断链。"""
    aid = await _make_wiki(env)
    # viewer 加成员（运行态读/搜索转发的权限面）
    await env.art_svc.upsert_member(
        user=env.owner, artifact_id=aid,
        member_user_id=env.viewer["id"], role="viewer")
    kb = await env.kbdb.create_kb(
        domain=DOMAIN, name="云核语料库", owner_id=env.owner["id"])
    src = await env.wiki_svc.upsert_source(
        user=env.owner, artifact_id=aid, kb_id=kb["id"], directories=[])
    assert src["scope"] == {"directories": []}          # 空数组=全库

    # 编译 completed 落 draft toc（真派生；两页归一节）
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001,
                        body=_body("页A") + "\n\n延伸阅读：[[页B]]")
    await _machine_page(env, aid, page_key="页B", title="页B", ordinal=1002)
    toc_row = await _seed_compile_toc(
        env, aid, [{"section_title": "云核词条",
                    "page_hints": [{"title": "页A"}, {"title": "页B"}]}])
    assert [s["section_title"] for s in toc_row["toc"]["sections"]] == ["云核词条"]

    # 人工改页（origin 保护面已由 test_wiki_acceptance 用例2 覆盖——此处只
    # 改内容供 diff）+ 断链制造（删 页B → 页A→页B 边落 broken）
    await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A",
        body_md="> 人工摘要。\n\n## 核心事实\n- 人工修订事实 [[src:u0]]\n\n"
                "延伸阅读：[[页B]]",
        expected_updated_at=await _token(env, aid))
    await env.wiki_svc.delete_page(
        env.owner, aid, page_key="页B", expected_updated_at=await _token(env, aid))
    links = await env.wiki_svc.list_links(
        user=env.owner, artifact_id=aid, page_key="页A", draft=True)
    assert links == [{"from_page": "页A", "to_page": "页B",
                      "anchor_text": "", "status": "broken"}]

    # save_toc 编辑（把死键 页B 剔出目录——页键存在性写入面）
    row = await env.wiki_svc.save_toc(
        user=env.owner, artifact_id=aid,
        toc={"sections": [{"section_title": "云核词条", "page_keys": ["页A"]}]},
        expected_updated_at=toc_row["updated_at"])
    assert row["toc"]["sections"] == [
        {"section_title": "云核词条", "page_keys": ["页A"]}]

    # 页级 diff 内容断言：changed 带 changes（old/new + body hunks）
    diff = await env.art_svc.diff_draft_vs_current(
        user=env.owner, artifact_id=aid)
    assert diff["added_count"] == 1 and diff["removed_count"] == 0
    assert diff["changed_count"] == 0                        # rev0 vs 空 current
    # 改动需有 current 才可见——发布 r1 后再改一页走第二段 diff（真链路）
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="r1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1 and result["row_count"] == 1
    rev1 = await env.adb.get_revision(aid, 1)
    frozen = rev1["schema_json"]["toc"]
    # 定版断言：节标题 + 页键 + 无死键（covered == 全部页面键）
    assert frozen["sections"] == [
        {"section_title": "云核词条", "page_keys": ["页A"]}]
    all_keys = [p["page_key"] for p in rev1["schema_json"]["pages"]]
    covered = [k for s in frozen["sections"] for k in s["page_keys"]]
    assert covered == all_keys and len(set(covered)) == len(covered)

    # 人工再改页 → 页级 diff 内容（body hunks + 字段 old/new）
    new_body = "> 人工摘要二。\n\n## 核心事实\n- 二次修订事实 [[src:u0]]"
    await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A", body_md=new_body,
        expected_updated_at=await _token(env, aid))
    diff = await env.art_svc.diff_draft_vs_current(
        user=env.owner, artifact_id=aid)
    assert diff["changed_count"] == 1
    change = diff["changed"][0]
    assert change["page_key"] == "页A"
    body = change["changes"]["body_md"]
    assert body["new_preview"] == new_body
    assert any(h.startswith("@@ -1,") for h in body["diff_hunks"])
    assert any(h.startswith("-") and "人工摘要。" in h for h in body["diff_hunks"])
    assert any(h.startswith("+") and "二次修订" in h for h in body["diff_hunks"])

    # 运行态 toc 读取 = 快照（viewer 视角；与 rev1 定版逐字段一致）
    runtime_toc = await env.wiki_svc.toc(user=env.viewer, artifact_id=aid)
    assert runtime_toc["sections"] == frozen["sections"]
    assert runtime_toc["revision"] == 1
    # 历史读同源
    hist = await env.wiki_svc.toc(
        user=env.owner, artifact_id=aid, revision=1)
    assert hist["sections"] == frozen["sections"]

    # 搜索转发桩：artifact_ref 口径（REF_PREFIX+DB id）+ domain/username 透传
    forward = _StubForward()
    out = await env.wiki_svc.search(
        user=env.viewer, artifact_id=aid, q="云核", forward=forward)
    assert out["results"][0]["page_key"] == "页A"
    assert forward.calls == [{
        "artifact_ref": REF_PREFIX + aid, "q": "云核", "mode": "fusion",
        "top_k": 10, "domain": DOMAIN, "username": env.viewer["username"],
    }]


# --------------------------------------------------- 闭环3：权限

async def test_loop_permission_matrix_and_key_open(env):
    """权限闭环：owner/editor/viewer×(toc draft/pages draft/publish/rollback)
    路由级；editor 不可改可见性、owner 可；public+钥匙开放子集（子集不放大：
    跨域静默剔除）；停用→恢复；artifact_ref 进 web 响应。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    await env.art_svc.upsert_member(
        user=env.owner, artifact_id=aid,
        member_user_id=env.editor["id"], role="editor")
    await env.art_svc.upsert_member(
        user=env.owner, artifact_id=aid,
        member_user_id=env.viewer["id"], role="viewer")
    # 钥匙主人同域绑定但非制品成员——可见面=public 后才可开放
    token = await _token(env, aid)
    await env.art_svc.publish(
        user=env.editor, artifact_id=aid, change_summary="editor 可发布",
        expected_updated_at=token)
    assert (await env.adb.get_artifact(aid))["current_revision"] == 1

    # viewer：运行态可读、设计态全拒（toc/pages draft → 403）
    env.holder["user"] = env.viewer
    r = await env.client.get(f"/api/artifacts/{aid}/wiki/toc")
    assert r.status_code == 200 and r.json()["revision"] == 1
    r = await env.client.get(f"/api/artifacts/{aid}/wiki/toc?draft=1")
    assert r.status_code == 403
    r = await env.client.get(f"/api/artifacts/{aid}/wiki/pages?draft=1")
    assert r.status_code == 403
    r = await env.client.get(f"/api/artifacts/{aid}/wiki/links?draft=1")
    assert r.status_code == 403
    # viewer 回滚/发布 → 403（服务级）
    with pytest.raises(ArtifactForbidden):
        await env.art_svc.rollback_to(
            user=env.viewer, artifact_id=aid, revision=1,
            expected_updated_at=await _token(env, aid))

    # editor：可发布（上文已证）但不可改可见性；owner 可
    with pytest.raises(ArtifactForbidden):
        await env.art_svc.update_meta(
            user=env.editor, artifact_id=aid, fields={"visibility": "public"})
    art = await env.art_svc.update_meta(
        user=env.owner, artifact_id=aid, fields={"visibility": "public"})
    assert art["visibility"] == "public"

    # artifact_ref 进 web 响应（list/get 共用 _with_ref）
    env.holder["user"] = env.key_owner           # 非成员走 public 可见面
    r = await env.client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 200
    assert r.json()["artifact_ref"] == REF_PREFIX + aid

    # 钥匙开放子集：本域 public 制品入选；跨域制品静默剔除（子集不放大）
    foreign_art = await env.art_svc.create_artifact(
        user=env.foreign, domain_id=OTHER_DOMAIN, kind="table",
        title="跨域表")
    key = await env.key_svc.create_key(
        user_id=env.key_owner["id"], name="验收钥匙", domain=DOMAIN,
        is_admin=False)
    final = await env.key_svc.replace_open_artifacts(
        user_id=env.key_owner["id"], key_id=key["id"],
        artifact_ids=[aid, foreign_art["artifact_id"]], artifact_db=env.adb)
    assert final == [aid]                        # 跨域被 SQL 域防线剔除
    assert await env.kbdb.key_open_artifact_ids(key_id=key["id"]) == [aid]

    # 停用→恢复（服务级；serving 侧 deprecated 滤除由 Java IT 覆盖）
    art = await env.art_svc.deprecate(user=env.owner, artifact_id=aid)
    assert art["status"] == "deprecated"
    art = await env.art_svc.republish(user=env.owner, artifact_id=aid)
    assert art["status"] == "published"


# --------------------------------------------------- 闭环3b：域撤销（66号 §11）

async def test_loop_domain_revocation(env):
    """域撤销：editor 成员被移出域后——制品读/写全 404（反探测），直调服务
    不能绕过；钥匙开放子集随撤域静默收缩（readable_artifact_domain → None）。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="r1",
        expected_updated_at=await _token(env, aid))
    await env.art_svc.upsert_member(
        user=env.owner, artifact_id=aid,
        member_user_id=env.editor["id"], role="editor")
    await env.art_svc.update_meta(
        user=env.owner, artifact_id=aid, fields={"visibility": "public"})

    # 撤域前：成员读/写正常
    page = await env.wiki_svc.get_page(
        user=env.editor, artifact_id=aid, page_key="页A")
    assert page["title"] == "页A"

    # editor 补绑第二域（unbind 拒绝移出 active 用户的最后一个域）
    await env.kbdb.bind_domain(user_id=env.editor["id"], domain=OTHER_DOMAIN)
    # 撤域（移出 user_domains）——真库操作，66号 §11
    await env.kbdb.unbind_domain(user_id=env.editor["id"], domain=DOMAIN)

    # 撤域后直调服务全拒（反探测 ArtifactNotFound；不能绕过）
    with pytest.raises(ArtifactNotFound):
        await env.art_svc.get_artifact(user=env.editor, artifact_id=aid)
    with pytest.raises(ArtifactNotFound):
        await env.art_svc.publish(
            user=env.editor, artifact_id=aid, change_summary="绕过",
            expected_updated_at=await _token(env, aid))
    with pytest.raises(ArtifactNotFound):
        await env.wiki_svc.get_page(
            user=env.editor, artifact_id=aid, page_key="页A")
    with pytest.raises(ArtifactNotFound):
        await env.wiki_svc.save_toc(
            user=env.editor, artifact_id=aid,
            toc={"sections": [{"section_title": "x", "page_keys": ["页A"]}]})
    # 路由面同口径：GET 详情 404（反探测，不区分成员/撤域）
    env.holder["user"] = env.editor
    r = await env.client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 404
    # 撤域=读可见面清零（钥匙开放子集的候选判定谓词）
    assert await env.adb.readable_artifact_domain(
        aid, env.editor["id"]) is None

    # 钥匙开放子集随撤域收缩：key_owner 先开放成功（域内+public 可读），
    # 撤域事务内钥匙被级联吊销（同事务 revoke——Agent 侧读面直接关闭，
    # 比静默收缩更强的 66号 §11 兜底）；开放行残留但钥匙已 revoked。
    await env.kbdb.bind_domain(
        user_id=env.key_owner["id"], domain=OTHER_DOMAIN)
    key = await env.key_svc.create_key(
        user_id=env.key_owner["id"], name="撤域钥匙", domain=DOMAIN,
        is_admin=False)
    final = await env.key_svc.replace_open_artifacts(
        user_id=env.key_owner["id"], key_id=key["id"],
        artifact_ids=[aid], artifact_db=env.adb)
    assert final == [aid]
    assert await env.kbdb.key_open_artifact_ids(key_id=key["id"]) == [aid]
    await env.kbdb.unbind_domain(user_id=env.key_owner["id"], domain=DOMAIN)
    keys = await env.key_svc.list_keys(
        user_id=env.key_owner["id"], is_admin=False)
    row = next(k for k in keys if k["id"] == key["id"])
    assert row["status"] == "revoked"
    assert await env.kbdb.key_open_artifact_ids(key_id=key["id"]) == [aid]
    # 已吊销钥匙不可再改开放集（_require_owned_active）
    with pytest.raises(McpKeyError):
        await env.key_svc.replace_open_artifacts(
            user_id=env.key_owner["id"], key_id=key["id"],
            artifact_ids=[], artifact_db=env.adb)


# --------------------------------------------------- 闭环4：保存语义

async def test_loop_save_semantics(env):
    """保存闭环：toc PUT 乐观锁（stale→409 / 行存在缺令牌→409，路由级）；
    元信息 PATCH 即时生效；discard 只弃页草稿、不动 toc 行。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    toc_row = await _seed_compile_toc(
        env, aid, [{"section_title": "云核词条",
                    "page_hints": [{"title": "页A"}]}])

    # 行存在缺令牌 → 409（M-1 铁律：不静默 last-write-wins）
    env.holder["user"] = env.owner
    r = await env.client.put(
        f"/api/artifacts/{aid}/wiki/toc",
        json={"sections": [{"section_title": "新节", "page_keys": ["页A"]}]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # stale 令牌 → 409（先成功写一次推进令牌，旧令牌再写）
    r = await env.client.put(
        f"/api/artifacts/{aid}/wiki/toc",
        json={"sections": [{"section_title": "第二节", "page_keys": ["页A"]}],
              "expected_updated_at": toc_row["updated_at"].isoformat()})
    assert r.status_code == 200
    r = await env.client.put(
        f"/api/artifacts/{aid}/wiki/toc",
        json={"sections": [{"section_title": "旧令牌", "page_keys": ["页A"]}],
              "expected_updated_at": toc_row["updated_at"].isoformat()})
    assert r.status_code == 409

    # 元信息 PATCH 即时生效（usage_note/description 无需发布）
    r = await env.client.patch(
        f"/api/artifacts/{aid}",
        json={"description": "统一界面验收制品"})
    assert r.status_code == 200
    assert (await env.adb.get_artifact(aid))["description"] == "统一界面验收制品"

    # discard：页草稿清空，toc 行不动（设计决策：discard 只弃页草稿）
    await env.art_svc.discard_draft(
        user=env.owner, artifact_id=aid,
        expected_updated_at=await _token(env, aid))
    assert await env.wdb.count_pages(aid, 0) == 0
    row = await env.wdb.get_draft_toc(aid)
    assert row is not None and row["toc"]["sections"][0]["section_title"] == "第二节"


# --------------------------------------------------- 闭环5：异常

async def test_loop_concurrent_publish_conflict(env):
    """并发发布冲突：两编辑各持旧令牌，后写者发布 → 409 stale（乐观锁全
    精度比对），版本不落。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    stale = await _token(env, aid)
    # 并发面：另一写者抢先写页推进制品令牌（乐观锁对象=制品行 updated_at）
    await env.wiki_svc.upsert_page(
        env.owner, aid, page_key="页A", title="页A",
        body_md=_body("页A", ("u0",)), expected_updated_at=stale)
    with pytest.raises(StaleArtifact):
        await env.art_svc.publish(
            user=env.owner, artifact_id=aid, change_summary="stale",
            expected_updated_at=stale)
    assert (await env.adb.get_artifact(aid))["current_revision"] == 0
    assert await env.adb.get_revision(aid, 1) is None   # 版本未落


async def test_loop_source_without_permission(env):
    """来源无权：绑定他人私有库 → 404 反探测（不暴露库存在性）。"""
    aid = await _make_wiki(env)
    other_kb = await env.kbdb.create_kb(
        domain=DOMAIN, name="他人私有库", owner_id=env.editor["id"])
    with pytest.raises(ArtifactNotFound):
        await env.wiki_svc.upsert_source(
            user=env.owner, artifact_id=aid, kb_id=other_kb["id"],
            directories=[])


async def test_loop_compile_failed_state(env):
    """编译失败态可辨识：批次 details state=failed + error → status 端点
    原样透传（前端可辨识并重试）。"""
    aid = await _make_wiki(env)
    batch = await env.adb.create_batch(
        aid, channel="wiki_compile", file_name="", sheet_name="",
        row_count=0, created_by=env.owner["id"])
    ok = await env.inputs.update_batch_details(
        batch["batch_id"],
        {"state": "failed", "error": "writing_failed:ConnectionError",
         "progress": {"batch": "2/5", "pages_done": 2}})
    assert ok is True
    status = await env.compile_svc.status(user=env.owner, artifact_id=aid)
    assert status["state"] == "failed"
    assert "writing_failed" in str(status["error"])
    assert status["progress"]["batch"] == "2/5"


async def test_loop_projection_failure_reproject_and_status(env):
    """投影 failed→reproject 幂等重建 + vector 未就绪 status 口径：发布→
    pending 计数（未就绪可辨识）→ 直写一向量/标记一 failed → status 三计数
    → reproject 重建（failed 落回 pending）→ 健康在飞态 no-op。"""
    aid = await _make_wiki(env)
    await env.wdb.ensure_draft(aid)
    await _machine_page(env, aid, page_key="页A", title="页A", ordinal=1001)
    await _machine_page(env, aid, page_key="页B", title="页B", ordinal=1002)
    result = await env.art_svc.publish(
        user=env.owner, artifact_id=aid, change_summary="r1",
        expected_updated_at=await _token(env, aid))
    assert result["revision"] == 1
    await _drain_embeds()

    # vector 未就绪口径：status = {total:2, pending:2, done:0, failed:0}
    status = await env.wiki_svc.status(user=env.owner, artifact_id=aid)
    assert status == {"revision": 1, "units": {
        "total": 2, "done": 0, "failed": 0, "pending": 2}}

    # 一路 done、一路终态 failed → 三计数可辨识
    await env.wdb.update_unit_embeddings(aid, 1, ["页A"], [[0.5] * 1024])
    await env.wdb.mark_units_failed(aid, 1, ["页B"])
    status = await env.wiki_svc.status(user=env.owner, artifact_id=aid)
    assert status["units"] == {"total": 2, "done": 1, "failed": 1, "pending": 0}

    # reproject（editor+ 可触发——权限面引用 test_wiki_routes；此处真库重建链）：
    # failed>0 → 重建（delete+insert 落回 pending）
    out = await env.wiki_svc.reproject(user=env.owner, artifact_id=aid)
    assert out == {"artifact_id": aid, "revision": 1, "reprojected": True}
    await _drain_embeds()
    assert await env.wdb.units_status(aid, 1) == {
        "total": 2, "done": 0, "failed": 0, "pending": 2}
    art = await env.adb.get_artifact(aid)
    assert art["current_revision"] == 1          # 不产伪发布版本

    # 健康在飞态（pending>0 无 failed）→ no-op（不把 pending 重置打断重嵌）
    out = await env.wiki_svc.reproject(user=env.owner, artifact_id=aid)
    assert out["reprojected"] is False
