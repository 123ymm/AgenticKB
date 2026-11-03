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
    svc = WikiService(art_svc, wdb, lifecycle)

    app = FastAPI()
    app.include_router(wiki_router)
    holder = {"user": _user("u-alice")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_wiki_service] = lambda: svc
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            adb=adb, wdb=wdb, tc=tc, svc=svc, art_svc=art_svc,
            app=app, client=client, holder=holder,
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
    assert fake.calls == [
        {"artifact_ref": WID, "q": "核心网", "mode": "dense", "top_k": 5,
         "domain": DOMAIN, "username": "u-viewer"},
        {"artifact_ref": WID, "q": "核心网", "mode": "fusion", "top_k": 10,
         "domain": DOMAIN, "username": "u-viewer"},
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
    assert "revision_not_found" in r.text

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
