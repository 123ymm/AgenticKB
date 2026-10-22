"""Wiki 制品来源绑定（63号批次3 Task 1）—— sources CRUD + 脏标记观测（无 PG）。

真实 WikiService/ArtifactService + 批次2 Fake 家族（FakeArtifactDB/FakeWikiDB/
FakeTaskClient）+ 本文件新增 FakeCompileInputsDB（权威乐观锁比对+touch 语义与
真库同构）。覆盖：三端点权限矩阵（viewer PUT/DELETE 403、GET 200；非成员 404
反探测）；kb 不可读 404（防探测绑定他人私有库）；dirty 三态（无指纹 / 一致 /
快照变化）；软删后复活（重绑同库 status 回 active、指纹保留）；绑定写推进
乐观锁令牌（touch 调用断言）；422 invalid_scope（空串/空白目录 + pydantic
定位）；DELETE 锁令牌随 body。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import WikiError
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
)

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
WID = "art_wiki_src"
NOW = datetime(2026, 10, 22, 9, 0, 0, 500000, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fakes

class FakeCompileInputsDB:
    """CompileInputsDB 的内存实现：sources 按 (artifact_id, kb_id) 存行
    （软删=置 status，复活=UPDATE 回 active——与真库裁定同构）；current_
    fingerprint 由 kb_fingerprints 提供观测值。共享 artifacts dict——权威
    乐观锁比对 + touch 与 FakeWikiDB 单一真相源。"""

    def __init__(self, artifacts: dict[str, dict]) -> None:
        self.artifacts = artifacts
        # (artifact_id, kb_id) -> 绑定行
        self.sources: dict[tuple[str, str], dict] = {}
        # kb_id -> current_fingerprint 观测值（缺省 = 无就绪快照）
        self.kb_fingerprints: dict[str, dict] = {}
        # 可读 kb_id 集（is_visible 谓词桩）
        self.readable: set[str] = set()
        self.titles: dict[str, str] = {}
        # batch_id -> details（增量合并镜像）；batches 供 latest_compile_batch
        self.batch_details: dict[str, dict] = {}
        self.batches: list[dict] = []
        # touch 调用记录（乐观锁推进断言用）
        self.touches: list[str] = []

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

    async def upsert_source(
        self, artifact_id: str, kb_id: str, scope: dict, created_by: str,
        *, expected_updated_at=None,
    ) -> dict:
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
            # 更新/复活：scope 覆盖、status 复活 active、fingerprint 保留
            row["scope"] = dict(scope or {})
            row["status"] = "active"
            row["created_by"] = created_by
        self._touch(artifact_id)
        return dict(row)

    async def delete_source(
        self, artifact_id: str, kb_id: str, *, expected_updated_at=None,
    ) -> bool:
        self._assert_expected(artifact_id, expected_updated_at)
        row = self.sources.get((artifact_id, kb_id))
        if row is None or row["status"] != "active":
            return False
        row["status"] = "removed"   # 软删：绑定历史/指纹保留供复活
        self._touch(artifact_id)
        return True

    async def update_fingerprint(
        self, artifact_id: str, kb_id: str, fingerprint: dict,
    ) -> bool:
        row = self.sources.get((artifact_id, kb_id))
        if row is None:
            return False
        row["fingerprint"] = dict(fingerprint or {})
        return True

    # ---- 指纹观测 / kb 元数据 --------------------------------------------

    async def current_fingerprint(self, kb_id: str) -> dict:
        fp = self.kb_fingerprints.get(kb_id)
        if fp is None:
            return {"snapshots": [],
                    "computed_at": "2026-10-22T00:00:00+00:00"}
        return {"snapshots": [dict(s) for s in fp.get("snapshots", [])],
                "computed_at": fp.get("computed_at")}

    async def kb_snapshot_unit_count(self, kb_id: str) -> int:
        fp = await self.current_fingerprint(kb_id)
        return sum(int(s.get("unit_count") or 0) for s in fp["snapshots"])

    async def kb_readable(self, kb_id: str, user_id: str) -> bool:
        return kb_id in self.readable

    async def kb_titles(self, kb_ids: list[str]) -> dict[str, str]:
        return {k: self.titles[k] for k in kb_ids if k in self.titles}

    # ---- 编译批次 --------------------------------------------------------

    async def update_batch_details(self, batch_id: str, patch: dict) -> bool:
        if batch_id not in self.batch_details:
            return False
        self.batch_details[batch_id] = {**self.batch_details[batch_id], **patch}
        return True

    async def latest_compile_batch(self, artifact_id: str) -> dict | None:
        rows = [b for b in self.batches
                if b["artifact_id"] == artifact_id
                and b["channel"] == "wiki_compile"]
        if not rows:
            return None
        latest = max(rows, key=lambda b: (b["created_at"], b["batch_id"]))
        return {**dict(latest), "details": dict(
            self.batch_details.get(latest["batch_id"], {}))}


# ---------------------------------------------------------------- helpers

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


async def _token(env) -> str:
    return env.adb.artifacts[WID]["updated_at"].isoformat()


async def _bind(env, kb_id: str = "kb-1", directories=None, *, user=None):
    return await env.svc.upsert_source(
        user or _user("u-alice"), WID, kb_id,
        directories if directories is not None else [])


# ------------------------------------------------------------ 权限矩阵

async def test_sources_permission_matrix(env):
    """viewer：GET 200（runtime 读面）/ PUT/DELETE 403（设计态写面）；
    非成员三端点一律 404 反探测（不暴露制品存在性）。"""
    await _bind(env)
    env.holder["user"] = _user("u-viewer")

    r = await env.client.get(f"/api/artifacts/{WID}/wiki/sources")
    assert r.status_code == 200
    assert len(r.json()["sources"]) == 1
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-2", "directories": []})
    assert r.status_code == 403
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1")
    assert r.status_code == 403

    env.holder["user"] = _user("u-stranger")
    for method, url in (
        ("GET", f"/api/artifacts/{WID}/wiki/sources"),
        ("PUT", f"/api/artifacts/{WID}/wiki/sources"),
        ("DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1"),
    ):
        r = await env.client.request(
            method, url,
            json={"kb_id": "kb-1", "directories": []} if method == "PUT"
            else None)
        assert r.status_code == 404, (method, r.status_code)


async def test_get_sources_envelope_and_empty(env):
    """GET 信封 {"sources": [...]}；无绑定空列表。"""
    r = await env.client.get(f"/api/artifacts/{WID}/wiki/sources")
    assert r.status_code == 200 and r.json() == {"sources": []}


# ------------------------------------------------------------ kb 可读判定

async def test_upsert_unreadable_kb_404_anti_probe(env):
    """绑定不可读 kb → 404 反探测（等同不存在，防探测他人私有库），且不落库。"""
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-secret", "directories": []})
    assert r.status_code == 404
    assert env.inputs.sources.get((WID, "kb-secret")) is None


async def test_list_sources_kb_title_and_unreadable(env):
    """kb_title 只对可读行返回；绑定后失去读权的行 kb_title=None+unreadable
    （绑定行仍可见——制品成员看得到绑定行，只隐库元数据）。scope 往返。"""
    await _bind(env, "kb-1", ["方案/云核"], user=_user("u-bob"))  # editor 可绑
    await _bind(env, "kb-2")
    env.inputs.readable.discard("kb-2")   # 绑定后失去 kb-2 读权

    rows = {r["kb_id"]: r
            for r in await env.svc.list_sources(_user("u-alice"), WID)}
    assert rows["kb-1"]["kb_title"] == "云核知识库"
    assert rows["kb-1"]["unreadable"] is False
    assert rows["kb-1"]["scope"] == {"directories": ["方案/云核"]}
    assert rows["kb-2"]["kb_title"] is None
    assert rows["kb-2"]["unreadable"] is True


# ------------------------------------------------------------ dirty 三态

async def test_dirty_three_states(env):
    """dirty：①无指纹（刚绑定）→ True；②指纹与当前快照集一致 → False
    （(snapshot_id, unit_count) 集合序无关）；③快照变化（unit_count 漂移 /
    kb 侧快照撤空）→ True。"""
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "s1", "unit_count": 5}],
        "computed_at": "X"}

    # ① 无指纹 → 脏（提示首编/重编）
    await _bind(env)
    rows = await env.svc.list_sources(_user("u-alice"), WID)
    assert rows[0]["dirty"] is True

    # ② 一致 → 干净
    await env.inputs.update_fingerprint(WID, "kb-1", {
        "snapshots": [{"snapshot_id": "s1", "unit_count": 5}],
        "computed_at": "X"})
    rows = await env.svc.list_sources(_user("u-alice"), WID)
    assert rows[0]["dirty"] is False

    # 快照集顺序无关：[s1,s2] vs [s2,s1] 仍干净
    env.inputs.kb_fingerprints["kb-1"] = {
        "snapshots": [{"snapshot_id": "s2", "unit_count": 1},
                      {"snapshot_id": "s1", "unit_count": 5}],
        "computed_at": "Y"}
    await env.inputs.update_fingerprint(WID, "kb-1", {
        "snapshots": [{"snapshot_id": "s1", "unit_count": 5},
                      {"snapshot_id": "s2", "unit_count": 1}],
        "computed_at": "Y"})
    rows = await env.svc.list_sources(_user("u-alice"), WID)
    assert rows[0]["dirty"] is False

    # ③ unit_count 漂移 → 脏
    env.inputs.kb_fingerprints["kb-1"]["snapshots"][0]["unit_count"] = 6
    rows = await env.svc.list_sources(_user("u-alice"), WID)
    assert rows[0]["dirty"] is True

    # kb 侧快照撤空（重挖后全 removed）→ 脏
    env.inputs.kb_fingerprints["kb-1"] = {"snapshots": [], "computed_at": "Z"}
    rows = await env.svc.list_sources(_user("u-alice"), WID)
    assert rows[0]["dirty"] is True


# ------------------------------------------------------- 软删 / 复活

async def test_soft_delete_then_revive_preserves_fingerprint(env):
    """软删：list 不可见、二次删 404 source_not_found；复活：重绑同库
    status 回 active、指纹保留（绑定历史/指纹只随制品级联硬删）。"""
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": ["a/b"]})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    # 模拟编译完成写指纹
    await env.inputs.update_fingerprint(WID, "kb-1", {
        "snapshots": [{"snapshot_id": "s", "unit_count": 2}],
        "computed_at": "X"})

    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1",
        json={"expected_updated_at": await _token(env)})
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert await env.inputs.list_sources(WID) == []

    # 二次删 → 404 source_not_found（软删行不算 active 绑定）
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1")
    assert r.status_code == 404
    assert "source_not_found" in r.text

    # 复活：重绑同库 status 回 active、指纹保留供 dirty 对比
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": []})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    rows = await env.inputs.list_sources(WID)
    assert rows[0]["fingerprint"]["snapshots"] \
        == [{"snapshot_id": "s", "unit_count": 2}]


async def test_delete_nonexistent_kb_source_404(env):
    """删从未绑定的 kb → 404 source_not_found。"""
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-never")
    assert r.status_code == 404
    assert "source_not_found" in r.text


# ------------------------------------------------------------ 乐观锁 touch

async def test_upsert_touch_advances_optimistic_token(env):
    """绑定写推进乐观锁令牌（touch 调用断言）：PUT 后旧令牌再写 → 409
    stale_artifact；DELETE 同款（锁令牌随 body）。"""
    stale = await _token(env)   # PUT 前令牌
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": [],
              "expected_updated_at": stale})
    assert r.status_code == 200, r.text
    assert WID in env.inputs.touches            # touch 调用断言
    assert await _token(env) != stale           # 令牌已推进

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": ["x"],
              "expected_updated_at": stale})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1",
        json={"expected_updated_at": stale})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # 当前令牌 → DELETE 放行
    r = await env.client.request(
        "DELETE", f"/api/artifacts/{WID}/wiki/sources/kb-1",
        json={"expected_updated_at": await _token(env)})
    assert r.status_code == 200


# ------------------------------------------------------------ 校验 422

async def test_invalid_scope_422(env):
    """directories 含空串/纯空白 → 422 invalid_scope（服务层）；元素非字符串
    → pydantic 定位断言 loc=["body","directories",1]；kb_id 空 → loc=["body",
    "kb_id"]；空数组=全库合法。"""
    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": [""]})
    assert r.status_code == 422 and "invalid_scope" in r.text

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": ["a", "  "]})
    assert r.status_code == 422 and "invalid_scope" in r.text

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "kb-1", "directories": ["a", 5]})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "directories", 1]

    r = await env.client.put(
        f"/api/artifacts/{WID}/wiki/sources",
        json={"kb_id": "", "directories": []})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "kb_id"]

    # 服务层直调：非列表 directories → invalid_scope（防旁路）
    with pytest.raises(WikiError, match="invalid_scope"):
        await env.svc.upsert_source(_user("u-alice"), WID, "kb-1", "not-a-list")
    with pytest.raises(WikiError, match="invalid_scope"):
        await env.svc.upsert_source(_user("u-alice"), WID, "  ", [])

    # 空数组=全库，合法；目录元素 strip 归一
    row = await env.svc.upsert_source(_user("u-alice"), WID, "kb-1", [])
    assert row["scope"] == {"directories": []}
    row = await env.svc.upsert_source(_user("u-alice"), WID, "kb-1", [" a/b "])
    assert row["scope"] == {"directories": ["a/b"]}
