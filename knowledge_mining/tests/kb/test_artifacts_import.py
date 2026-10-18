"""Excel 三步导入向导（61号 §六）—— 纯内存可跑。

解析/推断/映射用真实实现（openpyxl + 临时文件）；ArtifactService/DB 用
test_artifacts_service 的 fake。覆盖：类型推断、建表导入、追加映射（列名
不同+新列+drop）、主键 UPSERT 与 provenance 双来源、文件内主键冲突两种
策略、公式注入/列数/行数护栏、tolerant/strict 类型错误。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from openpyxl import Workbook

from knowledge_mining.mining.kb.services.artifact_import_service import (
    MAX_UPLOAD_BYTES,
    PREVIEW_TTL_SECONDS,
    _PREVIEW_TOKENS,
    ArtifactImportError,
    ArtifactImportService,
)
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactService,
    InvalidOperation,
    PrimaryKeyConflict,
)
from knowledge_mining.tests.kb.test_artifacts_service import FakeArtifactDB, _user

pytestmark = pytest.mark.asyncio

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
B_HEADER = ["MOP编号", "L1分类", "L2分类", "是否夜间执行"]
B_ROWS = [
    ("NC20260601000295", "数据业务", "网络部署/扩容", "是"),
    ("NC20260601000297", "网络维护", "隐患整改", "否"),
    ("NC20260601000300", "云资源", "资源开通", "否"),
    ("NC20260601000401", "数据业务", "业务变更", "是"),
    ("NC20260601000402", "网络维护", "故障处置", "否"),
]
OWNER = _user("u-owner")


def _write_xlsx(path: Path, header: list[str], rows: list[tuple]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Sheet1")
    sheet.append(header)
    for row in rows:
        sheet.append(list(row))
    workbook.save(str(path))
    return path.read_bytes()


def _make_service() -> tuple[FakeArtifactDB, ArtifactService, ArtifactImportService]:
    db = FakeArtifactDB()
    # codex二审批号⑧：execute_import 经 list_columns/_require_design_session 走
    # _assert_role——OWNER 需 plant-a 域绑定。
    db.domain_bindings.add(("u-owner", "plant-a"))
    return db, ArtifactService(db), ArtifactImportService(db)


A_CREATE_OPS = [
    {"source": "MOP", "action": "new", "target_name": "MOP",
     "column_type": "text", "is_primary_key": True, "is_indexed": True},
    {"source": "L1分类", "action": "new", "target_name": "L1分类",
     "column_type": "enum", "enum_values": ["数据业务", "网络维护", "云资源"]},
    {"source": "L2分类", "action": "new", "target_name": "L2分类",
     "column_type": "text"},
]


async def _import_a(
    db: FakeArtifactDB, svc: ArtifactService, imp: ArtifactImportService,
    tmp_path: Path,
) -> tuple[str, str]:
    """建表模式导入 A.xlsx（MOP 主键）→ 返回 (artifact_id, preview_token)。"""
    del db
    art = await svc.create_artifact(user=OWNER, domain_id="plant-a", title="MOP 表")
    aid = art["artifact_id"]
    out = imp.parse_upload(_write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS), "A.xlsx")
    token = out["preview_token"]
    plan = imp.build_mapping(token, column_ops=A_CREATE_OPS)
    report = await imp.execute_import(
        svc, user=OWNER, artifact_id=aid, preview_token=token, plan=plan,
    )
    assert report["inserted"] == 8
    return aid, token


@pytest.fixture(autouse=True)
def _clear_previews():
    yield
    for record in _PREVIEW_TOKENS.values():
        try:
            os.unlink(record["path"])
        except OSError:
            pass
    _PREVIEW_TOKENS.clear()


# ---------------------------------------------------------------- 第一步：解析

async def test_parse_upload_infers_types_and_samples(tmp_path):
    _, _, imp = _make_service()
    data = _write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    out = imp.parse_upload(data, "A.xlsx")
    preview = out["preview"]
    assert preview["file_name"] == "A.xlsx"
    assert preview["sheets"] == ["Sheet1"]
    assert preview["default_sheet"] == "Sheet1"
    columns = {c["source_name"]: c for c in preview["columns"]}
    # L1分类：distinct 3 且非数值 → enum，候选=distinct 值
    assert columns["L1分类"]["inferred_type"] == "enum"
    assert set(columns["L1分类"]["enum_candidates"]) == {"数据业务", "网络维护", "云资源"}
    assert columns["MOP"]["inferred_type"] == "enum"  # 8 distinct ≤ 50 非数值
    assert len(preview["sample_rows"]) == 8
    assert preview["sample_rows"][0]["MOP"] == "NC20260601000295"
    assert out["preview_token"] in _PREVIEW_TOKENS


async def test_parse_upload_numeric_and_date_inference(tmp_path):
    _, _, imp = _make_service()
    header = ["编号", "数量", "单价", "日期"]
    rows = [("X1", 3, 1.5, "2026-06-01"), ("X2", 4, 2.5, "2026-06-02")]
    out = imp.parse_upload(_write_xlsx(tmp_path / "E.xlsx", header, rows), "E.xlsx")
    columns = {c["source_name"]: c for c in out["preview"]["columns"]}
    assert columns["数量"]["inferred_type"] == "int"
    assert columns["单价"]["inferred_type"] == "float"
    assert columns["日期"]["inferred_type"] == "date"
    # 空列 → text
    blank_file = _write_xlsx(tmp_path / "E2.xlsx", ["编号", "备注"], [("X1", None)])
    columns = {
        c["source_name"]: c
        for c in imp.parse_upload(blank_file, "E2.xlsx")["preview"]["columns"]
    }
    assert columns["备注"]["inferred_type"] == "text"


async def test_parse_upload_rejects_bad_files():
    _, _, imp = _make_service()
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(b"x" * (MAX_UPLOAD_BYTES + 1), "big.xlsx")
    assert excinfo.value.code == "file_too_large"
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(b"a,b\n1,2", "A.csv")
    assert excinfo.value.code == "unsupported_file_type"
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(b"not a zip at all", "A.xlsx")
    assert excinfo.value.code == "invalid_xlsx"


async def test_parse_upload_rejects_formula_injection(tmp_path):
    _, _, imp = _make_service()
    data = _write_xlsx(tmp_path / "F.xlsx", ["MOP"], [["=1+1"]])
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(data, "F.xlsx")
    assert excinfo.value.code == "formula_not_allowed"
    # 表头本身是公式同样拒绝
    data = _write_xlsx(tmp_path / "F2.xlsx", ["=SUM(A1)"], [["x"]])
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(data, "F2.xlsx")
    assert excinfo.value.code == "formula_not_allowed"


async def test_parse_upload_column_limit(tmp_path):
    _, _, imp = _make_service()
    header = [f"列{i}" for i in range(201)]
    data = _write_xlsx(tmp_path / "wide.xlsx", header, [("x",) * 201])
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(data, "wide.xlsx")
    assert excinfo.value.code == "too_many_columns"


async def test_parse_upload_row_limit(tmp_path, monkeypatch):
    import knowledge_mining.mining.kb.services.artifact_import_service as mod

    monkeypatch.setattr(mod, "MAX_ROWS", 2)
    _, _, imp = _make_service()
    data = _write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS)  # 8 行 > 2
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(data, "A.xlsx")
    assert excinfo.value.code == "too_many_rows"


# ------------------------------------------------------------- 第二步：映射

async def test_build_mapping_create_plan_and_validation(tmp_path):
    _, _, imp = _make_service()
    out = imp.parse_upload(_write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS), "A.xlsx")
    token = out["preview_token"]
    plan = imp.build_mapping(token, column_ops=A_CREATE_OPS)
    assert plan["mode"] == "create"
    assert plan["missing_columns"] == []
    assert [c["target_name"] for c in plan["columns"]] == ["MOP", "L1分类", "L2分类"]
    # source 名不存在于表头 → 拒绝
    with pytest.raises(InvalidOperation) as excinfo:
        imp.build_mapping(token, column_ops=[{**A_CREATE_OPS[0], "source": "不存在列"}])
    assert "unknown_source_column" in str(excinfo.value)
    # 非法列类型 → 拒绝
    with pytest.raises(InvalidOperation) as excinfo:
        imp.build_mapping(token, column_ops=[
            {"source": "MOP", "action": "new", "target_name": "MOP",
             "column_type": "decimal"}])
    assert "invalid_column_type" in str(excinfo.value)
    # 非法 sheet → 拒绝
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.build_mapping(token, sheet="NoSheet", column_ops=A_CREATE_OPS)
    assert excinfo.value.code == "unknown_sheet"


async def test_build_mapping_append_missing_columns(tmp_path):
    _, _, imp = _make_service()
    out = imp.parse_upload(_write_xlsx(tmp_path / "B.xlsx", B_HEADER, B_ROWS), "B.xlsx")
    target = [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_primary_key": True},
        {"column_id": "c002", "column_name": "L1分类", "column_type": "enum"},
        {"column_id": "c003", "column_name": "L2分类", "column_type": "text"},
    ]
    ops = [
        {"source": "MOP编号", "action": "map_to", "target": "c001"},
        {"source": "L1分类", "action": "map_to", "target": "c002"},
        {"source": "L2分类", "action": "drop"},
        {"source": "是否夜间执行", "action": "new", "target_name": "是否夜间执行",
         "column_type": "text"},
    ]
    plan = imp.build_mapping(out["preview_token"], column_ops=ops, target_columns=target)
    assert plan["mode"] == "append"
    # schema 有（c003 L2分类）而文件未映射 → missing_columns 提示
    assert [m["column_id"] for m in plan["missing_columns"]] == ["c003"]
    # map_to 未知目标列 → 拒绝
    with pytest.raises(InvalidOperation) as excinfo:
        imp.build_mapping(out["preview_token"], column_ops=[
            {"source": "MOP编号", "action": "map_to", "target": "c999"}],
            target_columns=target)
    assert "unknown_target_column" in str(excinfo.value)


# ------------------------------------------------------- 第三步：建表导入

async def test_execute_create_imports_rows_with_provenance(tmp_path):
    db, svc, imp = _make_service()
    aid, token = await _import_a(db, svc, imp, tmp_path)
    rows = db.rev0_rows[aid]
    assert len(rows) == 8
    first = rows[0]
    assert first["row_key"] == "16:NC20260601000295"  # 主键值长度前缀编码即 row_key
    assert first["data"]["L1分类"] == "数据业务"
    assert first["data"]["L2分类"] == "网络部署/扩容"
    provenance = first["provenance"][0]
    assert provenance["file_name"] == "A.xlsx"
    assert provenance["sheet_name"] == "Sheet1"
    assert provenance["row_index"] == 2  # Excel 物理行号（表头=第 1 行）
    assert provenance["batch_id"].startswith("bat_")
    columns = {c["column_name"]: c for c in db.columns[aid]}
    assert columns["MOP"]["is_primary_key"] is True
    assert columns["L1分类"]["column_type"] == "enum"
    assert db.artifacts[aid]["draft_open"] is True
    batches = db.batches[aid]
    assert len(batches) == 1
    assert batches[0]["channel"] == "upload"
    assert batches[0]["row_count"] == 8
    assert batches[0]["created_by"] == "u-owner"
    # 导入成功后预览 token 被消费
    assert token not in _PREVIEW_TOKENS
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.get_preview(token)
    assert excinfo.value.code == "preview_expired"


async def test_execute_create_blocked_when_schema_exists(tmp_path):
    db, svc, imp = _make_service()
    aid, _token = await _import_a(db, svc, imp, tmp_path)
    out = imp.parse_upload(_write_xlsx(tmp_path / "A2.xlsx", A_HEADER, A_ROWS), "A2.xlsx")
    plan = imp.build_mapping(out["preview_token"], column_ops=A_CREATE_OPS)
    with pytest.raises(InvalidOperation) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id=aid,
            preview_token=out["preview_token"], plan=plan,
        )
    assert "schema_already_exists" in str(excinfo.value)


async def test_execute_append_blocked_without_schema(tmp_path):
    db, svc, imp = _make_service()
    art = await svc.create_artifact(user=OWNER, domain_id="plant-a", title="空制品")
    out = imp.parse_upload(_write_xlsx(tmp_path / "B.xlsx", B_HEADER, B_ROWS), "B.xlsx")
    # 手搓 map_to 计划（build_mapping 在无目标列时无法构造 append 计划）
    plan = {"sheet": "Sheet1", "header_row": 1, "columns": [
        {"source": "MOP编号", "action": "map_to", "target_column_id": "c001"},
    ]}
    with pytest.raises(InvalidOperation) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id=art["artifact_id"],
            preview_token=out["preview_token"], plan=plan,
        )
    assert "no_schema" in str(excinfo.value)


# ------------------------------------------------------- 第三步：追加导入

async def test_execute_append_maps_updates_and_drops(tmp_path):
    db, svc, imp = _make_service()
    aid, _token = await _import_a(db, svc, imp, tmp_path)
    column_ids = {c["column_name"]: c["column_id"] for c in db.columns[aid]}
    out_b = imp.parse_upload(
        _write_xlsx(tmp_path / "B.xlsx", B_HEADER, B_ROWS), "B.xlsx")
    ops = [
        {"source": "MOP编号", "action": "map_to", "target": column_ids["MOP"]},
        {"source": "L1分类", "action": "map_to", "target": column_ids["L1分类"]},
        {"source": "L2分类", "action": "drop"},
        {"source": "是否夜间执行", "action": "new", "target_name": "是否夜间执行",
         "column_type": "text"},
    ]
    plan = imp.build_mapping(
        out_b["preview_token"], column_ops=ops,
        target_columns=await svc.list_columns(user=OWNER, artifact_id=aid),
    )
    report = await imp.execute_import(
        svc, user=OWNER, artifact_id=aid,
        preview_token=out_b["preview_token"], plan=plan,
        conflict_policy={"type_error": "tolerant"},
    )
    # B：3 行同 MOP 更新 + 2 行新 MOP 插入
    assert report["inserted"] == 2
    assert report["updated"] == 3
    assert [m["column_name"] for m in report["missing_columns"]] == ["L2分类"]
    rows = {r["row_key"]: r for r in db.rev0_rows[aid]}
    # 同 MOP 行：data 覆盖 + provenance 双来源（A/B 导入同一编码 → 同键 UPSERT 命中）
    shared = rows["16:NC20260601000295"]
    assert shared["data"]["是否夜间执行"] == "是"
    assert len(shared["provenance"]) == 2
    assert {p["file_name"] for p in shared["provenance"]} == {"A.xlsx", "B.xlsx"}
    # drop 列不进 data：新行无该键；被本批覆盖的行该列置 null（§6.2
    # 「schema 有文件无的列 → 该批行此列置 null」——UPSERT data 覆盖语义）
    new_row = rows["16:NC20260601000401"]
    assert "L2分类" not in new_row["data"]
    assert shared["data"].get("L2分类") is None
    # 新列已入 schema
    assert any(c["column_name"] == "是否夜间执行" for c in db.columns[aid])
    assert len(db.batches[aid]) == 2  # A + B 两个批次


async def test_key_conflict_block_then_keep_first(tmp_path):
    db, svc, imp = _make_service()
    aid, _token = await _import_a(db, svc, imp, tmp_path)
    column_ids = {c["column_name"]: c["column_id"] for c in db.columns[aid]}
    header = ["MOP编号", "L1分类"]
    rows = [
        ("NC20260601000295", "变更A"),
        ("NC20260601000303", "新行1"),
        ("NC20260601000303", "新行2"),  # 文件内主键重复
    ]
    out = imp.parse_upload(_write_xlsx(tmp_path / "C.xlsx", header, rows), "C.xlsx")
    ops = [
        {"source": "MOP编号", "action": "map_to", "target": column_ids["MOP"]},
        {"source": "L1分类", "action": "map_to", "target": column_ids["L1分类"]},
    ]
    plan = imp.build_mapping(
        out["preview_token"], column_ops=ops,
        target_columns=await svc.list_columns(user=OWNER, artifact_id=aid),
    )
    # block（默认）：整体报错，不落任何行
    before = len(db.rev0_rows[aid])
    with pytest.raises(PrimaryKeyConflict) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id=aid,
            preview_token=out["preview_token"], plan=plan,
        )
    assert "duplicate_key" in str(excinfo.value)
    assert len(db.rev0_rows[aid]) == before
    # block 报错不消费 token → 换 keep_first 策略重试
    report = await imp.execute_import(
        svc, user=OWNER, artifact_id=aid,
        preview_token=out["preview_token"], plan=plan,
        conflict_policy={"key_conflict": "keep_first"},
    )
    assert report["key_conflicts"] == 1
    assert report["inserted"] == 1 and report["updated"] == 1
    after = {r["row_key"]: r for r in db.rev0_rows[aid]}
    assert after["16:NC20260601000303"]["data"]["L1分类"] == "新行1"  # 保留首行
    assert "__duplicate_key__" in after["16:NC20260601000303"]["conflicts"]
    assert after["16:NC20260601000295"]["data"]["L1分类"] == "变更A"


# ------------------------------------------------------------- 类型策略

async def test_type_error_strict_blocks_and_tolerant_marks(tmp_path):
    db, svc, imp = _make_service()
    header = ["编号", "数量"]
    rows = [("X1", 3), ("X2", "abc"), ("X3", 5)]
    ops = [
        {"source": "编号", "action": "new", "target_name": "编号",
         "column_type": "text", "is_primary_key": True},
        {"source": "数量", "action": "new", "target_name": "数量",
         "column_type": "int"},
    ]
    # strict（默认）：全量校验后报错，不落任何行、不建批次
    out = imp.parse_upload(_write_xlsx(tmp_path / "D.xlsx", header, rows), "D.xlsx")
    plan = imp.build_mapping(out["preview_token"], column_ops=ops)
    art = await svc.create_artifact(user=OWNER, domain_id="plant-a", title="数值表")
    aid = art["artifact_id"]
    with pytest.raises(PrimaryKeyConflict) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id=aid,
            preview_token=out["preview_token"], plan=plan,
        )
    assert "type_error" in str(excinfo.value)
    assert db.rev0_rows[aid] == []
    assert db.batches.get(aid, []) == []
    # tolerant：失败格置 null + conflicts 标记
    report = await imp.execute_import(
        svc, user=OWNER, artifact_id=aid,
        preview_token=out["preview_token"], plan=plan,
        conflict_policy={"type_error": "tolerant"},
    )
    assert report["type_errors"] == 1
    assert report["inserted"] == 3
    row_map = {r["row_key"]: r for r in db.rev0_rows[aid]}
    assert row_map["2:X2"]["data"]["数量"] is None
    assert row_map["2:X2"]["conflicts"]["数量"] == "type_error:abc"
    assert row_map["2:X1"]["data"]["数量"] == 3


# ------------------------------------------------------------- 预览 TTL

async def test_preview_ttl_expiry_and_lazy_cleanup(tmp_path):
    _, _, imp = _make_service()
    data = _write_xlsx(tmp_path / "E.xlsx", ["编号"], [("X1",)])
    out = imp.parse_upload(data, "E.xlsx")
    token = out["preview_token"]
    record = _PREVIEW_TOKENS[token]
    record["parsed_at"] -= PREVIEW_TTL_SECONDS + 1
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.get_preview(token)
    assert excinfo.value.code == "preview_expired"
    # 下一次调用顺带清理过期登记
    imp.parse_upload(data, "E2.xlsx")
    assert token not in _PREVIEW_TOKENS
    assert not os.path.exists(record["path"])


async def test_execute_import_requires_valid_preview(tmp_path):
    db, svc, imp = _make_service()
    with pytest.raises(ArtifactImportError) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id="art_x",
            preview_token="deadbeef", plan={"columns": []},
        )
    assert excinfo.value.code == "preview_expired"


async def test_execute_import_validates_conflict_policy(tmp_path):
    _, svc, imp = _make_service()
    data = _write_xlsx(tmp_path / "E.xlsx", ["编号"], [("X1",)])
    out = imp.parse_upload(data, "E.xlsx")
    plan = imp.build_mapping(
        out["preview_token"],
        column_ops=[{"source": "编号", "action": "new", "target_name": "编号",
                     "column_type": "text"}],
    )
    with pytest.raises(InvalidOperation) as excinfo:
        await imp.execute_import(
            svc, user=OWNER, artifact_id="art_x",
            preview_token=out["preview_token"], plan=plan,
            conflict_policy={"type_error": "whatever"},
        )
    assert "invalid_conflict_policy" in str(excinfo.value)


# ------------------------------------------------- 对抗审查修复回归（61号）

async def test_execute_append_ordinal_continues_after_existing(tmp_path):
    """修复⑧回归：追加导入 prepared 行 ordinal 起始 = 现有 rev0 行数 + 1
    （新行接在存量之后，不再与已有行挤 1..N）。"""
    db, svc, imp = _make_service()
    aid, _token = await _import_a(db, svc, imp, tmp_path)   # 8 行，ordinal 1..8
    column_ids = {c["column_name"]: c["column_id"] for c in db.columns[aid]}
    out_b = imp.parse_upload(
        _write_xlsx(tmp_path / "B.xlsx", B_HEADER, B_ROWS), "B.xlsx")
    ops = [
        {"source": "MOP编号", "action": "map_to", "target": column_ids["MOP"]},
        {"source": "L1分类", "action": "map_to", "target": column_ids["L1分类"]},
        {"source": "L2分类", "action": "drop"},
    ]
    plan = imp.build_mapping(
        out_b["preview_token"], column_ops=ops,
        target_columns=await svc.list_columns(user=OWNER, artifact_id=aid),
    )
    await imp.execute_import(
        svc, user=OWNER, artifact_id=aid,
        preview_token=out_b["preview_token"], plan=plan,
    )
    rows = {r["row_key"]: r for r in db.rev0_rows[aid]}
    # B.xlsx 新 MOP 两行（文件内第 4/5 数据行）→ ordinal 12/13（8 存量 + 前 3 行）
    assert rows["16:NC20260601000401"]["ordinal"] == 12
    assert rows["16:NC20260601000402"]["ordinal"] == 13


async def test_preview_token_binding_mismatch_404(tmp_path):
    """修复⑬服务级：绑定登记（artifact_id+uploader）的 token 用于他制品/
    他用户 → ArtifactNotFound；正确绑定放行；未绑定登记（直调形态）不设卡。"""
    from knowledge_mining.mining.kb.services.artifact_service import ArtifactNotFound

    _, svc, imp = _make_service()
    art = await svc.create_artifact(user=OWNER, domain_id="plant-a", title="绑定")
    data = _write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    out = imp.parse_upload(
        data, "A.xlsx", artifact_id=art["artifact_id"], uploader="u-owner")
    token = out["preview_token"]

    # 正确绑定 → 放行
    preview = imp.get_preview(
        token, artifact_id=art["artifact_id"], uploader="u-owner")
    assert preview["preview"]["file_name"] == "A.xlsx"
    # 他制品 / 他用户 → 404
    with pytest.raises(ArtifactNotFound):
        imp.get_preview(token, artifact_id="art_other", uploader="u-owner")
    with pytest.raises(ArtifactNotFound):
        imp.get_preview(
            token, artifact_id=art["artifact_id"], uploader="u-owner2")
    with pytest.raises(ArtifactNotFound):
        imp.build_mapping(
            token, column_ops=A_CREATE_OPS,
            artifact_id="art_other", uploader="u-owner")
    # 未绑定登记（不传 kwargs）不设卡——服务层直调形态
    unbound = imp.parse_upload(data, "A2.xlsx")
    assert imp.get_preview(unbound["preview_token"])["preview"]["file_name"] == "A2.xlsx"


async def test_preview_cap_per_uploader(tmp_path):
    """修复⑭服务级：每 uploader 活跃登记 ≤5——第 6 个清最旧 + preview_limit
    （重试即可成功）；另一 uploader 不受影响。"""
    data = _write_xlsx(tmp_path / "A.xlsx", A_HEADER, A_ROWS)
    _, _, imp = _make_service()
    tokens = [
        imp.parse_upload(
            data, f"{i}.xlsx", artifact_id="art_x", uploader="u1",
        )["preview_token"]
        for i in range(5)
    ]
    oldest_path = _PREVIEW_TOKENS[tokens[0]]["path"]
    with pytest.raises(ArtifactImportError) as excinfo:
        imp.parse_upload(data, "5.xlsx", artifact_id="art_x", uploader="u1")
    assert excinfo.value.code == "preview_limit"
    assert tokens[0] not in _PREVIEW_TOKENS        # 最旧被清
    assert not os.path.exists(oldest_path)          # 临时文件一并清理
    again = imp.parse_upload(data, "5b.xlsx", artifact_id="art_x", uploader="u1")
    assert again["preview_token"] in _PREVIEW_TOKENS   # 立即重试成功
    other = imp.parse_upload(data, "x.xlsx", artifact_id="art_x", uploader="u2")
    assert other["preview_token"] in _PREVIEW_TOKENS   # 他 uploader 独立计数
