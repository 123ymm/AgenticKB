"""制品 REST 路由（61号 §十）—— httpx ASGI + fake service/db（不碰真库）。

dependency_overrides 注入真实 ArtifactService(FakeArtifactDB) 与真实导入服务，
打真实路由层：状态码映射（404/403/422/409/410/413）+ 向导全链（上传→预览→
导入→发布→查行）。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from knowledge_mining.mining.kb.auth import current_user
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
from knowledge_mining.tests.kb.test_artifacts_service import FakeArtifactDB, _user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_previews():
    """预览登记是模块级全局——逐测试清场（防 uploader 计数跨测试串扰）。"""
    yield
    for record in _PREVIEW_TOKENS.values():
        try:
            os.unlink(record["path"])
        except OSError:
            pass
    _PREVIEW_TOKENS.clear()

DOMAIN = "cloud_core_network"
A_HEADER = ["MOP", "L1分类", "L2分类"]
A_ROWS = [
    ("NC20260601000295", "数据业务", "网络部署/扩容"),
    ("NC20260601000296", "数据业务", "业务开通"),
    ("NC20260601000297", "网络维护", "隐患排查"),
    ("NC20260601000298", "网络维护", "日常巡检"),
    ("NC20260601000299", "数据业务", "网络部署/扩容"),
    ("NC20260601000300", "云资源", "资源开通"),
    ("NC20260601000301", "云资源", "资源释放"),
    ("NC20260601000302", "网络维护", "故障处置"),
]


def _xlsx_bytes(path: Path, header: list[str], rows: list[tuple]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Sheet1")
    sheet.append(header)
    for row in rows:
        sheet.append(list(row))
    workbook.save(str(path))
    return path.read_bytes()


@pytest_asyncio.fixture
async def env(tmp_path):
    db = FakeArtifactDB()
    # codex二审批号⑧：非 admin 成员路径需当前域绑定——测试内切换的成员用户
    # 绑域；u-stranger 刻意不绑（保留「无域绑定 403/404」门禁断言的锐度）。
    db.domain_bindings.update((uid, DOMAIN) for uid in ("u-alice", "u-viewer", "u-bob"))
    svc = ArtifactService(db)
    imp = ArtifactImportService(db)
    app = FastAPI()
    app.include_router(artifacts_router)
    holder = {"user": _user("u-alice")}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    app.dependency_overrides[get_artifact_db] = lambda: db
    app.dependency_overrides[get_artifact_service] = lambda: svc
    app.dependency_overrides[get_artifact_import_service] = lambda: imp
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            db=db, svc=svc, imp=imp, app=app, client=client,
            holder=holder, tmp_path=tmp_path,
        )


async def _create_artifact(env) -> str:
    r = await env.client.post("/api/artifacts", json={"title": "MOP 责任域表"})
    assert r.status_code == 201, r.text
    return r.json()["artifact_id"]


async def _upload_a(env, aid: str) -> str:
    data = _xlsx_bytes(env.tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("A.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    return r.json()["preview_token"]


# ----------------------------------------------------------------- CRUD

async def test_create_list_detail_patch_delete(env):
    aid = await _create_artifact(env)
    assert env.db.artifacts[aid]["domain_id"] == DOMAIN  # 缺省回落绑定域

    r = await env.client.get("/api/artifacts")
    assert r.status_code == 200
    assert [a["artifact_id"] for a in r.json()["artifacts"]] == [aid]

    r = await env.client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 200
    assert r.json()["title"] == "MOP 责任域表"
    assert r.json()["schema"] == []
    # 批次4b：详情附 my_role（创建者 owner 兜底）——前端 canWrite 判定口径
    assert r.json()["my_role"] == "owner"

    r = await env.client.patch(
        f"/api/artifacts/{aid}",
        json={"title": "MOP 表", "description": "割接前查责任域"},
    )
    assert r.status_code == 200 and r.json()["title"] == "MOP 表"

    # 删除门禁（008）：draft 态直接删 → 409 artifact_not_deprecated；
    # 停用后放行（owner 硬删）。
    r = await env.client.delete(f"/api/artifacts/{aid}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "artifact_not_deprecated"
    assert await env.db.get_artifact(aid) is not None
    r = await env.client.post(f"/api/artifacts/{aid}/deprecate")
    assert r.status_code == 200
    r = await env.client.delete(f"/api/artifacts/{aid}")
    assert r.status_code == 200
    assert await env.db.get_artifact(aid) is None
    r = await env.client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 404


async def test_create_requires_domain_grant(env):
    env.holder["user"] = _user("u-stranger")
    r = await env.client.post("/api/artifacts", json={"title": "无域制品"})
    assert r.status_code in (403, 422)
    # 显式无绑定域 → 403（域门禁）
    r = await env.client.post(
        "/api/artifacts", json={"title": "无域制品", "domain_id": DOMAIN})
    assert r.status_code == 403


# ------------------------------------------------------------- kind 放开

async def test_create_artifact_wiki_kind(env):
    """wiki 制品创建（路由层透传 kind；service/db 61号已支持）。"""
    r = await env.client.post("/api/artifacts", json={
        "title": "云核命令Wiki", "domain_id": DOMAIN, "kind": "wiki",
    })
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "wiki"


async def test_create_artifact_invalid_kind_rejected(env):
    """非法 kind 在 pydantic Literal 层即被拦截（422），不进 service。"""
    r = await env.client.post("/api/artifacts", json={
        "title": "x", "domain_id": DOMAIN, "kind": "graph",
    })
    assert r.status_code == 422  # pydantic Literal 层拦截
    assert r.json()["detail"][0]["loc"] == ["body", "kind"]  # 定位到 kind 字段


async def test_create_artifact_default_kind_is_table(env):
    """不传 kind 的既有调用方零破坏——默认仍为 table。"""
    r = await env.client.post("/api/artifacts", json={
        "title": "y", "domain_id": DOMAIN,
    })
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "table"


# ------------------------------------------------------- 向导全链（核心）

async def test_wizard_upload_preview_import_publish_rows(env):
    aid = await _create_artifact(env)
    data = _xlsx_bytes(env.tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("A.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    token = body["preview_token"]
    columns = {c["source_name"]: c for c in body["preview"]["columns"]}
    assert columns["L1分类"]["inferred_type"] == "enum"

    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/{token}")
    assert r.status_code == 200
    assert r.json()["preview"]["file_name"] == "A.xlsx"

    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={
            "preview_token": token,
            "plan": {
                "sheet": "Sheet1", "header_row": 1,
                "column_ops": [
                    {"source": "MOP", "action": "new", "target_name": "MOP",
                     "column_type": "text", "is_primary_key": True},
                    {"source": "L1分类", "action": "new", "target_name": "L1分类",
                     "column_type": "enum",
                     "enum_values": ["数据业务", "网络维护", "云资源"]},
                    {"source": "L2分类", "action": "new", "target_name": "L2分类",
                     "column_type": "text"},
                ],
            },
            "conflict_policy": {"type_error": "tolerant"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 8

    r = await env.client.patch(
        f"/api/artifacts/{aid}", json={"usage_note": "割接前查 MOP 责任域"})
    assert r.status_code == 200
    r = await env.client.post(
        f"/api/artifacts/{aid}/publish", json={"change_summary": "首版"})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1

    r = await env.client.get(f"/api/artifacts/{aid}/table/rows")
    assert r.status_code == 200
    assert r.json()["total"] == 8

    r = await env.client.get(f"/api/artifacts/{aid}/table/columns")
    assert r.status_code == 200
    assert [c["column_name"] for c in r.json()["columns"]] == A_HEADER

    r = await env.client.get(f"/api/artifacts/{aid}/batches")
    assert r.status_code == 200
    assert r.json()["batches"][0]["row_count"] == 8

    # 预览已消费 → 再取 410
    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/{token}")
    assert r.status_code == 410


# ------------------------------------------------------------- 版本历史

async def test_list_revisions_runtime_readable(env):
    """GET /revisions：运行态信息（viewer+ 可读），倒序，非成员 404 反探测。"""
    aid = await _create_artifact(env)
    env.db.rev0_rows[aid] = [{"row_key": "r1", "data": {"MOP": "NC-1"}}]
    await env.client.patch(f"/api/artifacts/{aid}", json={"usage_note": "查责任域"})
    r = await env.client.post(f"/api/artifacts/{aid}/publish", json={"change_summary": "首版"})
    assert r.status_code == 200, r.text
    # 二次发布前检出 draft（对抗审查修复③：干净态发布 → 409 no_pending_changes）
    await env.db.ensure_draft(aid)
    env.db.rev0_rows[aid] = [{"row_key": "r2", "data": {"MOP": "NC-2"}}]
    r = await env.client.post(f"/api/artifacts/{aid}/publish", json={"change_summary": "补充行"})
    assert r.status_code == 200 and r.json()["revision"] == 2

    r = await env.client.get(f"/api/artifacts/{aid}/revisions")
    assert r.status_code == 200, r.text
    revisions = r.json()["revisions"]
    assert [rev["revision"] for rev in revisions] == [2, 1]  # revision 倒序
    assert revisions[0]["artifact_id"] == aid
    assert revisions[0]["row_count"] == 1
    assert revisions[0]["change_summary"] == "补充行"
    assert revisions[0]["published_by"] == "u-alice"
    assert revisions[0]["published_at"]

    # viewer 成员可读（运行态信息）；非成员 404 反探测
    env.db.members[aid]["u-viewer"] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{aid}/revisions")
    assert r.status_code == 200
    env.holder["user"] = _user("u-stranger")
    r = await env.client.get(f"/api/artifacts/{aid}/revisions")
    assert r.status_code == 404


# ------------------------------------------------------------- 错误映射

async def test_list_columns_runtime_returns_published_snapshot(env):
    """codex二审功能项②路由级：?runtime=true → 发布定格快照列
    （revisions.schema_json，viewer+ 可读）——设计态草稿新列不漏进运行态；
    viewer 默认（设计态）403、runtime 200。"""
    aid = await _create_artifact(env)
    token = await _upload_a(env, aid)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={"preview_token": token, "plan": {"column_ops": [
            {"source": "MOP", "action": "new", "target_name": "MOP",
             "column_type": "text", "is_primary_key": True},
            {"source": "L1分类", "action": "new", "target_name": "L1分类",
             "column_type": "text"},
        ]}},
    )
    assert r.status_code == 200, r.text
    await env.client.patch(f"/api/artifacts/{aid}", json={"usage_note": "查责任域"})
    r = await env.client.post(
        f"/api/artifacts/{aid}/publish", json={"change_summary": "r1"})
    assert r.status_code == 200, r.text

    # 设计态草稿加新列（未发布）——默认口径可见，runtime 口径不可见
    await env.db.ensure_draft(aid)
    env.db.columns[aid].append({
        "column_id": "c_draft", "column_name": "草稿新列", "column_type": "text",
        "is_indexed": False, "is_primary_key": False, "ordinal": 3,
        "status": "active",
    })
    r = await env.client.get(f"/api/artifacts/{aid}/table/columns")
    assert [c["column_name"] for c in r.json()["columns"]] == ["MOP", "L1分类", "草稿新列"]
    r = await env.client.get(f"/api/artifacts/{aid}/table/columns?runtime=true")
    assert r.status_code == 200, r.text
    assert [c["column_name"] for c in r.json()["columns"]] == ["MOP", "L1分类"]

    # viewer：设计态 403 / runtime 200（运行态快照对全员开放）
    env.db.members[aid]["u-viewer"] = "viewer"
    env.holder["user"] = _user("u-viewer")
    r = await env.client.get(f"/api/artifacts/{aid}/table/columns")
    assert r.status_code == 403
    r = await env.client.get(f"/api/artifacts/{aid}/table/columns?runtime=true")
    assert r.status_code == 200
    assert [c["column_name"] for c in r.json()["columns"]] == ["MOP", "L1分类"]

    # 未发布制品 runtime → 空数组（无快照可读）
    aid2 = await _create_artifact(env)
    r = await env.client.get(f"/api/artifacts/{aid2}/table/columns?runtime=true")
    assert r.status_code == 200 and r.json()["columns"] == []


async def test_permission_404_and_403_mapping(env):
    aid = await _create_artifact(env)
    env.db.members[aid]["u-viewer"] = "viewer"  # viewer 成员
    env.holder["user"] = _user("u-stranger")  # 非成员
    r = await env.client.get(f"/api/artifacts/{aid}")
    assert r.status_code == 404
    env.holder["user"] = _user("u-viewer")  # 成员但 viewer → 设计态 403
    r = await env.client.get(f"/api/artifacts/{aid}/table/columns")
    assert r.status_code == 403
    r = await env.client.get(f"/api/artifacts/{aid}/table/rows?draft=1")
    assert r.status_code == 403


async def test_publish_without_usage_note_maps_422(env):
    aid = await _create_artifact(env)
    r = await env.client.post(
        f"/api/artifacts/{aid}/publish", json={"change_summary": "x"})
    assert r.status_code == 422


async def test_stale_updated_at_maps_409(env):
    aid = await _create_artifact(env)
    r = await env.client.patch(
        f"/api/artifacts/{aid}",
        json={"title": "新名", "expected_updated_at": "2000-01-01T00:00:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"


async def test_map_error_wiki_no_pages_maps_409():
    """63号批次2 状态码归一：wiki 空页发布门禁经 lifecycle 翻译为
    InvalidOperation("wiki_no_pages")（publish 走本路由），_map_error 特判
    409（发布门禁族，对齐 no_pending_changes）——表格路径永不产生该码。"""
    from knowledge_mining.mining.kb.routes.artifacts import _map_error
    from knowledge_mining.mining.kb.services.artifact_service import (
        InvalidOperation,
    )

    mapped = _map_error(InvalidOperation("wiki_no_pages"))
    assert mapped.status_code == 409
    assert mapped.detail["code"] == "wiki_no_pages"
    # 其他 InvalidOperation 码不受特判影响（仍 400 裸串）
    assert _map_error(InvalidOperation("row_not_found")).status_code == 400


async def test_preview_unknown_token_maps_410(env):
    aid = await _create_artifact(env)
    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/deadbeef")
    assert r.status_code == 410


async def test_publish_clean_state_maps_409_no_pending_changes(env):
    """对抗审查修复③路由级：干净态（draft 关闭且 current>0）再发布 →
    409 no_pending_changes；开草稿编辑后恢复。"""
    aid = await _create_artifact(env)
    env.db.rev0_rows[aid] = [{"row_key": "r1", "data": {"MOP": "NC-1"}}]
    await env.client.patch(f"/api/artifacts/{aid}", json={"usage_note": "查责任域"})
    r = await env.client.post(f"/api/artifacts/{aid}/publish", json={"change_summary": "首版"})
    assert r.status_code == 200, r.text
    r = await env.client.post(f"/api/artifacts/{aid}/publish", json={"change_summary": "再发"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_pending_changes"
    # 检出草稿后（编辑动作亦会自动开）发布恢复 200
    await env.db.ensure_draft(aid)
    r = await env.client.post(f"/api/artifacts/{aid}/publish", json={"change_summary": "r2"})
    assert r.status_code == 200 and r.json()["revision"] == 2


async def test_row_write_defenses_map_400(env):
    """对抗审查修复⑩路由级：公式注入 400 / 未知列 400（update_cell 与
    batch-assign 双入口）。"""
    aid = await _create_artifact(env)
    token = await _upload_a(env, aid)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={"preview_token": token, "plan": {"column_ops": [
            {"source": "MOP", "action": "new", "target_name": "MOP",
             "column_type": "text", "is_primary_key": True},
            {"source": "L1分类", "action": "new", "target_name": "L1分类",
             "column_type": "text"},
        ]}},
    )
    assert r.status_code == 200, r.text

    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/4e433230323630363031303030323935",
        json={"column": "L1分类", "value": "=SUM(A1)"})
    assert r.status_code == 400 and "formula_not_allowed" in r.text
    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/4e433230323630363031303030323935",
        json={"column": "不存在列", "value": "x"})
    assert r.status_code == 400 and "unknown_column" in r.text
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows/batch-assign",
        json={"column": "不存在列", "value": "x", "where": []})
    assert r.status_code == 400 and "unknown_column" in r.text
    # 行增：公式 / 未知键同样 400
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows", json={"data": {"MOP": "+1"}})
    assert r.status_code == 400 and "formula_not_allowed" in r.text
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows", json={"data": {"幽灵列": "x"}})
    assert r.status_code == 400 and "unknown_column" in r.text


async def test_preview_token_bound_to_artifact_and_uploader(env):
    """对抗审查修复⑬路由级：上传登记绑定 artifact_id+uploader——token 跨
    制品/跨用户使用 → 404（不暴露登记内容）。"""
    aid = await _create_artifact(env)
    other = await _create_artifact(env)
    token = await _upload_a(env, aid)   # 绑定 (aid, u-alice)

    r = await env.client.get(f"/api/artifacts/{other}/table/preview/{token}")
    assert r.status_code == 404
    r = await env.client.post(
        f"/api/artifacts/{other}/table/import",
        json={"preview_token": token, "plan": {"column_ops": [
            {"source": "MOP", "action": "new", "target_name": "MOP",
             "column_type": "text", "is_primary_key": True}]}},
    )
    assert r.status_code == 404
    # 同制品其他编辑者用他人 token → 404
    env.db.members[aid]["u-bob"] = "editor"
    env.holder["user"] = _user("u-bob")
    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/{token}")
    assert r.status_code == 404
    env.holder["user"] = _user("u-alice")
    # 本人本制品 → 200
    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/{token}")
    assert r.status_code == 200


async def test_preview_limit_maps_429(env):
    """对抗审查修复⑭路由级：同一 uploader 第 6 个活跃预览 → 429（最旧被清，
    立即重试成功）。"""
    aid = await _create_artifact(env)
    for _ in range(5):
        await _upload_a(env, aid)
    data = _xlsx_bytes(env.tmp_path / "A6.xlsx", A_HEADER, A_ROWS)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("A6.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 429
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("A6.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 202


async def test_upload_too_large_maps_413(env, monkeypatch):
    import knowledge_mining.mining.kb.routes.artifacts as artifacts_routes

    monkeypatch.setattr(artifacts_routes, "_UPLOAD_BODY_BUDGET", 10)
    aid = await _create_artifact(env)
    data = _xlsx_bytes(env.tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("A.xlsx", data, "application/octet-stream")},
    )
    assert r.status_code == 413


async def test_import_key_conflict_maps_409(env):
    aid = await _create_artifact(env)
    header = ["MOP"]
    rows = [("NC-1",), ("NC-1",)]  # 建表即遇文件内主键重复 → block 默认
    data = _xlsx_bytes(env.tmp_path / "C.xlsx", header, rows)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/upload",
        files={"file": ("C.xlsx", data, "application/octet-stream")},
    )
    token = r.json()["preview_token"]
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={
            "preview_token": token,
            "plan": {"column_ops": [
                {"source": "MOP", "action": "new", "target_name": "MOP",
                 "column_type": "text", "is_primary_key": True},
            ]},
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "primary_key_conflict"
    assert env.db.rev0_rows[aid] == []  # 不落任何行


# ------------------------------------------------------------- 表格编辑

async def test_cell_update_batch_assign_delete_row(env):
    aid = await _create_artifact(env)
    token = await _upload_a(env, aid)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={
            "preview_token": token,
            "plan": {"column_ops": [
                {"source": "MOP", "action": "new", "target_name": "MOP",
                 "column_type": "text", "is_primary_key": True},
                {"source": "L1分类", "action": "new", "target_name": "L1分类",
                 "column_type": "text"},
                {"source": "L2分类", "action": "new", "target_name": "L2分类",
                 "column_type": "text"},
            ]},
        },
    )
    assert r.status_code == 200, r.text

    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/4e433230323630363031303030323935",
        json={"column": "L2分类", "value": "演练前核查"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["L2分类"] == "演练前核查"

    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows/batch-assign",
        json={"column": "L1分类", "value": "数据业务",
              "where": [{"column": "L1分类", "op": "eq", "value": "网络维护"}]},
    )
    assert r.status_code == 200 and r.json()["updated"] == 3

    r = await env.client.request(
        "DELETE", f"/api/artifacts/{aid}/table/rows/4e433230323630363031303030333032")
    assert r.status_code == 200
    r = await env.client.get(f"/api/artifacts/{aid}/table/rows?draft=1")
    assert r.json()["total"] == 7


async def test_insert_row_manual_add(env):
    """POST /table/rows 手工行增（61号 §7.2）：有主键→拼主键（冲突 409 /
    缺失 400）；无主键→uuid；provenance 记 manual 来源。"""
    aid = await _create_artifact(env)
    token = await _upload_a(env, aid)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={
            "preview_token": token,
            "plan": {"column_ops": [
                {"source": "MOP", "action": "new", "target_name": "MOP",
                 "column_type": "text", "is_primary_key": True},
                {"source": "L1分类", "action": "new", "target_name": "L1分类",
                 "column_type": "text"},
                {"source": "L2分类", "action": "new", "target_name": "L2分类",
                 "column_type": "text"},
            ]},
        },
    )
    assert r.status_code == 200, r.text

    # 有主键：row_key = 主键值拼接
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows",
        json={"data": {"MOP": "NC20260601000410", "L1分类": "数据业务",
                       "L2分类": "演练前核查"}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["row_key"] == "4e433230323630363031303030343130"   # hex 编码（三审 P1-1）
    assert body["data"]["L2分类"] == "演练前核查"
    assert body["ordinal"] == 9
    assert body["provenance"] == [{"channel": "manual", "created_by": "u-alice"}]

    # 同主键再插 → 409
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows",
        json={"data": {"MOP": "NC20260601000410", "L1分类": "网络维护"}},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "primary_key_conflict"

    # 主键值缺失 → 400
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/rows",
        json={"data": {"L1分类": "数据业务"}},
    )
    assert r.status_code == 400

    # 落进 draft：8 导入 + 1 手工
    r = await env.client.get(f"/api/artifacts/{aid}/table/rows?draft=1")
    assert r.json()["total"] == 9

    # 无主键制品 → row_key=uuid（先建列——行增防线要求 data 键都是 active 列）
    aid2 = await _create_artifact(env)
    r = await env.client.post(
        f"/api/artifacts/{aid2}/table/columns",
        json={"column_name": "备注", "column_type": "text"},
    )
    assert r.status_code == 201, r.text
    r = await env.client.post(
        f"/api/artifacts/{aid2}/table/rows",
        json={"data": {"备注": "手工行"}},
    )
    assert r.status_code == 201, r.text
    key = r.json()["row_key"]
    assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)


async def test_row_key_hex_gateway_safe_and_addressable_in_routes(env):
    """codex三审 P1-1 回归：含 '/'、':'、'%'、中文的主键值 → row_key 纯
    [0-9a-f.]（encodeURIComponent 不转义 → 网关不拒 % 编码字节），行路由
    PATCH/DELETE 可寻址。"""
    from knowledge_mining.mining.kb.db_artifacts import _encode_key_parts

    aid = await _create_artifact(env)
    env.db.columns[aid] = [
        {"column_id": "c1", "column_name": "路径", "column_type": "text",
         "is_indexed": False, "is_primary_key": False, "ordinal": 1,
         "status": "active"},
    ]
    key = _encode_key_parts(["a/b:c%", "机房/一"])
    assert all(c in "0123456789abcdef." for c in key)   # 网关字符集安全
    assert "/" not in key and ":" not in key and "%" not in key
    env.db.rev0_rows[aid] = [
        {"row_key": key, "data": {"路径": "旧"}, "provenance": [],
         "conflicts": {}, "ordinal": 1},
    ]
    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/{key}",
        json={"column": "路径", "value": "新"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["路径"] == "新"
    r = await env.client.request("DELETE", f"/api/artifacts/{aid}/table/rows/{key}")
    assert r.status_code == 200
    assert env.db.rev0_rows[aid] == []


async def test_cell_update_stale_token_maps_409(env):
    """codex三审 P2-1 回归（路由级）：单格更新带旧 token → 409
    code=stale_artifact；并发窗口（前置快检过后仓储层才判 stale）同样 409
    而非 500。"""
    aid = await _create_artifact(env)
    token = await _upload_a(env, aid)
    r = await env.client.post(
        f"/api/artifacts/{aid}/table/import",
        json={"preview_token": token, "plan": {"column_ops": [
            {"source": "MOP", "action": "new", "target_name": "MOP",
             "column_type": "text", "is_primary_key": True},
            {"source": "L1分类", "action": "new", "target_name": "L1分类",
             "column_type": "text"},
        ]}},
    )
    assert r.status_code == 200, r.text
    key = "4e433230323630363031303030323935"

    # 旧 token → 409 stale_artifact
    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/{key}",
        json={"column": "L1分类", "value": "x",
              "expected_updated_at": "2000-01-01T00:00:00Z"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"

    # 并发窗口模拟：token 当前（service 前置快检过）但仓储层抛 stale → 仍 409
    from knowledge_mining.mining.kb.db_artifacts import (
        ArtifactError as ArtifactRepoError,
    )

    async def raise_stale(*args, **kwargs):
        raise ArtifactRepoError("stale_artifact")

    env.db.update_cell = raise_stale
    fresh = env.db.artifacts[aid]["updated_at"].isoformat()
    r = await env.client.patch(
        f"/api/artifacts/{aid}/table/rows/{key}",
        json={"column": "L1分类", "value": "y", "expected_updated_at": fresh},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_artifact"
