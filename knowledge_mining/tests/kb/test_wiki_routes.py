"""Wiki 制品 REST 路由 —— httpx ASGI + 真实 WikiService/生命周期 + fake 三件套
（不碰真库）。六端点全打：权限矩阵（viewer 写 403 / draft 不可见 / 404 反探测）/
hex path 中文往返 / 422 定位断言 / 409 锁令牌 / links 明文 query / status。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import encode_page_key
from knowledge_mining.mining.kb.deps import get_wiki_service
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
