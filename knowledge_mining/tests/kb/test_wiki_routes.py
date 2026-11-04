"""Wiki 制品 REST 路由 —— httpx ASGI + 真实 WikiService/生命周期 + fake 三件套
（不碰真库）。六端点全打：权限矩阵（viewer 写 403 / draft 不可见 / 404 反探测）/
hex path 中文往返 / 422 定位断言 / 409 锁令牌 / links 明文 query / status。
批次4 Task 4 追加：search 检索转发（FakeForward 桩——viewer 透传/非成员
404 即拦/空白 422/serving 故障族 502/404 透传）。
66号批次1 Task 1.4 追加：GET/PUT /wiki/toc（runtime 快照读+rev0 反探测 /
draft 行读权限矩阵 / revision 历史读+null 回落 / PUT 行级乐观锁 M-1 铁律+
页键存在性 M-4 / 发布 toc warnings 端到端 M-6）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import WikiError, encode_page_key
from knowledge_mining.mining.kb.deps import get_wiki_search_client, get_wiki_service
from knowledge_mining.mining.kb.routes.wiki import router as wiki_router
from knowledge_mining.mining.kb.services.artifact_service import ArtifactService
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
    _drain_background,
)
from knowledge_mining.tests.kb.test_wiki_sources import FakeCompileInputsDB

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
WID = "art_wiki_route"
NOW = datetime(2026, 10, 22, 9, 0, 0, 500000, tzinfo=timezone.utc)


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
    # Q01（66号批次5 Task 5.2）：注入 fake 取材域——unit-source 端点数据源
    # （现有用例不触达 sources/unit-source，注入零影响）。kb-1/kb-2 预置
    # 可读（与 sources 夹具同款）；其余 kb_id 默认不可读（kb 门负例）。
    inputs = FakeCompileInputsDB(adb.artifacts)
    inputs.readable = {"kb-1", "kb-2"}
    svc = WikiService(art_svc, wdb, lifecycle, inputs_db=inputs)

    app = FastAPI()
    app.include_router(wiki_router)
    holder = {"user": _user("u-alice")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_wiki_service] = lambda: svc
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            adb=adb, wdb=wdb, tc=tc, svc=svc, art_svc=art_svc,
            inputs=inputs, app=app, client=client, holder=holder,
        )


def _hex(key: str) -> str:
    return encode_page_key(key)


async def _publish(env, summary: str = "rev1") -> dict:
    result = await env.art_svc.publish(
        user=_user("u-alice"), artifact_id=WID, change_summary=summary)
    await _drain_background()
    return result


async def _token(env) -> str:
    return env.adb.artifacts[WID]["updated_at"].isoformat()


# ------------------------------------------------------------ 页面 CRUD

async def test_page_crud_roundtrip_hex_chinese(env):
    """PUT 新建（page_key 缺省后端归一）→ GET hex 往返中文键 → PUT 更新 →
    DELETE → 404。"""
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('占位')}",
        json={"title": "核心网 术语", "body_md": "正文 v1"})
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["page_key"] == "核心网-术语"
    assert page["origin"] == "manual" and page["ordinal"] == 1

    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/page/{_hex('核心网-术语')}",
        params={"draft": "true"})   # 未发布：单页读走 design 视图
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "核心网 术语"
    assert r.json()["body_md"] == "正文 v1"
    assert r.json()["frontmatter"] == {}

    # 更新：显式 page_key 稳定寻址（title 改名不漂移）
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('核心网-术语')}",
        json={"title": "核心网术语表", "page_key": "核心网-术语",
              "body_md": "正文 v2"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "核心网术语表"
    assert r.json()["page_key"] == "核心网-术语"

    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('核心网-术语')}",
        json={"expected_updated_at": await _token(env)})
    assert r.status_code == 200 and r.json() == {"deleted": True}
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/page/{_hex('核心网-术语')}")
    assert r.status_code == 404   # runtime 视图：删的是 rev0 页（未发布）
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/page/{_hex('核心网-术语')}",
        params={"draft": "true"})
    assert r.status_code == 404
    assert "page_not_found" in r.text


async def test_put_rename_to_occupied_conflict_409(env):
    """改名流：body.page_key 指向已被其他页占用的键 → 409 page_key_conflict。"""
    for title in ("旧页", "占用页"):
        r = await env.client.put(
            f"/api/artifacts/{WID}/wiki/page/{_hex(title)}",
            json={"title": title})
        assert r.status_code == 200, r.text
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('旧页')}",
        json={"title": "新名", "page_key": "占用页"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "page_key_conflict"


# -------------------------------------------------------------- 权限矩阵

async def test_viewer_write_and_draft_forbidden(env):
    """viewer：PUT/DELETE 403；draft=true / 显式 revision 一律 403（rev0 对
    runtime 不可见）；runtime 读与 status 放行。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": "v1"})
    env.holder["user"] = _user("u-viewer")

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('x')}",
        json={"title": "x"})
    assert r.status_code == 403
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}")
    assert r.status_code == 403

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages")
    assert r.status_code == 200
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages", params={"draft": "true"})
    assert r.status_code == 403
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}", params={"draft": "true"})
    assert r.status_code == 403
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/pages", params={"revision": 1})
    assert r.status_code == 403
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/links", params={"draft": "true"})
    assert r.status_code == 403
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/status")
    assert r.status_code == 200


async def test_runtime_cannot_see_rev0(env):
    """未发布运行态=空集（rev0 设计态对 runtime 不可见）；发布后 runtime 见
    current、draft 编辑页不漏。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": "v1"})
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages")
    assert r.status_code == 200 and r.json()["pages"] == []
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}")
    assert r.status_code == 404   # 反探测口径：运行态查不到即 404

    env.holder["user"] = _user("u-alice")
    await _publish(env)
    # 设计态加草稿页 → runtime 不漏
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('草稿')}",
        json={"title": "草稿页"})
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages")
    assert [p["page_key"] for p in r.json()["pages"]] == ["首页"]
    env.holder["user"] = _user("u-bob")
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/pages", params={"draft": "true"})
    assert {p["page_key"] for p in r.json()["pages"]} == {"首页", "草稿页"}


async def test_404_anti_probe_non_member(env):
    """非成员（含同域无绑定）六端点一律 404——不暴露制品存在性。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页"})
    env.holder["user"] = _user("u-stranger")
    hex_key = _hex("首页")
    for method, url in (
        ("GET", f"/api/artifacts/{WID}/wiki/pages"),
        ("GET", f"/api/artifacts/{WID}/wiki/page/{hex_key}"),
        ("PUT", f"/api/artifacts/{WID}/wiki/page/{hex_key}"),
        ("DELETE", f"/api/artifacts/{WID}/wiki/page/{hex_key}"),
        ("GET", f"/api/artifacts/{WID}/wiki/links"),
        ("GET", f"/api/artifacts/{WID}/wiki/status"),
    ):
        r = await env.client.request(method, url,
                                     json={"title": "x"} if method == "PUT" else None)
        assert r.status_code == 404, (method, r.status_code)


# -------------------------------------------------------------- 校验/锁

async def test_put_validation_422_loc(env):
    """422 定位断言：缺 title / 非法 page_type → loc=["body",字段]；非法 hex
    path → 422 invalid_page_key；专页缺必填键 → 422 invalid_frontmatter:字段。"""
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('x')}", json={"body_md": "无标题"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "title"]

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('x')}",
        json={"title": "t", "page_type": "dashboard"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "page_type"]

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/zz-not-hex",
        json={"title": "t"})
    assert r.status_code == 422 and "invalid_page_key" in r.text

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('矛盾')}",
        json={"title": "矛盾页", "page_type": "contradiction",
              "frontmatter": {"claim_b": "B", "severity": "low"}})
    assert r.status_code == 422
    assert "invalid_frontmatter:claim_a" in r.text

    # glossary 缺 term 同口径
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('词条')}",
        json={"title": "词条页", "page_type": "glossary", "frontmatter": {}})
    assert r.status_code == 422
    assert "invalid_frontmatter:term" in r.text


async def test_stale_token_maps_409(env):
    """PUT/DELETE 带旧锁令牌 → 409 stale_artifact（信封含 code）；DELETE 令牌
    随 body。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页"})
    stale = "2000-01-01T00:00:00Z"
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页2", "expected_updated_at": stale})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"expected_updated_at": stale})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # 当前令牌 → 放行（锁令牌随 body）
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"expected_updated_at": await _token(env)})
    assert r.status_code == 200


# ------------------------------------------------------- links / status

async def test_links_endpoint_direction_and_draft(env):
    """links：明文 query page_key / direction 双向 / draft 与 current 分叉。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('A页')}",
        json={"title": "A页", "body_md": "参见 [[B页]]"})
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('B页')}", json={"title": "B页"})
    await _publish(env)

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/links")
    assert r.status_code == 200
    assert [(e["from_page"], e["to_page"], e["status"])
            for e in r.json()["links"]] == [("A页", "B页", "valid")]

    # 反向链接：明文中文 query（不经网关 path 校验，无需 hex）
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/links",
        params={"page_key": "B页", "direction": "in"})
    assert r.status_code == 200
    assert [e["from_page"] for e in r.json()["links"]] == ["A页"]

    # 草稿删 B → draft 边 broken；runtime（current）不动
    deleted = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('B页')}",
        json={"expected_updated_at": await _token(env)})
    assert deleted.status_code == 200, deleted.text
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/links", params={"draft": "true"})
    assert [(e["from_page"], e["to_page"], e["status"])
            for e in r.json()["links"]] == [("A页", "B页", "broken")]
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/links")
    assert [(e["to_page"], e["status"]) for e in r.json()["links"]] \
        == [("B页", "valid")]

    # viewer draft → 403
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/links", params={"draft": "true"})
    assert r.status_code == 403


async def test_pages_links_revision_envelope(env):
    """D03.1：pages/links 响应信封回填 revision（图谱混版判定契约——66号：
    前端 pages/links 两请求间发生发布须可检出）：runtime=current（未发布
    0）；draft=设计态投影（draft_open 时 rev0，干净态投影 current）；显式
    revision=N 原样回填。"""
    # 未发布 runtime：revision=0 + 空集（rev0 对 runtime 不可见）
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages")
    assert r.status_code == 200
    assert r.json() == {"revision": 0, "pages": []}
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/links")
    assert r.json() == {"revision": 0, "links": []}

    # 建页 + 发布 → runtime 两端点 revision=1（同版可判定）
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('A页')}",
        json={"title": "A页", "body_md": "参见 [[B页]]"})
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('B页')}", json={"title": "B页"})
    await _publish(env)
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/pages")
    assert r.json()["revision"] == 1
    assert len(r.json()["pages"]) == 2
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/links")
    assert r.json()["revision"] == 1
    assert len(r.json()["links"]) == 1

    # draft 视图（editor）：干净态投影 current=1；检出草稿后 rev0
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/pages", params={"draft": "true"})
    assert r.status_code == 200 and r.json()["revision"] == 1
    await env.client.put(   # 页写检出草稿（draft_open=True）
        f"/api/artifacts/{WID}/wiki/page/{_hex('草稿')}",
        json={"title": "草稿页"})
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/pages", params={"draft": "true"})
    assert r.json()["revision"] == 0
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/links", params={"draft": "true"})
    assert r.json()["revision"] == 0

    # 显式历史 revision 原样回填（design 角色）——仅 pages 端点：links 路由
    # 不收 revision 查询参数（FastAPI 静默丢弃后走 runtime，恰与 1 同值是
    # 巧合而非契约，审查 #4 勘正删除——links 信封 revision 只随 draft/缺省
    # 视图口径，见路由 docstring）。
    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/pages", params={"revision": 1})
    assert r.status_code == 200 and r.json()["revision"] == 1


async def test_status_endpoint(env):
    """status：未发布 revision=0/units 全 0；发布后 units 进度（发布后置
    embedding 完成后 done=total）。"""
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/status")
    assert r.status_code == 200
    assert r.json() == {"revision": 0,
                        "units": {"total": 0, "done": 0, "failed": 0,
                                  "pending": 0}}

    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": "正文"})
    await _publish(env)
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/status")
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 1
    assert body["units"]["total"] == 1 and body["units"]["done"] == 1


# ------------------------------------------------------------ 检索转发（批次4 Task 4）

class FakeForward:
    """转发客户端桩：记录调用参数（domain/username 口径断言）；可注入预设
    响应或 WikiError（serving 故障族翻译链路用）。"""

    def __init__(self, response: dict | None = None,
                 error: WikiError | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    async def search(self, *, artifact_ref: str, q: str, mode: str,
                     top_k: int, domain: str, username: str,
                     timeout: float = 15.0) -> dict:
        self.calls.append({
            "artifact_ref": artifact_ref, "q": q, "mode": mode,
            "top_k": top_k, "domain": domain, "username": username,
        })
        if self._error is not None:
            raise self._error
        return self._response


def _search_payload() -> dict:
    """serving wiki_search 视图样例（snake_case 契约）。"""
    return {
        "view": "wiki_search", "artifact_ref": WID, "revision": 1,
        "degraded_dense": False,
        "results": [
            {"page_key": "核心网-术语", "title": "核心网 术语", "unit_key": "u1",
             "snippet": "MOP 是移动对外门户……", "score": 0.9,
             "channels": ["wiki_dense", "wiki_keyword"]},
        ],
    }


async def test_search_viewer_passthrough_and_forward_body(env):
    """viewer（runtime read）可搜：转发 body 的 domain=制品行 domain_id /
    username=会话用户名（is_admin/key_id 由真实客户端定死，路由测试桩不含）；
    serving 响应原样透传。"""
    fake = FakeForward(response=_search_payload())
    env.app.dependency_overrides[get_wiki_search_client] = lambda: fake
    env.holder["user"] = _user("u-viewer")

    r = await env.client.get(
        f"/api/artifacts/{WID}/wiki/search",
        params={"q": "核心网", "mode": "dense", "top_k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["view"] == "wiki_search"
    assert body["results"][0]["page_key"] == "核心网-术语"
    # 默认参数：mode/top_k 缺省 fusion/10
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "核心网"})
    assert r.status_code == 200
    # 契约钉死：转发 artifact_ref = "art_" + DB id（真形态 art_art_<hex>）——
    # serving ArtifactWikiSearchService.parseRef 单剥一个前缀还原 DB id；
    # 直传 DB id（自带 art_）会被剥成裸 hex → artifact_not_found 404
    # （Task 1.6 实施中协议核实的存量缺陷，防再犯）。
    assert fake.calls == [
        {"artifact_ref": f"art_{WID}", "q": "核心网", "mode": "dense",
         "top_k": 5, "domain": DOMAIN, "username": "u-viewer"},
        {"artifact_ref": f"art_{WID}", "q": "核心网", "mode": "fusion",
         "top_k": 10, "domain": DOMAIN, "username": "u-viewer"},
    ]


async def test_search_non_member_404_no_forward(env):
    """非成员（未绑域/无成员行）→ mining 侧 404 反探测即拦，不发起转发
    （存活性不外泄给 serving，也省一次往返）。"""
    fake = FakeForward(response=_search_payload())
    env.app.dependency_overrides[get_wiki_search_client] = lambda: fake
    env.holder["user"] = _user("u-outside")

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "核心网"})
    assert r.status_code == 404
    assert fake.calls == []


async def test_search_blank_or_invalid_params_422(env):
    """q 空串（FastAPI min_length 拦）/ 纯空白（服务层与 serving isBlank 同
    口径拦）/ mode 非法（Literal 拦）→ 422 且不发起转发。"""
    fake = FakeForward(response=_search_payload())
    env.app.dependency_overrides[get_wiki_search_client] = lambda: fake
    env.holder["user"] = _user("u-viewer")

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": ""})
    assert r.status_code == 422
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "   "})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_query"
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "核心网", "mode": "vector"})
    assert r.status_code == 422
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "核心网", "top_k": 999})
    assert r.status_code == 422
    assert fake.calls == []


async def test_search_serving_failure_family_502(env):
    """serving 故障族（未配密钥/网络不可达/超时/内部错）→ 502 中文信封。"""
    env.holder["user"] = _user("u-viewer")
    for code in ("serving_unavailable", "serving_timeout", "serving_error"):
        fake = FakeForward(error=WikiError(code))
        env.app.dependency_overrides[get_wiki_search_client] = \
            lambda f=fake: f   # default 参绑当前桩（防循环晚绑定）
        r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                                 params={"q": "核心网"})
        assert r.status_code == 502, (code, r.text)
        assert r.json()["detail"]["code"] == code
        assert r.json()["detail"]["message"]   # 中文文案非空


async def test_search_serving_404_passthrough(env):
    """serving 侧 404（如 rev0 未发布对 serving 不可见）→ 反探测口径透传
    404（不升 502——与 mining 侧非成员 404 同响应面）。"""
    fake = FakeForward(error=WikiError("artifact_not_found"))
    env.app.dependency_overrides[get_wiki_search_client] = lambda: fake
    env.holder["user"] = _user("u-viewer")

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/search",
                             params={"q": "核心网"})
    assert r.status_code == 404


# ------------------------------------------------------------ 目录（66号批次1 Task 1.4）
# GET /wiki/toc 缺省（runtime）读 current 快照；?draft=true 读 toc 行本身
# （设计态编辑对象）；?revision=N 读历史快照（editor+）；PUT 整体替换草稿行。
# 乐观锁对象是 **toc 行自身** updated_at（页 PUT 比对 kb_artifacts.updated_at
# 的行级变体）：行已存在时 expected_updated_at 必传（缺 → 409 stale_artifact，
# M-1 审查铁律——不静默 last-write-wins），首写免令牌（编译钩子/首次编辑）。

async def test_toc_runtime_read_and_rev0_anti_probe(env):
    """组1：runtime（viewer 可读）返回发布冻结的快照 sections；rev0 未发布
    → 404 反探测（口径同单页读——运行态查不到即 404）。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('页A')}",
        json={"title": "页A", "body_md": "v1"})
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc")
    assert r.status_code == 404   # 未发布：运行态无目录 → 404
    assert "toc_not_found" in r.text

    env.holder["user"] = _user("u-bob")
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "基础", "page_keys": ["页A"]}]})
    assert r.status_code == 200, r.text
    env.holder["user"] = _user("u-alice")
    await _publish(env)

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sections"] == [
        {"section_title": "基础", "page_keys": ["页A"]}]
    assert body["flat"] is False and body["revision"] == 1


async def test_toc_draft_view_permissions_and_empty_fallback(env):
    """组2：draft 视图 viewer 403（rev0 对 runtime 不可见）；editor 无 toc 行
    → {"sections":[],"flat":true,+pages_total}；行存在 → 读行本身（返回
    updated_at=下次 PUT 的乐观锁令牌）。"""
    for title in ("页A", "页B"):
        await env.client.put(
            f"/api/artifacts/{WID}/wiki/page/{_hex(title)}",
            json={"title": title})

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"draft": "true"})
    assert r.status_code == 403

    env.holder["user"] = _user("u-bob")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"draft": "true"})
    assert r.status_code == 200, r.text
    assert r.json() == {"sections": [], "flat": True, "pages_total": 2}

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1", "page_keys": ["页A"]}]})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["toc"] == {"sections": [
        {"section_title": "S1", "page_keys": ["页A"]}]}
    assert row["updated_by"] == "u-bob" and row["updated_at"]

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"draft": "true"})
    body = r.json()
    assert body["sections"] == [{"section_title": "S1", "page_keys": ["页A"]}]
    assert body["flat"] is False and body["pages_total"] == 2
    assert body["updated_at"] == row["updated_at"]


async def test_toc_revision_history_and_null_fallback(env):
    """组3：revision=N 历史快照读 editor+（viewer 403）；revision 不存在 →
    404 revision_not_found；旧版快照（目录定版前发布）schema_json.toc=null →
    flat 回落（200，不伪装成有结构）。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('页A')}",
        json={"title": "页A", "body_md": "v1"})
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1", "page_keys": ["页A"]}]})
    await _publish(env)   # rev1：快照冻结 toc

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"revision": 1})
    assert r.status_code == 403   # 显式 revision 一律 design 角色

    env.holder["user"] = _user("u-bob")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"revision": 1})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "sections": [{"section_title": "S1", "page_keys": ["页A"]}],
        "flat": False, "revision": 1}

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"revision": 99})
    assert r.status_code == 404
    # M-2：无后缀 404 码走 {code,message} 信封（与 toc_not_found 同面）
    assert r.json()["detail"]["code"] == "revision_not_found"
    assert r.json()["detail"]["message"]

    # M-1：draft+revision 同传 → revision 优先（口径同 pages/_design_revision）
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"draft": "true", "revision": 1})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "sections": [{"section_title": "S1", "page_keys": ["页A"]}],
        "flat": False, "revision": 1}   # revision 视图形态（无 pages_total）

    # 旧版快照回落：目录定版前发布的版本行无 toc → sections=[] flat=true
    env.wdb.revisions[WID][1]["schema_json"]["toc"] = None
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc",
                             params={"revision": 1})
    assert r.status_code == 200, r.text
    assert r.json() == {"sections": [], "flat": True, "revision": 1}


async def test_toc_put_lock_and_validation(env):
    """组4：PUT——首写免令牌 200（返回行含 updated_at 新令牌）；行已存在缺
    令牌 → 409 stale_artifact（M-1 铁律，信封同页 PUT 令牌失配）；stale 令牌
    → 409；畸形 sections → 422（浅层 loc=body.sections / 深层
    invalid_toc_shape 带路径）；页键不存在 → 422 toc_unknown_page_keys:键名
    清单（M-4 消解）；viewer 403 / 非成员 404。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('页A')}",
        json={"title": "页A", "body_md": "v1"})

    env.holder["user"] = _user("u-viewer")
    r = await env.client.put(f"/api/artifacts/{WID}/wiki/toc",
                             json={"sections": []})
    assert r.status_code == 403
    env.holder["user"] = _user("u-stranger")
    r = await env.client.put(f"/api/artifacts/{WID}/wiki/toc",
                             json={"sections": []})
    assert r.status_code == 404   # 反探测

    env.holder["user"] = _user("u-bob")
    # 浅层畸形：sections 非列表 → pydantic loc 定位（先例：title 缺失）
    r = await env.client.put(f"/api/artifacts/{WID}/wiki/toc",
                             json={"sections": "不是列表"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "sections"]
    # 深层畸形：page_keys 非列表 → 422 invalid_toc_shape 带 json path 细节
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1", "page_keys": "页A"}]})
    assert r.status_code == 422
    assert "invalid_toc_shape" in r.text

    # 页键不存在 → 422 带具体键名（校验面即拒，发布 dropped 警示不复现）
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1",
                            "page_keys": ["页A", "幽灵页"]}]})
    assert r.status_code == 422
    assert "toc_unknown_page_keys:幽灵页" in r.text

    # 首写免令牌 → 200；返回的 updated_at 即下次编辑令牌
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1", "page_keys": ["页A"]}]})
    assert r.status_code == 200, r.text
    token = r.json()["updated_at"]

    # 行已存在 + 缺令牌 → 409（不静默覆盖另一 editor 的目录）
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S2", "page_keys": ["页A"]}]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # stale 令牌 → 409 同信封
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S2", "page_keys": ["页A"]}],
              "expected_updated_at": "2000-01-01T00:00:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # 当前令牌 → 200 且新令牌可继续链式编辑（令牌随写前进）
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S2", "page_keys": ["页A"]}],
              "expected_updated_at": token})
    assert r.status_code == 200, r.text
    assert r.json()["toc"]["sections"][0]["section_title"] == "S2"
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S3", "page_keys": ["页A"]}],
              "expected_updated_at": r.json()["updated_at"]})
    assert r.status_code == 200, r.text


async def test_toc_upsert_in_lock_token_required(env):
    """I-1（质量审查）：锁内权威判定——直调 wdb.upsert_draft_toc(
    require_token_if_exists=True) 行存在+缺令牌 → stale_artifact（绕过
    service 快检路径，覆盖 TOCTOU 窗口关闭的锁内分支；PG 并发真首写竞态
    用例留批次6）。默认 False（编译钩子口径）：行存在+缺令牌 → 放行整体
    替换。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('页A')}",
        json={"title": "页A", "body_md": "v1"})
    toc = {"sections": [{"section_title": "S1", "page_keys": ["页A"]}]}
    await env.wdb.upsert_draft_toc(WID, toc, updated_by="u-bob")

    # require_token_if_exists=True：行存在 + 缺令牌 → 锁内 409 口径
    with pytest.raises(WikiError, match="stale_artifact"):
        await env.wdb.upsert_draft_toc(
            WID, toc, updated_by="u-bob", require_token_if_exists=True)

    # 同参数但带当前令牌 → 放行（权威比对分支不受影响）
    row = env.wdb.draft_tocs[WID]
    updated = await env.wdb.upsert_draft_toc(
        WID, toc, updated_by="u-bob",
        expected_updated_at=row["updated_at"],
        require_token_if_exists=True)
    assert updated["updated_at"] != row["updated_at"]   # 令牌随写前进

    # 默认 False（编译 completed 钩子口径）：行存在 + 缺令牌 → 整体替换放行
    replaced = await env.wdb.upsert_draft_toc(
        WID, toc, updated_by="compiler")
    assert replaced["updated_by"] == "compiler"


async def test_publish_toc_warnings_end_to_end(env):
    """Step 4（M-6 遗留端到端）：目录只收录页A（页B 未收录）→ 发布响应
    warnings 真带 toc_appended_page_keys:页B；检出后目录补录页B、删页A（目录
    仍引用页A）→ 再发布 → toc_dropped_page_keys:页A。发布走 art_svc.publish
    → WikiLifecycle 真拼接路径（fake 只镜像 wdb 层 resolve 产物——最贴近
    真实：警告从 db 解析浮到 HTTP 响应面的链路是产品代码）。"""
    for title, body in (("页A", "a"), ("页B", "b")):
        await env.client.put(
            f"/api/artifacts/{WID}/wiki/page/{_hex(title)}",
            json={"title": title, "body_md": body})
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1", "page_keys": ["页A"]}]})

    result = await _publish(env)
    assert any(w.startswith("toc_appended_page_keys:页B")
               for w in result["warnings"]), result["warnings"]

    # runtime 读到解析后的完整目录（未收录页进尾部「其他」节）
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/toc")
    assert r.status_code == 200, r.text
    assert r.json()["sections"] == [
        {"section_title": "S1", "page_keys": ["页A"]},
        {"section_title": "其他", "page_keys": ["页B"]}]

    # 二幕：干净态检出（PUT toc 前置 ensure_draft）→ 补录页B → 删页A → 发布
    env.holder["user"] = _user("u-bob")
    row = env.wdb.draft_tocs[WID]
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/toc",
        json={"sections": [{"section_title": "S1",
                            "page_keys": ["页A", "页B"]}],
              "expected_updated_at": row["updated_at"].isoformat()})
    assert r.status_code == 200, r.text
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/page/{_hex('页A')}",
        json={"expected_updated_at": await _token(env)})
    assert r.status_code == 200, r.text
    env.holder["user"] = _user("u-alice")
    result = await _publish(env)
    assert any(w.startswith("toc_dropped_page_keys:页A")
               for w in result["warnings"]), result["warnings"]


# ---------------------------------------------- Q01 回原文（66号批次5 Task 5.2）
# GET /wiki/unit-source?unit_id=：unit_id（=retrieval units v2 的
# representation_id，即 provenance unit_ids 元素）→ 来源单元原文截读（500
# 字）+ kb_id/snapshot_id/section_ref。双重权限：制品读取（viewer 可读运行
# 态）+ 源 kb 可读二次校验（66号 §2「能读制品不等于能读整个来源知识库」——
# kb_readable=KbDB.is_visible 谓词复用）。404 反探测单码：非本制品 unit /
# 原文退役 / 无源库读权 不区分（unit_not_found）。

def _seed_unit(env, unit_id: str, kb_id: str = "kb-1", revisions: set[int] = {1},
               content_text: str = "原文片段", section_ref: str = "s-0",
               snapshot_id: str = "snap-1") -> None:
    """fake 取材域预置：真身行 + (artifact, unit) 可见 revision 集。"""
    env.inputs.unit_rows[unit_id] = {
        "kb_id": kb_id, "snapshot_id": snapshot_id,
        "content_text": content_text, "section_ref": section_ref}
    env.inputs.unit_provenance[(WID, unit_id)] = set(revisions)


async def _publish_one_page(env, body_md: str = "v1") -> None:
    """建一页并发布（rev1）——publish 门禁要求非空页集。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": body_md})
    await _publish(env)


async def test_unit_source_roundtrip_and_truncation(env):
    """viewer 命中：{unit_id, content_text(截500), kb_id, snapshot_id,
    section_ref}；600 字原文截前 500；空白 unit_id 缺参 → 422。"""
    await _publish_one_page(env)
    _seed_unit(env, "ru-1", content_text="长" * 600)

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "unit_id": "ru-1", "content_text": "长" * 500,
        "kb_id": "kb-1", "snapshot_id": "snap-1", "section_ref": "s-0"}

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source")
    assert r.status_code == 422


async def test_unit_source_provenance_gate_404(env):
    """非本制品 provenance 的 unit（库里存在但未挂账）/ 完全未知 unit →
    404 unit_not_found（不开放任意 unit 反查）。"""
    await _publish_one_page(env)
    _seed_unit(env, "ru-cited", revisions={1})
    _seed_unit(env, "ru-orphan", revisions={99})   # 挂在别的 revision

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-cited"})
    assert r.status_code == 200
    for unit_id in ("ru-orphan", "ru-unknown"):
        r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                                 params={"unit_id": unit_id})
        assert r.status_code == 404, unit_id
        assert r.json()["detail"]["code"] == "unit_not_found"


async def test_unit_source_kb_readability_second_gate(env):
    """源 kb 不可读 → 404 unit_not_found **同码同文案**（不区分暴露 unit
    存在性）；可读恢复后放行（同一 viewer，制品面不变——独立判定）。"""
    await _publish_one_page(env)
    _seed_unit(env, "ru-1", kb_id="kb-secret", revisions={1})

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-1"})
    assert r.status_code == 404
    denied = r.json()["detail"]

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-none"})
    assert r.status_code == 404
    assert r.json()["detail"] == denied   # 同口径：不泄露是权限还是不存在

    env.inputs.readable.add("kb-secret")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-1"})
    assert r.status_code == 200 and r.json()["kb_id"] == "kb-secret"


async def test_unit_source_revision_view_matrix(env):
    """视图 revision 口径对齐单页读：未发布 runtime=空集 404；draft/显式
    revision 一律 design 角色（viewer 403）；design 草稿视图命中 rev0；
    revision=N 精确匹配该版 provenance；非成员 404 反探测。"""
    # 未发布（rev0 草稿页）：design 视图命中，runtime 不见
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": "v1"})
    _seed_unit(env, "ru-2", revisions={0})

    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2"})
    assert r.status_code == 404   # 运行态空集（rev0 对 runtime 不可见）
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2", "draft": "true"})
    assert r.status_code == 403

    env.holder["user"] = _user("u-bob")   # editor：design 面
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2", "draft": "true"})
    assert r.status_code == 200 and r.json()["unit_id"] == "ru-2"

    # 发布后 rev0 清空：draft 视图（干净态投影 current）与 revision=1 命中、
    # 显式 revision=0（已清）不再命中。
    _seed_unit(env, "ru-2", revisions={1})
    await _publish(env)
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2"})
    assert r.status_code == 200
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2", "revision": 1})
    assert r.status_code == 200
    # revision=0 不可请求（ge=1 门）——rev0 经 draft=true 触达
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2", "revision": 0})
    assert r.status_code == 422
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2", "revision": 99})
    assert r.status_code == 404

    env.holder["user"] = _user("u-stranger")
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/unit-source",
                             params={"unit_id": "ru-2"})
    assert r.status_code == 404


# ---------------------------------------------- Q01 投影修复（66号批次5 Task 5.3）
# POST /wiki/reproject：对 current_revision 的 units 投影幂等重入——修复仅重建
# 既定版本的检索投影，不悄悄改正文、不产生伪发布版本（66号 §14）。editor+；
# 健康投影 no-op（reprojected=False，不重置已 done 的 units）；failed>0 /
# 投影缺失（发布后置失败）→ 重入 project_units + embedding 重拉起
# （reprojected=True）。

async def test_reproject_permissions_and_healthy_noop(env):
    """权限矩阵：editor 200 / viewer 403 / 非成员 404；健康投影（发布后置
    完成，全 done）→ no-op 成功 reprojected=False。"""
    await _publish_one_page(env)   # rev1 + drain → units 全 done

    env.holder["user"] = _user("u-viewer")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 403
    env.holder["user"] = _user("u-stranger")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 404   # 反探测

    env.holder["user"] = _user("u-bob")   # editor
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 200, r.text
    assert r.json() == {"artifact_id": WID, "revision": 1,
                        "reprojected": False}
    # 健康态 no-op：已 done 的 units 不被重置
    assert (await env.wdb.units_status(WID, 1))["done"] == 1


async def test_reproject_failed_units_rebuilt_no_fake_revision(env):
    """failed>0 → reprojected=True：投影重置（failed 清零、pending 待重嵌），
    embedding 重拉起自愈；**不产伪版本**——revisions 行数 / current_revision /
    正文一字不动（66号 §14）。"""
    await _publish_one_page(env)
    rows = env.wdb.units[(WID, 1)]
    assert rows   # 发布后置投影应存在（单页 1 unit）
    await env.wdb.mark_units_failed(WID, 1, [r["unit_key"] for r in rows])

    env.holder["user"] = _user("u-bob")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 200, r.text
    assert r.json() == {"artifact_id": WID, "revision": 1,
                        "reprojected": True}

    # 投影重置：failed 清零、全量 pending（待 embedding 自愈）
    status = await env.wdb.units_status(WID, 1)
    assert status["failed"] == 0
    assert status["pending"] == status["total"] == 1

    # 不产伪版本（66号 §14 铁律三面）
    assert set(env.wdb.revisions[WID]) == {1}
    assert env.adb.artifacts[WID]["current_revision"] == 1
    page = await env.wdb.get_page(WID, 1, "首页")
    assert page["title"] == "首页" and page["body_md"] == "v1"

    # 后台 embedding 重拉起 → 自愈闭环（drain 后全 done）
    await _drain_background()
    assert await env.wdb.units_status(WID, 1) == \
        {"total": 1, "done": 1, "failed": 0, "pending": 0}


async def test_reproject_idempotent_second_call_noop(env):
    """幂等（66号 §14）：连调两次——第一次修复（reprojected=True）→ 自愈完成
    → 第二次 no-op 成功（reprojected=False），已 done 的 units 不被重置
    （不产无谓 pending / 重复 embedding）。"""
    await _publish_one_page(env)
    await env.wdb.mark_units_failed(
        WID, 1, [r["unit_key"] for r in env.wdb.units[(WID, 1)]])

    env.holder["user"] = _user("u-bob")
    r1 = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r1.json()["reprojected"] is True
    await _drain_background()   # 重嵌完成 → done

    r2 = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"artifact_id": WID, "revision": 1,
                         "reprojected": False}
    # 第二次未重置投影：units 仍全 done（若重入会落回 pending）
    assert (await env.wdb.units_status(WID, 1))["done"] == 1


async def test_reproject_missing_projection_rebuilt(env):
    """投影缺失（发布后置 project_units 失败只记日志——63号 §5.1 语义）→
    reproject 补建投影（total=0 亦视为待修面）。"""
    await _publish_one_page(env)
    del env.wdb.units[(WID, 1)]   # 模拟投影从未落地
    assert (await env.wdb.units_status(WID, 1))["total"] == 0

    env.holder["user"] = _user("u-bob")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 200, r.text
    assert r.json()["reprojected"] is True
    await _drain_background()
    assert await env.wdb.units_status(WID, 1) == \
        {"total": 1, "done": 1, "failed": 0, "pending": 0}


async def test_reproject_unpublished_404(env):
    """未发布（rev0）→ 404 revision_not_found（无既定版本可修投影）。"""
    await env.client.put(
        f"/api/artifacts/{WID}/wiki/page/{_hex('首页')}",
        json={"title": "首页", "body_md": "v1"})   # 草稿页，不发布
    env.holder["user"] = _user("u-bob")
    r = await env.client.post(f"/api/artifacts/{WID}/wiki/reproject")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "revision_not_found"
