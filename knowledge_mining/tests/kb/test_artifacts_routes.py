"""制品 REST 路由（61号 §十）—— httpx ASGI + fake service/db（不碰真库）。

dependency_overrides 注入真实 ArtifactService(FakeArtifactDB) 与真实导入服务，
打真实路由层：状态码映射（404/403/422/409/410/413）+ 向导全链（上传→预览→
导入→发布→查行）。
"""
from __future__ import annotations

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
    ArtifactImportService,
)
from knowledge_mining.mining.kb.services.artifact_service import ArtifactService
from knowledge_mining.tests.kb.test_artifacts_service import FakeArtifactDB, _user

pytestmark = pytest.mark.asyncio

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
    db.domain_bindings.add(("u-alice", DOMAIN))
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

    r = await env.client.patch(
        f"/api/artifacts/{aid}",
        json={"title": "MOP 表", "description": "割接前查责任域"},
    )
    assert r.status_code == 200 and r.json()["title"] == "MOP 表"

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


# ------------------------------------------------------------- 错误映射

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


async def test_preview_unknown_token_maps_410(env):
    aid = await _create_artifact(env)
    r = await env.client.get(f"/api/artifacts/{aid}/table/preview/deadbeef")
    assert r.status_code == 410


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
        f"/api/artifacts/{aid}/table/rows/NC20260601000295",
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
        "DELETE", f"/api/artifacts/{aid}/table/rows/NC20260601000302")
    assert r.status_code == 200
    r = await env.client.get(f"/api/artifacts/{aid}/table/rows?draft=1")
    assert r.json()["total"] == 7
