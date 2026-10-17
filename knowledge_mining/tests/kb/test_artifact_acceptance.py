"""61号 §十二 九个验收用例的测试串联 —— 表格知识制品 MVP 验收（纯 fake 本地跑绿）。

数据 = 61号 §六 示例：A.xlsx 8 行（MOP/L1分类/L2分类）建表导入发布 rev1；
B.xlsx 5 行（MOP编号/L1分类/L2分类/是否夜间执行；3 行同 MOP + 2 行新 MOP）
追加映射导入。xlsx 现造函数与 A/B 数据、A 建表 ops 全部 import 自
test_artifacts_import；FakeArtifactDB import 自 test_artifacts_service，
子类补真库 publish 语义（new_rev 取 max+1 / schema 快照进 revisions /
发布后表达式索引重建记账），向导三步与 REST 路由均打真实实现。

其中可双轨的（fake+真库）不强制真库版——真库链路由 test_artifacts_db.py
（需 PG）另行覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_artifacts import ArtifactError as ArtifactRepoError
from knowledge_mining.mining.kb.deps import (
    get_artifact_db,
    get_artifact_import_service,
    get_artifact_service,
)
from knowledge_mining.mining.kb.routes.artifacts import router as artifacts_router
from knowledge_mining.mining.kb.services.artifact_import_service import (
    _PREVIEW_TOKENS,
    ArtifactImportService,
)
from knowledge_mining.mining.kb.services.artifact_service import ArtifactService
from knowledge_mining.tests.kb.test_artifacts_import import (
    A_CREATE_OPS,
    A_HEADER,
    A_ROWS,
    B_HEADER,
    B_ROWS,
    _write_xlsx,
)
from knowledge_mining.tests.kb.test_artifacts_service import (
    NOW,
    FakeArtifactDB,
    _user,
)

pytestmark = pytest.mark.asyncio

DOMAIN = "cloud_core_network"
OWNER = _user("u-owner")


# ---------------------------------------------------------------- fake 扩展

class AcceptanceFakeArtifactDB(FakeArtifactDB):
    """FakeArtifactDB + 真库 publish 语义，够九用例验收：

    - new_rev = max(current, revisions 历史, revision_rows 历史行) + 1——
      回滚拨低指针后重发布不与既有版本撞主键（对齐真库 GREATEST 口径）。
    - revisions 记录随发布定格 schema_json 快照（真库 publish 事务第②步）。
    - 发布事务成功后重建类型化表达式索引（记账索引名，命名对齐真库
      ArtifactDB.create_expression_indexes：idx_kbtr_{artifact_id}_{column_id}）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.index_names: list[str] = []

    async def publish(
        self, artifact_id: str, change_summary: str, published_by: str,
    ) -> dict:
        self.calls.append("publish")
        art = self.artifacts[artifact_id]
        if not (art.get("usage_note") or "").strip():
            raise ArtifactRepoError("usage_note_required")
        history = [r["revision"] for r in self.revisions.get(artifact_id, [])]
        row_revs = list(self.revision_rows.get(artifact_id, {}).keys())
        new_rev = max([int(art["current_revision"]), *history, *row_revs], default=0) + 1
        rows = self.rev0_rows.get(artifact_id, [])
        self.revision_rows.setdefault(artifact_id, {})[new_rev] = [dict(r) for r in rows]
        self.revisions.setdefault(artifact_id, []).append({
            "revision": new_rev, "row_count": len(rows),
            "schema_json": [dict(c) for c in self.columns.get(artifact_id, [])],
            "change_summary": change_summary, "published_by": published_by,
            "published_at": NOW,
        })
        self.rev0_rows[artifact_id] = []
        art.update(current_revision=new_rev, status="published", draft_open=False)
        self._touch(artifact_id)
        await self.drop_draft_expression_indexes(artifact_id)
        indexed = [c for c in self.columns.get(artifact_id, []) if c.get("is_indexed")]
        await self.create_expression_indexes(artifact_id, new_rev, indexed)
        return {"revision": new_rev, "row_count": len(rows)}

    async def get_revision(self, artifact_id: str, revision: int) -> dict | None:
        for record in self.revisions.get(artifact_id, []):
            if record["revision"] == revision:
                return {
                    "artifact_id": artifact_id, "revision": revision,
                    "schema_json": [dict(c) for c in record.get("schema_json", [])],
                    "row_count": record["row_count"],
                    "change_summary": record.get("change_summary", ""),
                    "published_by": record.get("published_by", ""),
                    "published_at": record.get("published_at"),
                }
        return None

    async def create_expression_indexes(
        self, artifact_id: str, revision: int, indexed_columns: list[dict],
    ) -> list[str]:
        del revision  # 真库 revision 仅为调用方语义；索引名不含版本
        created: list[str] = []
        for col in indexed_columns:
            name = f"idx_kbtr_{artifact_id}_{col['column_id']}"
            if name not in self.index_names:
                self.index_names.append(name)
            created.append(name)
        return created

    async def drop_draft_expression_indexes(self, artifact_id: str) -> None:
        prefix = f"idx_kbtr_{artifact_id}_"
        self.index_names = [n for n in self.index_names if not n.startswith(prefix)]


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _clear_previews():
    yield
    for record in _PREVIEW_TOKENS.values():
        try:
            os.unlink(record["path"])
        except OSError:
            pass
    _PREVIEW_TOKENS.clear()


@pytest_asyncio.fixture
async def env(tmp_path):
    """service 层（db/svc/imp）+ 路由层（ASGI client）双面孔：用例1-5/8 走
    service，用例6/7/9 打真实 REST 路由（状态码口径）。"""
    db = AcceptanceFakeArtifactDB()
    db.domain_bindings.add(("u-owner", DOMAIN))
    svc = ArtifactService(db)
    imp = ArtifactImportService(db)
    app = FastAPI()
    app.include_router(artifacts_router)
    holder = {"user": _user("u-owner")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_artifact_db] = lambda: db
    app.dependency_overrides[get_artifact_service] = lambda: svc
    app.dependency_overrides[get_artifact_import_service] = lambda: imp
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            db=db, svc=svc, imp=imp, client=client, holder=holder,
            tmp_path=tmp_path,
        )


def _make_xlsx_a(tmp_path: Path) -> bytes:
    return _write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS)


def _make_xlsx_b(tmp_path: Path) -> bytes:
    return _write_xlsx(tmp_path / "B.xlsx", B_HEADER, B_ROWS)


async def _published_rev1(env) -> str:
    """用例2-9 共同前置：A.xlsx 建表导入（MOP 主键）8 行 → usage_note → 发布 rev1。"""
    art = await env.svc.create_artifact(
        user=OWNER, domain_id=DOMAIN, title="MOP 责任域表")
    aid = art["artifact_id"]
    out = env.imp.parse_upload(_make_xlsx_a(env.tmp_path), "A.xlsx")
    plan = env.imp.build_mapping(out["preview_token"], column_ops=A_CREATE_OPS)
    report = await env.imp.execute_import(
        env.svc, user=OWNER, artifact_id=aid,
        preview_token=out["preview_token"], plan=plan,
    )
    assert report["inserted"] == 8
    await env.svc.update_meta(
        user=OWNER, artifact_id=aid, fields={"usage_note": "割接前查 MOP 责任域"})
    result = await env.svc.publish(user=OWNER, artifact_id=aid, change_summary="首版")
    assert result["revision"] == 1 and result["row_count"] == 8
    return aid


async def _append_b(env, aid: str) -> dict:
    """B.xlsx 追加导入（MOP编号 map_to MOP / L1分类 map_to / L2分类 drop /
    是否夜间执行 new）→ 返回导入报告。"""
    column_ids = {
        c["column_name"]: c["column_id"]
        for c in await env.svc.list_columns(user=OWNER, artifact_id=aid)
    }
    out = env.imp.parse_upload(_make_xlsx_b(env.tmp_path), "B.xlsx")
    ops = [
        {"source": "MOP编号", "action": "map_to", "target": column_ids["MOP"]},
        {"source": "L1分类", "action": "map_to", "target": column_ids["L1分类"]},
        {"source": "L2分类", "action": "drop"},
        {"source": "是否夜间执行", "action": "new", "target_name": "是否夜间执行",
         "column_type": "text"},
    ]
    plan = env.imp.build_mapping(
        out["preview_token"], column_ops=ops,
        target_columns=await env.svc.list_columns(user=OWNER, artifact_id=aid),
    )
    return await env.imp.execute_import(
        env.svc, user=OWNER, artifact_id=aid,
        preview_token=out["preview_token"], plan=plan,
        conflict_policy={"type_error": "tolerant"},
    )


# ------------------------------------------------------------ 九个验收用例

class TestSixtyOneAcceptanceCases:
    """61号 §十二 用例编号一一对应（test_case1..test_case9）。"""

    async def test_case1_create_import_publish(self, env):
        """用例1：create → upload A → import（建表）→ 填 usage_note → publish rev1。"""
        art = await env.svc.create_artifact(
            user=OWNER, domain_id=DOMAIN, title="MOP 责任域表")
        aid = art["artifact_id"]
        assert art["status"] == "draft" and art["current_revision"] == 0

        out = env.imp.parse_upload(_make_xlsx_a(env.tmp_path), "A.xlsx")
        plan = env.imp.build_mapping(out["preview_token"], column_ops=A_CREATE_OPS)
        assert plan["mode"] == "create"
        report = await env.imp.execute_import(
            env.svc, user=OWNER, artifact_id=aid,
            preview_token=out["preview_token"], plan=plan,
        )
        assert report["inserted"] == 8

        await env.svc.update_meta(
            user=OWNER, artifact_id=aid, fields={"usage_note": "割接前查 MOP 责任域"})
        result = await env.svc.publish(user=OWNER, artifact_id=aid, change_summary="首版")
        assert result["revision"] == 1
        assert result["row_count"] == 8

        artifact = await env.db.get_artifact(aid)
        assert artifact["current_revision"] == 1
        assert artifact["draft_open"] is False
        assert artifact["status"] == "published"

        # 版本快照 schema_json：L1分类 enum 列 + MOP is_primary_key
        revision = await env.db.get_revision(aid, 1)
        schema = {c["column_name"]: c for c in revision["schema_json"]}
        assert schema["L1分类"]["column_type"] == "enum"
        assert schema["L1分类"]["enum_values"] == ["数据业务", "网络维护", "云资源"]
        assert schema["MOP"]["is_primary_key"] is True
        assert revision["row_count"] == 8

        # 表达式索引：is_indexed 的 MOP 列一条（fake 记账镜像真库命名规则）
        columns = await env.db.list_columns(aid)
        mop_id = next(c["column_id"] for c in columns if c["column_name"] == "MOP")
        assert env.db.index_names == [f"idx_kbtr_{aid}_{mop_id}"]

    async def test_case2_agent_consumption_contract(self, env):
        """用例2：Agent 消费。Agent 链路（DSL→SQL→行视图）由 serving
        ArtifactQueryServiceTest（24 例）+ mcp_server/tests/test_artifact_tools.py
        （12 例）覆盖；此处钉 Python 侧契约：按 revision 读行时 provenance
        完整可溯源（batch_id/file_name/sheet_name/row_index 四键）。"""
        aid = await _published_rev1(env)
        rows = (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, revision=1))["rows"]
        assert len(rows) == 8
        provenance = rows[0]["provenance"]
        assert len(provenance) == 1
        assert set(provenance[0]) == {"batch_id", "file_name", "sheet_name",
                                      "row_index"}
        assert provenance[0]["file_name"] == "A.xlsx"
        assert provenance[0]["sheet_name"] == "Sheet1"
        assert provenance[0]["row_index"] == 2  # Excel 物理行号（表头=第1行）

    async def test_case3_append_import_mapping(self, env):
        """用例3：B.xlsx 追加导入（列映射）——同 MOP 更新双来源 / 新 MOP 插入 /
        diff vs current 数字与实际一致。"""
        aid = await _published_rev1(env)
        report = await _append_b(env, aid)
        assert report["inserted"] == 2   # NC20260601000401 / 402 新 MOP
        assert report["updated"] == 3    # 295 / 297 / 300 同 MOP
        assert [m["column_name"] for m in report["missing_columns"]] == ["L2分类"]

        rows = {r["row_key"]: r for r in env.db.rev0_rows[aid]}
        # 同 MOP 行：data 覆盖（UPSERT）+ provenance 双来源
        shared = rows["NC20260601000295"]
        assert shared["data"]["是否夜间执行"] == "是"
        assert len(shared["provenance"]) == 2
        assert {p["file_name"] for p in shared["provenance"]} == {"A.xlsx", "B.xlsx"}
        # 新行插入（drop 列不进 data）
        assert rows["NC20260601000401"]["data"]["L1分类"] == "数据业务"
        assert "L2分类" not in rows["NC20260601000401"]["data"]
        # schema 已含追加新列
        assert any(c["column_name"] == "是否夜间执行"
                   for c in await env.db.list_columns(aid))

        diff = await env.svc.diff_draft_vs_current(user=OWNER, artifact_id=aid)
        assert diff["added_count"] == 2
        assert diff["removed_count"] == 0
        # 3 条被覆盖行 × 2 列变更（L2分类 置空 + 是否夜间执行 赋值）= 6 条列级
        assert diff["changed_count"] == 6
        assert {"row_key": "NC20260601000295", "column": "是否夜间执行",
                "old": None, "new": "是"} in diff["changed"]

    async def test_case4_rollback_roundtrip(self, env):
        """用例4：回滚往返——拨指针数据不动；拨低后重发布 new_rev 取 max+1
        不撞既有版本主键。"""
        aid = await _published_rev1(env)
        await _append_b(env, aid)
        result = await env.svc.publish(user=OWNER, artifact_id=aid,
                                       change_summary="追加 B")
        assert result["revision"] == 2 and result["row_count"] == 10

        await env.svc.rollback_to(user=OWNER, artifact_id=aid, revision=1)
        artifact = await env.db.get_artifact(aid)
        assert artifact["current_revision"] == 1
        # rev2 行数据仍在（历史读可读）
        assert (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, revision=2))["total"] == 10

        # 拨回 rev2（往返）
        await env.svc.rollback_to(user=OWNER, artifact_id=aid, revision=2)
        assert (await env.db.get_artifact(aid))["current_revision"] == 2

        # 拨低后重发布：ensure_draft 从 rev1 检出 8 行 → 新版本取 max+1 = rev3
        await env.svc.rollback_to(user=OWNER, artifact_id=aid, revision=1)
        await env.svc.update_cell(
            user=OWNER, artifact_id=aid, row_key="NC20260601000295",
            column="L2分类", value="演练前核查")
        result = await env.svc.publish(user=OWNER, artifact_id=aid,
                                       change_summary="回滚后重发")
        assert result["revision"] == 3
        assert result["row_count"] == 8
        # 三个版本各自独立可读
        assert [r["revision"] for r in await env.db.list_revisions(aid)] == [3, 2, 1]
        assert (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, revision=3))["total"] == 8
        assert (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, revision=2))["total"] == 10

    async def test_case5_discard_restores(self, env):
        """用例5：discard 复原——乱改 draft 后丢弃，draft 视图回到与 current 一致。"""
        aid = await _published_rev1(env)
        current_rows = (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, revision=1))["rows"]

        await env.svc.update_cell(
            user=OWNER, artifact_id=aid, row_key="NC20260601000295",
            column="L1分类", value="乱改")
        await env.svc.delete_row(
            user=OWNER, artifact_id=aid, row_key="NC20260601000296")
        diff = await env.svc.diff_draft_vs_current(user=OWNER, artifact_id=aid)
        assert diff["changed_count"] == 1 and diff["removed_count"] == 1

        artifact = await env.svc.discard_draft(user=OWNER, artifact_id=aid)
        assert artifact["draft_open"] is False
        draft_rows = (await env.svc.list_rows(
            user=OWNER, artifact_id=aid, draft=True))["rows"]
        assert draft_rows == current_rows

    async def test_case6_permission_matrix_routes(self, env):
        """用例6：权限矩阵路由级——viewer 已发布 rows 200 / draft 403；
        非成员 GET 详情 404（反探测）；editor 删 403；owner 删 → 六表级联清理。"""
        aid = await _published_rev1(env)
        await env.svc.upsert_member(
            user=OWNER, artifact_id=aid, member_user_id="u-editor", role="editor")
        await env.svc.upsert_member(
            user=OWNER, artifact_id=aid, member_user_id="u-viewer", role="viewer")

        env.holder["user"] = _user("u-viewer")
        r = await env.client.get(f"/api/artifacts/{aid}/table/rows")
        assert r.status_code == 200 and r.json()["total"] == 8
        r = await env.client.get(f"/api/artifacts/{aid}/table/rows?draft=1")
        assert r.status_code == 403  # 成员无权=403（REST 口径）

        env.holder["user"] = _user("u-stranger")
        r = await env.client.get(f"/api/artifacts/{aid}")
        assert r.status_code == 404  # 反探测：非成员 404，不是 403

        env.holder["user"] = _user("u-editor")
        r = await env.client.delete(f"/api/artifacts/{aid}")
        assert r.status_code == 403
        assert await env.db.get_artifact(aid) is not None

        env.holder["user"] = OWNER
        r = await env.client.delete(f"/api/artifacts/{aid}")
        assert r.status_code == 200
        # fake 记账断言六表清理（kb_artifacts / members / revisions / batches /
        # columns / rows[rev0+历史]）
        assert aid not in env.db.artifacts
        assert aid not in env.db.members
        assert aid not in env.db.revisions
        assert aid not in env.db.batches
        assert aid not in env.db.columns
        assert aid not in env.db.rev0_rows and aid not in env.db.revision_rows

    async def test_case7_usage_note_gate(self, env):
        """用例7：usage_note 阻断——空说明发布 422，填写后放行。"""
        r = await env.client.post(
            "/api/artifacts", json={"title": "MOP 责任域表", "domain_id": DOMAIN})
        assert r.status_code == 201, r.text
        aid = r.json()["artifact_id"]

        r = await env.client.post(
            f"/api/artifacts/{aid}/publish", json={"change_summary": "x"})
        assert r.status_code == 422

        r = await env.client.patch(
            f"/api/artifacts/{aid}", json={"usage_note": "割接前查 MOP 责任域"})
        assert r.status_code == 200
        r = await env.client.post(
            f"/api/artifacts/{aid}/publish", json={"change_summary": "首版"})
        assert r.status_code == 200 and r.json()["revision"] == 1

    async def test_case8_deprecate_republish(self, env):
        """用例8：停用恢复——deprecate → deprecated；republish → published。
        serving 侧对 deprecated 制品的滤除（st_ 源只取 published）由 Task7
        ArtifactQueryServiceTest 覆盖。"""
        aid = await _published_rev1(env)
        artifact = await env.svc.deprecate(user=OWNER, artifact_id=aid)
        assert artifact["status"] == "deprecated"
        assert artifact["current_revision"] == 1  # 状态流转不动版本
        artifact = await env.svc.republish(user=OWNER, artifact_id=aid)
        assert artifact["status"] == "published"

    async def test_case9_alias_compat(self, env):
        """用例9：alias 兼容读——列改名写 alias（旧名留档）。
        DSL 旧名展开（alias→现名）在 serving ArtifactQueryServiceTest 用例断言。"""
        aid = await _published_rev1(env)
        columns = await env.svc.list_columns(user=OWNER, artifact_id=aid)
        mop_id = next(c["column_id"] for c in columns if c["column_name"] == "MOP")

        r = await env.client.patch(
            f"/api/artifacts/{aid}/table/columns/{mop_id}",
            json={"column_name": "MOP单号"})
        assert r.status_code == 200, r.text
        assert r.json()["column_name"] == "MOP单号"
        assert r.json()["alias"] == ["MOP"]

        columns = {
            c["column_name"]: c
            for c in await env.svc.list_columns(user=OWNER, artifact_id=aid)
        }
        assert columns["MOP单号"]["alias"] == ["MOP"]
        assert columns["MOP单号"]["is_primary_key"] is True  # 改名不动主键标记
