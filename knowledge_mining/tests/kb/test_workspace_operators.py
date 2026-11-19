# -*- coding: utf-8 -*-
"""工作台数据集契约 + 算子注册表 + 4 确定性算子测试（67号工作台 MVP Task 4）。

纯函数测试，无需真库（不带 postgres 标记，宿主直接 pytest 可跑）。

材料 = 64号 B.3 固定验收材料（模块级 fixture，用例拿深拷贝防串改）：
- 产品表 P1/3000W、P2/2000W、P3/1200W（型号/部件编码/功率_W/电源类型）
- 库存表 P1/5、P3/0（型号/可用库存）
铁律断言：功率 ≥2000 → 2 行（P1、P2）；left 关联 → 3 行（P2 库存空）；
inner 关联 → 2 行；右表重复 P1 → 关联必须失败且不产出任何输出。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

import pytest

from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceValidationError,
    _ensure_acyclic,
)
from knowledge_mining.mining.kb.workspace_dataset import (
    MAX_ROWS,
    MAX_SERIALIZED_BYTES,
    TRIAL_ROWS,
    dataset_hash,
    merge_quality,
    serialized_size_ok,
    slice_for_trial,
    validate_dataset,
)
from knowledge_mining.mining.kb.workspace_operators import (
    OPERATOR_DEFS,
    PortSpec,
    run_operator,
    topological_order,
)

_PRODUCT_SOURCE = "产品表.xlsx/Sheet1"
_INVENTORY_SOURCE = "库存表.xlsx/Sheet1"

#: 产品表（B.3）：P1/3000W、P2/2000W、P3/1200W。
_PRODUCTS = {
    "kind": "table",
    "cols": ["型号", "部件编码", "功率_W", "电源类型"],
    "rows": [
        {"_id": "p1", "_source": _PRODUCT_SOURCE, "型号": "P1",
         "部件编码": "PRD-0001", "功率_W": 3000, "电源类型": "AC"},
        {"_id": "p2", "_source": _PRODUCT_SOURCE, "型号": "P2",
         "部件编码": "PRD-0002", "功率_W": 2000, "电源类型": "AC"},
        {"_id": "p3", "_source": _PRODUCT_SOURCE, "型号": "P3",
         "部件编码": "PRD-0003", "功率_W": 1200, "电源类型": "DC"},
    ],
}

#: 库存表（B.3）：P1/5、P3/0。
_INVENTORY = {
    "kind": "table",
    "cols": ["型号", "可用库存"],
    "rows": [
        {"_id": "i1", "_source": _INVENTORY_SOURCE, "型号": "P1", "可用库存": 5},
        {"_id": "i2", "_source": _INVENTORY_SOURCE, "型号": "P3", "可用库存": 0},
    ],
}

#: 右键重复负例材料（B.3 铁律）：P1 在库存表出现两次。
_INVENTORY_DUP = {
    "kind": "table",
    "cols": ["型号", "可用库存"],
    "rows": [
        {"_id": "i1", "_source": _INVENTORY_SOURCE, "型号": "P1", "可用库存": 5},
        {"_id": "i1b", "_source": _INVENTORY_SOURCE, "型号": "P1", "可用库存": 2},
        {"_id": "i2", "_source": _INVENTORY_SOURCE, "型号": "P3", "可用库存": 0},
    ],
}

#: 文档底表（split_sections 用，形态对齐 Task 5 kb_dir 产出）。
_DOCS = {
    "kind": "table",
    "cols": ["文件", "文档内容_JSON"],
    "rows": [
        {"_id": "doc-1", "_source": "kb1/方案/P1产品资料.md",
         "文件": "P1产品资料.md",
         "文档内容_JSON": {"sections": [
             {"level": 1, "title": "基本属性", "text": "型号P1，功率3000W"},
             {"level": 1, "title": "技术规格", "text": "电源AC，部件PRD-0001"},
             {"level": 2, "title": "配套关系", "text": "配套P3"},
         ]}},
        {"_id": "doc-2", "_source": "kb1/方案/P3产品资料.md",
         "文件": "P3产品资料.md",
         "文档内容_JSON": {"sections": [
             {"level": 2, "title": "备注", "text": "仅二级章节"},
         ]}},
    ],
}


@pytest.fixture
def products() -> dict:
    return copy.deepcopy(_PRODUCTS)


@pytest.fixture
def inventory() -> dict:
    return copy.deepcopy(_INVENTORY)


@pytest.fixture
def inventory_dup() -> dict:
    return copy.deepcopy(_INVENTORY_DUP)


@pytest.fixture
def docs() -> dict:
    return copy.deepcopy(_DOCS)


# ------------------------------------------------------------ 数据集契约


def test_validate_dataset_accepts_b3_material(products, inventory) -> None:
    validate_dataset(products)
    validate_dataset(inventory)


def test_validate_dataset_allows_none_values_and_partial_cols() -> None:
    """None 值合法；行字段键是 cols 的子集也合法（缺列≠多余列）。"""
    ds = {"kind": "table", "cols": ["型号", "功率_W", "备注"],
          "rows": [{"_id": "r1", "_source": "s", "型号": "P1",
                    "功率_W": None}]}
    validate_dataset(ds)


def test_validate_dataset_kind_defaults_to_table() -> None:
    """缺 kind 视为 table（LOW 钉测）：Task 3 时代数据集不带 kind，兼容读；
    显式 table 同过，documents/relations 预留形态照拒（见拒绝参数化）。"""
    ds = {"cols": ["型号"], "rows": [{"_id": "r1", "_source": "s",
                                      "型号": "P1"}]}
    validate_dataset(ds)
    ds["kind"] = "table"
    validate_dataset(ds)


@pytest.mark.parametrize("mutate", [
    lambda ds: ds.update(kind="documents"),
    lambda ds: ds.update(cols=[]),
    lambda ds: ds.update(cols=["型号", "型号", "功率_W"]),
    lambda ds: ds.update(cols=["型号", ""]),
    lambda ds: ds.update(cols="型号"),
    lambda ds: ds.pop("cols"),
    lambda ds: ds.update(rows="x"),
    lambda ds: ds.pop("rows"),
    lambda ds: ds["rows"].append("不是dict"),
    lambda ds: ds["rows"][0].pop("_id"),
    lambda ds: ds["rows"][0].update(_id=""),
    lambda ds: ds["rows"][0].update(_id=1),
    lambda ds: ds["rows"][0].pop("_source"),
    lambda ds: ds["rows"][0].update(_source=5),
    lambda ds: ds["rows"][0].update(幽灵列="越界字段"),
    lambda ds: ds.update(
        rows=[{"_id": f"r{i:05d}", "_source": "s"} for i in range(MAX_ROWS + 1)]),
], ids=["kind非table", "cols空", "cols重复", "cols空串名", "cols非列表",
        "cols缺失", "rows非列表", "rows缺失", "行非dict", "_id缺失",
        "_id空串", "_id非str", "_source缺失", "_source非str", "多余字段",
        "rows超上限"])
def test_validate_dataset_rejects(mutate, products) -> None:
    mutate(products)
    with pytest.raises(WorkspaceValidationError):
        validate_dataset(products)


def test_validate_dataset_rejects_non_dict() -> None:
    with pytest.raises(WorkspaceValidationError):
        validate_dataset(["不是对象"])


def test_dataset_hash_canonical_sha256(products) -> None:
    """canonical json（ensure_ascii=False / sort_keys / 紧凑分隔符）的
    sha256 hex——测试用同参数手工重算，钉死序列化口径。"""
    expected = hashlib.sha256(json.dumps(
        products, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    assert dataset_hash(products) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", dataset_hash(products))


def test_dataset_hash_stable_and_sensitive(products) -> None:
    h0 = dataset_hash(products)
    assert dataset_hash(products) == h0  # 同内容稳定

    # 行内键序无关（sort_keys 归一），值/来源/行数/列序敏感
    reordered = {"kind": "table", "cols": ["型号", "部件编码", "功率_W", "电源类型"],
                 "rows": [dict(reversed(list(row.items())))
                          for row in products["rows"]]}
    assert dataset_hash(reordered) == h0

    def _variant(mutate) -> str:
        ds = copy.deepcopy(products)
        mutate(ds)
        return dataset_hash(ds)

    assert _variant(lambda ds: ds["rows"][0].update(型号="P1X")) != h0
    assert _variant(lambda ds: ds["rows"][0].update(_source="他处")) != h0
    assert _variant(lambda ds: ds["rows"].pop()) != h0
    assert _variant(lambda ds: ds.update(cols=list(reversed(ds["cols"])))) != h0


def test_serialized_size_ok_boundary() -> None:
    assert serialized_size_ok(_PRODUCTS) is True

    base = {"kind": "table", "cols": ["f"],
            "rows": [{"_id": "r", "_source": "s", "f": ""}]}
    overhead = len(json.dumps(base, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8"))
    filler = MAX_SERIALIZED_BYTES - overhead  # 恰好补到预算上限
    at_limit = copy.deepcopy(base)
    at_limit["rows"][0]["f"] = "x" * filler
    assert serialized_size_ok(at_limit) is True

    over = copy.deepcopy(base)
    over["rows"][0]["f"] = "x" * (filler + 1)
    assert serialized_size_ok(over) is False


def test_slice_for_trial(products) -> None:
    assert TRIAL_ROWS == 3
    out = slice_for_trial(products)
    assert [r["_id"] for r in out["rows"]] == ["p1", "p2", "p3"]
    assert out["cols"] == products["cols"]

    # 深拷贝：改切片不动原件
    out["rows"][0]["型号"] = "被改"
    out["cols"].append("新列")
    assert products["rows"][0]["型号"] == "P1"
    assert products["cols"] == ["型号", "部件编码", "功率_W", "电源类型"]

    big = {"kind": "table", "cols": ["c"],
           "rows": [{"_id": f"r{i}", "_source": "s", "c": i} for i in range(5)]}
    assert [r["_id"] for r in slice_for_trial(big, 2)["rows"]] == ["r0", "r1"]
    assert [r["_id"] for r in slice_for_trial(big, 9)["rows"]] == \
        [f"r{i}" for i in range(5)]  # n 超行数=全取
    assert slice_for_trial(big, 0)["rows"] == []


def test_merge_quality_rules() -> None:
    assert merge_quality() == {}
    # 数值求和（跨任意份报告）
    assert merge_quality({"a": 1, "b": 2}, {"a": 3}, {"a": 4}) == \
        {"a": 8, "b": 2}
    # 列表拼接
    assert merge_quality({"l": [1]}, {"l": [2, 3]}) == {"l": [1, 2, 3]}
    # 标量覆盖：后者胜
    assert merge_quality({"s": "x", "keep": 1}, {"s": "y"}) == \
        {"s": "y", "keep": 1}
    assert merge_quality({"s": "x"}, {"s": "y"}, {"s": "z"}) == {"s": "z"}
    # 异型（数值→字符串）：不再求和，后者胜
    assert merge_quality({"a": 1}, {"a": "z"}) == {"a": "z"}
    assert merge_quality({"a": "z"}, {"a": 1}) == {"a": 1}
    # None 跳过（值级与整份报告级）
    assert merge_quality({"n": None}) == {}
    assert merge_quality({"n": None}, {"n": 5}) == {"n": 5}
    assert merge_quality(None, {"m": 1}, None) == {"m": 1}


# ------------------------------------------------------------ 注册表与 run_operator


def test_operator_registry_closed_shape() -> None:
    """封闭注册表：恰好 4 确定性算子 + 1 llm 算子（Task 7：kind=llm、
    run=None——执行分派 workspace_llm_operator，不走同步 run_operator）；
    条目字段齐全可被前端表单消费。"""
    assert set(OPERATOR_DEFS) == {"select", "filter", "join",
                                  "split_sections", "llm_extract"}
    for op, defn in OPERATOR_DEFS.items():
        assert defn["op"] == op
        assert isinstance(defn["name"], str) and defn["name"]
        assert isinstance(defn["input_arity"], int)
        assert isinstance(defn["config_schema"], dict)
        assert defn["ports"] and all(
            isinstance(p, PortSpec) for p in defn["ports"])
        port_ids = [p.id for p in defn["ports"]]
        assert len(port_ids) == len(set(port_ids))
        assert all(p.kind in ("table", "documents", "relations")
                   for p in defn["ports"])
        if defn["kind"] == "llm":
            assert defn["run"] is None
            continue
        assert defn["kind"] == "deterministic"
        assert callable(defn["run"])
    assert OPERATOR_DEFS["join"]["input_arity"] == 2
    for op in ("select", "filter", "split_sections", "llm_extract"):
        assert OPERATOR_DEFS[op]["input_arity"] == 1


@pytest.mark.parametrize("op, config, inputs", [
    ("drop_table", {}, [_PRODUCTS]),                       # 未注册算子
    ("select", {}, [_PRODUCTS, _PRODUCTS]),                # arity 多给
    ("join", {"method": "left", "left_key": "型号",
              "right_key": "型号"}, [_PRODUCTS]),          # arity 少给
    ("filter", {"field": "型号", "cond": "eq"}, "不是列表"),  # inputs 非列表
    ("filter", {"field": "型号", "cond": "eq", "value": "P1"},
     [{"kind": "table", "cols": ["型号"],
       "rows": [{"_id": "r", "_source": "s", "型号": "P1", "越界": 1}]}]),
    ("filter", "不是对象", [_PRODUCTS]),                    # config 非 dict
    ("filter", {"field": "型号"}, [_PRODUCTS]),             # 缺必填 cond
    ("filter", {"field": 5, "cond": "eq", "value": "P1"}, [_PRODUCTS]),
    ("filter", {"field": "型号", "cond": "between",
                "value": 1}, [_PRODUCTS]),                 # cond 词表外
    ("join", {"method": "outer", "left_key": "型号",
              "right_key": "型号"}, [_PRODUCTS, _INVENTORY]),
    ("split_sections", {"level": 3}, [_DOCS]),             # level 词表外
    ("split_sections", {"level": "1"}, [_DOCS]),           # level 非整数
    ("split_sections", {"level": True}, [_DOCS]),          # bool 不是 integer
], ids=["未注册", "arity多给", "arity少给", "inputs非列表", "输入越界字段",
        "config非dict", "缺必填", "类型不符", "cond词表外", "method词表外",
        "level词表外", "level非整数", "level布尔"])
def test_run_operator_gatekeeping(op, config, inputs) -> None:
    with pytest.raises(WorkspaceValidationError):
        run_operator(op, config, inputs)


def test_run_operator_validates_operator_outputs() -> None:
    """算子自产输出也要过 validate_dataset（注册表防线，坏算子不外漏）。"""
    def _broken_run(config, inputs):
        return ({"main": {"kind": "table", "cols": ["a"],
                          "rows": [{"_id": "1", "_source": "s",
                                    "a": 1, "ghost": 2}]}}, {})

    OPERATOR_DEFS["__broken__"] = {
        "op": "__broken__", "name": "坏算子", "input_arity": 1,
        "kind": "deterministic",
        "ports": [PortSpec("main", "主输出", "table")],
        "config_schema": {}, "run": _broken_run,
    }
    try:
        with pytest.raises(WorkspaceValidationError):
            run_operator("__broken__", {}, [_PRODUCTS])
    finally:
        del OPERATOR_DEFS["__broken__"]


def test_run_operator_allows_unknown_config_keys(products) -> None:
    """未知 config 键放行（LOW 钉测）：前端表单向前兼容——只校验 schema
    声明过的字段（缺必填/类型不符/enum 词表外），未声明键原样透传不算错。"""
    outputs, quality = run_operator(
        "filter", {"field": "型号", "cond": "eq", "value": "P1",
                   "client_hint": "表单折叠态"}, [products])
    assert [r["_id"] for r in outputs["main"]["rows"]] == ["p1"]
    assert quality == {"input_rows": 3, "filtered": 2}


# ------------------------------------------------------------ topological_order


def _nodes(*specs: tuple[str, tuple[str, ...]]) -> list[dict]:
    return [{"id": nid, "inputs": [f"{p}:main" for p in deps]}
            for nid, deps in specs]


def test_topological_order_linear_and_diamond() -> None:
    # B←A 给成乱序：输出仍按依赖序
    assert topological_order(_nodes(("B", ("A",)), ("A", ()))) == ["A", "B"]
    # 菱形 A→B/C→D：A 先行、D 殿后，B/C 保插入序
    assert topological_order(_nodes(
        ("A", ()), ("B", ("A",)), ("C", ("A",)), ("D", ("B", "C")),
    )) == ["A", "B", "C", "D"]
    assert topological_order([]) == []


def test_topological_order_rejects_cycle() -> None:
    with pytest.raises(WorkspaceValidationError):
        topological_order(_nodes(("A", ("B",)), ("B", ("A",))))
    with pytest.raises(WorkspaceValidationError):  # 自环
        topological_order(_nodes(("A", ("A",))))


@pytest.mark.parametrize("nodes", [
    "不是列表",
    ["不是对象"],
    [{"inputs": []}],                                # 缺 id
    [{"id": "", "inputs": []}],                      # 空 id
    [{"id": "A", "inputs": []}, {"id": "A", "inputs": []}],  # 重复 id
    [{"id": "A", "inputs": ["幽灵:main"]}],          # 引用缺失节点
    [{"id": "A", "inputs": "不是列表"}],
    [{"id": "A", "inputs": ["缺冒号"]}],
], ids=["非列表", "节点非dict", "缺id", "空id", "重复id", "悬空引用",
        "inputs非列表", "引用缺冒号"])
def test_topological_order_rejects_bad_shape(nodes) -> None:
    with pytest.raises(WorkspaceValidationError):
        topological_order(nodes)


def test_topological_order_aligned_with_ensure_acyclic() -> None:
    """与 db_workspaces._ensure_acyclic 同语义：无环图两判都过，有环图两判
    都拒（登记层 graph PUT 与执行器共用同一拓扑口径）。"""
    ok = _nodes(("A", ()), ("B", ("A",)), ("C", ("A",)), ("D", ("B", "C")))
    _ensure_acyclic(ok)
    assert set(topological_order(ok)) == {"A", "B", "C", "D"}

    cyclic = _nodes(("A", ("B",)), ("B", ("A",)))
    with pytest.raises(WorkspaceValidationError):
        _ensure_acyclic(cyclic)
    with pytest.raises(WorkspaceValidationError):
        topological_order(cyclic)


# ------------------------------------------------------------ select


def test_select_b3_rename_and_reorder(products) -> None:
    outputs, quality = run_operator(
        "select",
        {"columns": [{"id": "功率_W", "name": "功率"}, {"id": "型号"}]},
        [products])
    assert list(outputs) == ["main"]
    assert quality == {}
    ds = outputs["main"]
    assert ds["kind"] == "table"
    assert ds["cols"] == ["功率", "型号"]  # 列表顺序即输出列序
    assert [r["_id"] for r in ds["rows"]] == ["p1", "p2", "p3"]
    assert [r["_source"] for r in ds["rows"]] == [_PRODUCT_SOURCE] * 3
    assert [r["功率"] for r in ds["rows"]] == [3000, 2000, 1200]
    assert [r["型号"] for r in ds["rows"]] == ["P1", "P2", "P3"]


def test_select_passthrough_all_columns(products) -> None:
    outputs, _ = run_operator(
        "select",
        {"columns": [{"id": c} for c in products["cols"]]}, [products])
    ds = outputs["main"]
    assert ds["cols"] == products["cols"]
    assert ds["rows"] == products["rows"]


def test_select_missing_value_becomes_none() -> None:
    ds = {"kind": "table", "cols": ["型号", "电源类型"],
          "rows": [{"_id": "r1", "_source": "s", "型号": "P1",
                    "电源类型": "AC"},
                   {"_id": "r2", "_source": "s", "型号": "P2"}]}
    outputs, _ = run_operator(
        "select", {"columns": [{"id": "电源类型"}]}, [ds])
    out = outputs["main"]
    assert out["cols"] == ["电源类型"]
    assert [r["电源类型"] for r in out["rows"]] == ["AC", None]


@pytest.mark.parametrize("config", [
    {"columns": []},
    {"columns": "型号"},
    {"columns": ["不是对象"]},
    {"columns": [{"name": "X"}]},
    {"columns": [{"id": ""}]},
    {"columns": [{"id": "幽灵列"}]},
    {"columns": [{"id": "型号", "name": "X"}, {"id": "部件编码", "name": "X"}]},
    {"columns": [{"id": "型号"}, {"id": "型号"}]},
    {"columns": [{"id": "型号", "name": "_id"}]},
    {"columns": [{"id": "型号", "name": "_source"}]},
    {"columns": [{"id": "型号", "name": ""}]},
], ids=["空列清单", "columns非列表", "列项非对象", "缺id", "id空串",
        "未知列", "改名目标重复", "同列重复选择", "目标撞_id",
        "目标撞_source", "新名空串"])
def test_select_rejections(config, products) -> None:
    with pytest.raises(WorkspaceValidationError):
        run_operator("select", config, [products])


# ------------------------------------------------------------ filter


def test_filter_b3_power_gte_2000(products) -> None:
    """B.3 铁律断言：功率_W ≥2000 → 恰 2 行（P1、P2）。"""
    outputs, quality = run_operator(
        "filter", {"field": "功率_W", "cond": "gte", "value": 2000},
        [products])
    ds = outputs["main"]
    assert [r["型号"] for r in ds["rows"]] == ["P1", "P2"]
    assert [r["_id"] for r in ds["rows"]] == ["p1", "p2"]  # 命中行身份保留
    assert [r["_source"] for r in ds["rows"]] == [_PRODUCT_SOURCE] * 2
    assert ds["cols"] == products["cols"]
    assert quality == {"input_rows": 3, "filtered": 1}


@pytest.mark.parametrize("field, cond, value, expect", [
    ("型号", "eq", "P1", ["P1"]),
    ("型号", "ne", "P1", ["P2", "P3"]),
    ("电源类型", "ne", "DC", ["P1", "P2"]),
    ("功率_W", "gt", 1200, ["P1", "P2"]),
    ("功率_W", "lt", 2000, ["P3"]),
    ("功率_W", "lte", 2000, ["P2", "P3"]),
    ("功率_W", "gte", "2000", ["P1", "P2"]),      # value 数值字符串可转
    ("电源类型", "contains", "A", ["P1", "P2"]),
    ("型号", "contains", "P", ["P1", "P2", "P3"]),
    ("可用库存", "is_empty", None, []),            # 0 是值不是空
    ("可用库存", "not_empty", None, ["P1", "P3"]),
], ids=["eq", "ne", "ne文本", "gt", "lt", "lte", "gte数值串",
        "contains", "contains全中", "is_empty零是值", "not_empty"])
def test_filter_conditions(field, cond, value, expect) -> None:
    ds = _INVENTORY if field == "可用库存" else _PRODUCTS
    outputs, quality = run_operator(
        "filter", {"field": field, "cond": cond, "value": value}, [ds])
    key = "型号"
    assert [r[key] for r in outputs["main"]["rows"]] == expect
    assert quality == {"input_rows": len(ds["rows"]),
                       "filtered": len(ds["rows"]) - len(expect)}


def test_filter_null_semantics() -> None:
    """普通比较不匹配 null（含 eq/ne）；空值走 is_empty/not_empty。"""
    ds = {"kind": "table", "cols": ["型号", "功率_W"],
          "rows": [
              {"_id": "p1", "_source": "s", "型号": "P1", "功率_W": 3000},
              {"_id": "p2", "_source": "s", "型号": "P2", "功率_W": None},
              {"_id": "p4", "_source": "s", "型号": "P4"},  # 缺列键
          ]}
    # 普通（非 is_empty 族）条件下：cell 为 null/缺键的行永不命中；
    # value 为 null 的 eq 也永不命中（null 两侧都不参与普通比较）；
    # ne 3000 同样跳过 null 行——非空命中行里只剩与 3000 不等的 p?（本料无）。
    for cond, value, expect in (("gte", 2000, ["p1"]),
                                ("eq", None, []),
                                ("ne", 3000, []),
                                ("contains", "3", ["p1"])):
        outputs, _ = run_operator(
            "filter", {"field": "功率_W", "cond": cond, "value": value}, [ds])
        assert [r["_id"] for r in outputs["main"]["rows"]] == expect, \
            f"cond={cond} value={value!r}"
    outputs, _ = run_operator(
        "filter", {"field": "功率_W", "cond": "is_empty"}, [ds])
    assert [r["_id"] for r in outputs["main"]["rows"]] == ["p2", "p4"]
    outputs, _ = run_operator(
        "filter", {"field": "功率_W", "cond": "not_empty"}, [ds])
    assert [r["_id"] for r in outputs["main"]["rows"]] == ["p1"]


@pytest.mark.parametrize("config", [
    {"field": "幽灵列", "cond": "eq", "value": "P1"},
    {"field": "功率_W", "cond": "gt", "value": "abc"},   # 数值条件不可转
    {"field": "功率_W", "cond": "gte"},                  # 值条件缺 value
    {"field": "功率_W", "cond": "eq"},                   # eq 也要 value
], ids=["字段缺失", "value不可转数值", "gt缺value", "eq缺value"])
def test_filter_rejections(config, products) -> None:
    with pytest.raises(WorkspaceValidationError):
        run_operator("filter", config, [products])


def test_filter_is_empty_needs_no_value(products) -> None:
    outputs, _ = run_operator(
        "filter", {"field": "电源类型", "cond": "not_empty"}, [products])
    assert len(outputs["main"]["rows"]) == 3


def test_filter_eq_ne_loose_numeric_equality() -> None:
    """eq/ne 宽松相等钉测（M3）：数值口径下 "2000"==2000 命中（含空白
    归一、字符串/数值 value 双向）；ne 且 value=None=「非空即命中」——
    null 行仍不匹配（空值走 is_empty/not_empty），本断言即口径拍板。"""
    ds = {"kind": "table", "cols": ["型号", "功率_W"],
          "rows": [
              {"_id": "p1", "_source": "s", "型号": "P1", "功率_W": 3000},
              {"_id": "p2", "_source": "s", "型号": "P2", "功率_W": "2000"},
              {"_id": "p3", "_source": "s", "型号": "P3", "功率_W": 2000},
              {"_id": "p4", "_source": "s", "型号": "P4",
               "功率_W": " 2000 "},
              {"_id": "p5", "_source": "s", "型号": "P5", "功率_W": None},
          ]}

    def _ids(field: str, cond: str, value) -> list[str]:
        outputs, _ = run_operator(
            "filter", {"field": field, "cond": cond, "value": value}, [ds])
        return [r["_id"] for r in outputs["main"]["rows"]]

    # 数值宽松相等：三种书写形态同一值，字符串/数值 value 双向命中
    assert _ids("功率_W", "eq", "2000") == ["p2", "p3", "p4"]
    assert _ids("功率_W", "eq", 2000) == ["p2", "p3", "p4"]
    # 文本域不可数值化：回精确等
    assert _ids("型号", "eq", "P1") == ["p1"]
    # ne=eq 补集（null 行除外）：2000 的三种书写形态都被排除
    assert _ids("功率_W", "ne", "2000") == ["p1"]
    # ne 且 value=None：非空行全命中、null 行不命中（口径拍板，见 docstring）
    assert _ids("功率_W", "ne", None) == ["p1", "p2", "p3", "p4"]


# ------------------------------------------------------------ join


_JOIN_CFG = {"method": "left", "left_key": "型号", "right_key": "型号"}
_JOIN_CFG_INNER = {"method": "inner", "left_key": "型号", "right_key": "型号"}


def test_join_b3_left(products, inventory) -> None:
    """B.3 铁律断言：left 关联 → 3 行，P2 补充列为空。"""
    outputs, quality = run_operator(
        "join", _JOIN_CFG, [products, inventory])
    ds = outputs["main"]
    assert ds["cols"] == ["型号", "部件编码", "功率_W", "电源类型",
                          "右表_型号", "可用库存"]  # 冲突列前缀 右表_
    assert [r["_id"] for r in ds["rows"]] == ["p1", "p2", "p3"]  # _id=左._id
    by_id = {r["_id"]: r for r in ds["rows"]}
    assert by_id["p1"]["可用库存"] == 5
    assert by_id["p1"]["右表_型号"] == "P1"
    assert by_id["p1"]["_source"] == f"{_PRODUCT_SOURCE}；{_INVENTORY_SOURCE}"
    assert by_id["p2"]["可用库存"] is None            # 未匹配：补充列空
    assert by_id["p2"]["右表_型号"] is None
    assert by_id["p2"]["_source"] == _PRODUCT_SOURCE  # 仅左来源
    assert by_id["p3"]["可用库存"] == 0               # 0 是值
    assert quality == {"matched": 2, "unmatched_left": 1}


def test_join_b3_inner(products, inventory) -> None:
    """B.3 铁律断言：inner 关联 → 2 行（P1、P3）。"""
    outputs, quality = run_operator(
        "join", _JOIN_CFG_INNER, [products, inventory])
    ds = outputs["main"]
    assert [r["型号"] for r in ds["rows"]] == ["P1", "P3"]
    assert all(r["_source"] ==
               f"{_PRODUCT_SOURCE}；{_INVENTORY_SOURCE}" for r in ds["rows"])
    assert quality == {"matched": 2, "unmatched_left": 1}


def test_join_b3_right_duplicate_rejected(products, inventory_dup) -> None:
    """B.3 铁律：右表重复加入 P1 → 关联必须失败且不产出任何输出。"""
    with pytest.raises(WorkspaceValidationError):
        run_operator("join", _JOIN_CFG, [products, inventory_dup])
    with pytest.raises(WorkspaceValidationError):
        run_operator("join", _JOIN_CFG_INNER, [products, inventory_dup])


def test_join_null_keys_never_match(products, inventory) -> None:
    left = copy.deepcopy(products)
    left["rows"].append({"_id": "p9", "_source": "s9", "型号": None,
                         "部件编码": "PRD-0009", "功率_W": 999,
                         "电源类型": "AC"})
    right = copy.deepcopy(inventory)
    right["rows"].append({"_id": "i9", "_source": _INVENTORY_SOURCE,
                          "型号": None, "可用库存": 7})  # null 右键不参与匹配

    outputs, quality = run_operator("join", _JOIN_CFG, [left, right])
    by_id = {r["_id"]: r for r in outputs["main"]["rows"]}
    assert by_id["p9"]["可用库存"] is None
    assert by_id["p9"]["_source"] == "s9"
    assert quality == {"matched": 2, "unmatched_left": 2}

    outputs_i, quality_i = run_operator(
        "join", _JOIN_CFG_INNER, [left, right])
    assert [r["_id"] for r in outputs_i["main"]["rows"]] == ["p1", "p3"]
    assert quality_i == {"matched": 2, "unmatched_left": 2}


def test_join_no_conflict_no_prefix(products, inventory) -> None:
    right = {"kind": "table", "cols": ["产品型号", "可用库存"],
             "rows": [{"_id": "i1", "_source": _INVENTORY_SOURCE,
                       "产品型号": "P1", "可用库存": 5}]}
    outputs, _ = run_operator(
        "join", {"method": "left", "left_key": "型号", "right_key": "产品型号"},
        [products, right])
    ds = outputs["main"]
    assert ds["cols"] == ["型号", "部件编码", "功率_W", "电源类型",
                          "产品型号", "可用库存"]  # 无冲突不加前缀
    by_id = {r["_id"]: r for r in ds["rows"]}
    assert by_id["p1"]["产品型号"] == "P1" and by_id["p1"]["可用库存"] == 5
    assert by_id["p2"]["产品型号"] is None


@pytest.mark.parametrize("config", [
    {"method": "left", "left_key": "幽灵列", "right_key": "型号"},
    {"method": "left", "left_key": "型号", "right_key": "幽灵列"},
    {"method": "outer", "left_key": "型号", "right_key": "型号"},
    {"left_key": "型号", "right_key": "型号"},          # method 必填
    {"method": "left", "right_key": "型号"},            # left_key 必填
], ids=["左键不在左cols", "右键不在右cols", "method词表外",
        "缺method", "缺left_key"])
def test_join_rejections(config, products, inventory) -> None:
    with pytest.raises(WorkspaceValidationError):
        run_operator("join", config, [products, inventory])


# ------------------------------------------------------------ split_sections


def test_split_sections_level1(docs) -> None:
    outputs, quality = run_operator("split_sections", {"level": 1}, [docs])
    ds = outputs["main"]
    assert ds["cols"] == ["文件", "章节", "内容"]
    assert [r["_id"] for r in ds["rows"]] == \
        ["doc-1:基本属性", "doc-1:技术规格"]  # 一行一节，_id=文档:章节
    first = ds["rows"][0]
    assert first["文件"] == "P1产品资料.md"
    assert first["章节"] == "基本属性"
    assert first["内容"] == "型号P1，功率3000W"
    assert first["_source"] == "kb1/方案/P1产品资料.md"  # 来源继承文档
    assert quality == {"no_section_docs": ["P3产品资料.md"]}  # 仅二级的文档


def test_split_sections_level2(docs) -> None:
    outputs, quality = run_operator("split_sections", {"level": 2}, [docs])
    ds = outputs["main"]
    assert [r["_id"] for r in ds["rows"]] == ["doc-1:配套关系", "doc-2:备注"]
    assert quality == {"no_section_docs": []}


def test_split_sections_string_payload_to_content_column() -> None:
    """M2（计划明文）：文档内容_JSON 为字符串的行原样进「内容」列——
    章节=空串、内容=该字符串、_id=文档_id:全文；文件列与来源继承；
    不进问题账，也不阻断结构化文档。"""
    ds = {"kind": "table", "cols": ["文件", "文档内容_JSON"],
          "rows": [
              {"_id": "plain", "_source": "s1", "文件": "纯文本.md",
               "文档内容_JSON": "整份文档没有章节结构，全文一行"},
              {"_id": "ok", "_source": "s2", "文件": "结构化.md",
               "文档内容_JSON": {"sections": [
                   {"level": 1, "title": "基本属性", "text": "有效"}]}},
          ]}
    outputs, quality = run_operator("split_sections", {"level": 1}, [ds])
    ds_out = outputs["main"]
    assert [r["_id"] for r in ds_out["rows"]] == ["plain:全文", "ok:基本属性"]
    first = ds_out["rows"][0]
    assert first["文件"] == "纯文本.md"
    assert first["章节"] == ""
    assert first["内容"] == "整份文档没有章节结构，全文一行"
    assert first["_source"] == "s1"
    assert quality == {"no_section_docs": []}


def test_split_sections_json_string_payload_expands() -> None:
    """Task 13 e2e 集成缺口钉测：read_kb_dir 把 文档内容_JSON 存成 canonical
    JSON 字符串（Task 5 决策 4——dict 非六列型合法列值）——字符串载荷解析出
    dict 时必须走章节展开（否则真实 kb_dir 源接 split_sections 只会产出每
    文档一行「全文」，拆章节算子对真实来源不可达）；解不出 dict 的字符串
    （纯文本 / JSON 数组等）仍走全文路径（与上一用例同语义）。"""
    body = {"document_id": "d9", "title": "产品.md", "sections": [
        {"level": 1, "title": "基本属性", "text": "属性段"},
        {"level": 1, "title": "技术规格", "text": "规格段"},
    ]}
    ds = {"kind": "table", "cols": ["文件", "文档内容_JSON"],
          "rows": [
              {"_id": "d9", "_source": "kb1//产品.md", "文件": "产品.md",
               "文档内容_JSON": json.dumps(
                   body, ensure_ascii=False, separators=(",", ":"))},
              {"_id": "a1", "_source": "s2", "文件": "数组.md",
               "文档内容_JSON": "[1, 2]"},
          ]}
    outputs, quality = run_operator("split_sections", {"level": 1}, [ds])
    rows = outputs["main"]["rows"]
    assert [r["_id"] for r in rows] == ["d9:基本属性", "d9:技术规格",
                                        "a1:全文"]
    assert rows[0]["章节"] == "基本属性" and rows[0]["内容"] == "属性段"
    assert rows[1]["内容"] == "规格段"
    assert rows[2]["章节"] == "" and rows[2]["内容"] == "[1, 2]"
    assert quality == {"no_section_docs": []}


def test_split_sections_duplicate_titles_get_occurrence_suffix() -> None:
    """M1：同文档同级别同标题多节——_id 追加出现序后缀防撞号（B.3：新行
    身份由来源文档和章节定位组合；首个不带后缀，第二个起 #2/#3）。"""
    ds = {"kind": "table", "cols": ["文件", "文档内容_JSON"],
          "rows": [
              {"_id": "d1", "_source": "s", "文件": "重复标题.md",
               "文档内容_JSON": {"sections": [
                   {"level": 1, "title": "备注", "text": "第一段备注"},
                   {"level": 1, "title": "基本属性", "text": "属性"},
                   {"level": 1, "title": "备注", "text": "第二段备注"},
                   {"level": 1, "title": "备注", "text": "第三段备注"},
                   {"level": 2, "title": "备注", "text": "二级不算"},
               ]}},
          ]}
    outputs, quality = run_operator("split_sections", {"level": 1}, [ds])
    rows = outputs["main"]["rows"]
    assert [r["_id"] for r in rows] == \
        ["d1:备注", "d1:基本属性", "d1:备注#2", "d1:备注#3"]
    assert [r["内容"] for r in rows] == \
        ["第一段备注", "属性", "第二段备注", "第三段备注"]
    # 后缀只按 (文档, 标题) 计数：level=2 的同名节不消耗 level=1 的序号
    outputs2, _ = run_operator("split_sections", {"level": 2}, [ds])
    assert [r["_id"] for r in outputs2["main"]["rows"]] == ["d1:备注"]
    assert quality == {"no_section_docs": []}


def test_split_sections_problem_rows_into_account() -> None:
    """畸形载荷（非 dict 非字符串 / dict 无 sections 键 / sections 非
    列表）与无该级章节的文档同进问题账 no_section_docs，不产出行、不
    阻断其余文档。"""
    ds = {"kind": "table", "cols": ["文件", "文档内容_JSON"],
          "rows": [
              {"_id": "ok", "_source": "s1", "文件": "好文档.md",
               "文档内容_JSON": {"sections": [
                   {"level": 1, "title": "基本属性", "text": "有效"}]}},
              {"_id": "num", "_source": "s2", "文件": "数字载荷.md",
               "文档内容_JSON": 12345},
              {"_id": "nosec", "_source": "s3", "文件": "无章节键.md",
               "文档内容_JSON": {"标题": "没有sections键"}},
              {"_id": "badsec", "_source": "s4", "文件": "章节非列表.md",
               "文档内容_JSON": {"sections": "不是列表"}},
          ]}
    outputs, quality = run_operator("split_sections", {"level": 1}, [ds])
    assert [r["_id"] for r in outputs["main"]["rows"]] == ["ok:基本属性"]
    assert quality == {"no_section_docs": ["数字载荷.md", "无章节键.md",
                                           "章节非列表.md"]}


def test_split_sections_rejections(docs, products) -> None:
    with pytest.raises(WorkspaceValidationError):  # 缺 文档内容_JSON 列
        run_operator("split_sections", {"level": 1}, [products])
    no_file = {"kind": "table", "cols": ["文档内容_JSON"],
               "rows": [{"_id": "d", "_source": "s",
                         "文档内容_JSON": {"sections": []}}]}
    with pytest.raises(WorkspaceValidationError):  # 缺 文件 列
        run_operator("split_sections", {"level": 1}, [no_file])


# ------------------------------------------------------------ B.3 链路组合


def test_b3_chain_left_join_then_filter(products, inventory) -> None:
    """B.3 端到端口径（确定性部分）：left 关联后筛选功率 ≥2000W → P1、P2。"""
    joined, _ = run_operator("join", _JOIN_CFG, [products, inventory])
    outputs, quality = run_operator(
        "filter", {"field": "功率_W", "cond": "gte", "value": 2000},
        [joined["main"]])
    assert [r["型号"] for r in outputs["main"]["rows"]] == ["P1", "P2"]
    assert [r["可用库存"] for r in outputs["main"]["rows"]] == [5, None]
    assert quality == {"input_rows": 3, "filtered": 1}
    # 算子输出可直接进数据集契约（validate/hash 落 content_hash 链路可用）
    validate_dataset(outputs["main"])
    assert re.fullmatch(r"[0-9a-f]{64}", dataset_hash(outputs["main"]))
