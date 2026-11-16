# -*- coding: utf-8 -*-
"""KB 知识制品工作台算子注册表 + 4 确定性算子（67号工作台 MVP Task 4；
Task 7 加 llm_extract 条目）。

注册表 ``OPERATOR_DEFS`` 是封闭 dict（新增算子=改本模块加条目；登记层
db_workspaces._VALID_OPS 是图上可出现的算子词表——**source 节点不入注册
表**（executor 直读 kb_workspace_sources.input_manifest），**llm_extract
条目 kind="llm"、run=None**——执行不走 run_operator 的同步 run，由
executor 按 kind 分派 workspace_llm_operator.run_full/run_trial（LlmTaskClient
异步批），run_operator 对其显式拒绝）。

条目字段：``{"op", "name", "input_arity", "kind"(默认 deterministic),
"ports"(list[PortSpec]), "config_schema", "run"(llm 算子为 None)}``。
``config_schema`` 是
json-schema-lite：每字段 ``{type, required, enum?, ...}``（type ∈
string/integer/number/boolean/array/object/any；额外描述键仅供前端表单
消费，后端只校验顶层字段；数组元素结构由算子自身校验）——前端表单与后端
校验共用一份。

4 算子行为口径 = 64号 B.3 固定材料（P1/3000W、P2/2000W、P3/1200W +
库存 P1/5、P3/0）：功率 ≥2000 → 2 行；left 关联 3 行（P2 库存空）、
inner 2 行；**右键值重复 → Validation 且不产出任何输出**（B.3 铁律：
一对多膨胀必须显式声明，不做）；普通比较不匹配 null（空值走
is_empty/not_empty）。

本模块纯函数、无 DB 依赖；错误类复用登记层 WorkspaceValidationError。
topological_order 与 db_workspaces._ensure_acyclic 同语义（inputs 当边），
供 executor 逐节点执行取序。
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from knowledge_mining.mining.kb.db_workspaces import WorkspaceValidationError
from knowledge_mining.mining.kb.workspace_dataset import validate_dataset

__all__ = [
    "OPERATOR_DEFS",
    "PortSpec",
    "run_operator",
    "topological_order",
]


@dataclass(frozen=True)
class PortSpec:
    """算子输出端口声明（画布端口与 run() 返回 outputs 的键一致）。"""

    id: str
    name: str
    kind: str  # table | documents | relations（MVP 只有 table）


#: config_schema 的 type 词 → Python 判定（"any" 不设 type 或留空即可）。
_CONFIG_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "any": lambda v: True,
}

#: filter 条件词表（普通比较对 null 一律不匹配，空值走后两个）。
_FILTER_CONDS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains",
                 "is_empty", "not_empty")
#: 需要携带 value 的条件。
_VALUE_CONDS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains")
#: 数值比较条件（value 必须可转 float）。
_NUMERIC_CONDS = ("gt", "gte", "lt", "lte")

#: split_sections 输入/输出的固定列名（Task 5 kb_dir 底表契约）。
_DOC_CONTENT_COL = "文档内容_JSON"
_FILE_COL = "文件"
#: split_sections 输出的两列固定名（Task 7 质量收口 C 常量化）。
_SECTION_COL = "章节"
_CONTENT_COL = "内容"

#: join 右列与左列名冲突时的前缀（B.3：合并双方来源、冲突列可辨识）。
_RIGHT_PREFIX = "右表_"

#: join 合并双方 _source 的分隔符（全角分号，与 67号 §5.2 行来源串接一致）。
_SOURCE_SEP = "；"


# ------------------------------------------------------------ 通用校验


def _validate_config(op: str, schema: dict[str, Any], config: Any) -> None:
    """config 按 config_schema 校验：非对象/缺必填/类型不符/enum 词表外
    → WorkspaceValidationError（未知键放行——前端表单向前兼容）。"""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise WorkspaceValidationError(
            f"operator {op}: config must be an object")
    for field, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        if field not in config:
            if spec.get("required"):
                raise WorkspaceValidationError(
                    f"operator {op}: config.{field} is required")
            continue
        value = config[field]
        expected = spec.get("type", "any")
        predicate = _CONFIG_TYPES.get(expected)
        if predicate is None or not predicate(value):
            raise WorkspaceValidationError(
                f"operator {op}: config.{field} must be of type"
                f" {expected}: {value!r}")
        if "enum" in spec and value not in spec["enum"]:
            raise WorkspaceValidationError(
                f"operator {op}: config.{field} must be one of"
                f" {list(spec['enum'])}: {value!r}")


def run_operator(
    op: str, config: Any, inputs: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """算子统一入口：注册表查条目 → inputs 数量/契约校验 → config 校验
    → 调 def["run"](config, inputs) → 输出逐个过 validate_dataset
    （坏算子不外漏）→ 返回 (outputs, quality)。"""
    if op not in OPERATOR_DEFS:
        raise WorkspaceValidationError(
            f"unknown operator: {op!r} (registered:"
            f" {sorted(OPERATOR_DEFS)})")
    defn = OPERATOR_DEFS[op]
    if not isinstance(inputs, list):
        raise WorkspaceValidationError(
            f"operator {op}: inputs must be a list of datasets")
    if len(inputs) != defn["input_arity"]:
        raise WorkspaceValidationError(
            f"operator {op} expects {defn['input_arity']} input(s),"
            f" got {len(inputs)}")
    for i, ds in enumerate(inputs):
        try:
            validate_dataset(ds)
        except WorkspaceValidationError as exc:
            raise WorkspaceValidationError(
                f"operator {op}: input #{i} invalid: {exc}") from exc
    if defn.get("kind") == "llm" or defn.get("run") is None:
        raise WorkspaceValidationError(
            f"operator {op}: kind=llm 算子不经 run_operator 同步执行"
            f"（executor 分派 workspace_llm_operator.run_full/run_trial）")
    _validate_config(op, defn["config_schema"], config)
    outputs, quality = defn["run"](config if config is not None else {},
                                   inputs)
    if not isinstance(outputs, dict) or not outputs:
        raise WorkspaceValidationError(
            f"operator {op}: run() must return a non-empty outputs dict")
    for port_id, ds in outputs.items():
        try:
            validate_dataset(ds)
        except WorkspaceValidationError as exc:
            raise WorkspaceValidationError(
                f"operator {op}: output port {port_id!r} invalid:"
                f" {exc}") from exc
    if not isinstance(quality, dict):
        raise WorkspaceValidationError(
            f"operator {op}: run() must return a quality dict")
    return outputs, quality


# ------------------------------------------------------------ 拓扑排序


def topological_order(nodes: Any) -> list[str]:
    """Kahn 拓扑序（inputs 当边：producer→consumer），与 db_workspaces.
    _ensure_acyclic 同语义——节点 {id, inputs:["node:port"]}，返回执行序
    节点 id 列表；环/畸形（非 list、节点非 dict、id 空或重复、inputs 非
    列表、引用缺冒号或悬空）→ WorkspaceValidationError。无入度节点按
    声明序出队（确定性）。"""
    if not isinstance(nodes, list):
        raise WorkspaceValidationError("nodes must be a list")
    indegree: dict[str, int] = {}
    consumers: dict[str, list[str]] = {}
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise WorkspaceValidationError(
                f"graph.nodes[{i}] must be an object")
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            raise WorkspaceValidationError(
                f"graph.nodes[{i}].id must be a non-empty string")
        if nid in indegree:
            raise WorkspaceValidationError(f"duplicate node id: {nid}")
        indegree[nid] = 0
        consumers[nid] = []
    for node in nodes:
        inputs = node.get("inputs")
        if inputs is None:
            inputs = []
        if not isinstance(inputs, list):
            raise WorkspaceValidationError(
                f"node {node['id']}: inputs must be a list")
        for ref in inputs:
            if not isinstance(ref, str) or ":" not in ref:
                raise WorkspaceValidationError(
                    f"node {node['id']}: input must be 'node:port': {ref!r}")
            producer = ref.split(":", 1)[0]
            if producer not in indegree:
                raise WorkspaceValidationError(
                    f"node {node['id']} input references missing node:"
                    f" {producer}")
            consumers[producer].append(node["id"])
            indegree[node["id"]] += 1
    queue = deque(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for consumer in consumers[nid]:
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                queue.append(consumer)
    if len(order) != len(indegree):
        raise WorkspaceValidationError("graph contains a cycle")
    return order


# ------------------------------------------------------------ select 算子


def _run_select(
    config: dict[str, Any], inputs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """列选择/改名：columns 列表顺序即输出列序；至少 1 列；未知列/改名
    目标重复/目标撞 _id·_source → Validation；行身份与来源不变（B.3）。
    源列缺值（缺键）输出为 None（行字段补齐到新 cols）。"""
    ds = inputs[0]
    col_set = set(ds.get("cols", []))
    columns = config.get("columns")
    if not isinstance(columns, list) or not columns:
        raise WorkspaceValidationError(
            "select: config.columns must be a non-empty list")
    picked: list[tuple[str, str]] = []
    targets: set[str] = set()
    for i, entry in enumerate(columns):
        if not isinstance(entry, dict):
            raise WorkspaceValidationError(
                f"select: columns[{i}] must be an object")
        cid = entry.get("id")
        if not isinstance(cid, str) or not cid:
            raise WorkspaceValidationError(
                f"select: columns[{i}].id must be a non-empty string")
        if cid not in col_set:
            raise WorkspaceValidationError(f"select: unknown column: {cid!r}")
        name = entry.get("name", cid)
        if not isinstance(name, str) or not name:
            raise WorkspaceValidationError(
                f"select: columns[{i}].name must be a non-empty string")
        if name in ("_id", "_source"):
            raise WorkspaceValidationError(
                f"select: output column must not shadow reserved"
                f" field: {name!r}")
        if name in targets:
            raise WorkspaceValidationError(
                f"select: duplicate output column: {name!r}")
        targets.add(name)
        picked.append((cid, name))
    rows_out = []
    for row in ds.get("rows", []):
        out: dict[str, Any] = {"_id": row["_id"], "_source": row["_source"]}
        for cid, name in picked:
            out[name] = row.get(cid)
        rows_out.append(out)
    return ({"main": {"kind": "table", "cols": [name for _, name in picked],
                      "rows": rows_out}}, {})


# ------------------------------------------------------------ filter 算子


def _as_number(value: Any) -> float | None:
    """数值强转（bool/不可转 → None）：int/float 直取，str 容错空白。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _is_empty_cell(cell: Any) -> bool:
    """空值判定：None 或空串（is_empty/not_empty 专用；0 是值不是空）。"""
    return cell is None or (isinstance(cell, str) and cell.strip() == "")


def _loose_eq(cell: Any, value: Any) -> bool:
    """相等判定：先精确等，再尝试数值口径（"2000" == 2000 命中）。"""
    if cell == value:
        return True
    a, b = _as_number(cell), _as_number(value)
    return a is not None and b is not None and a == b


def _filter_match(cond: str, cell: Any, value: Any) -> bool:
    if cond == "is_empty":
        return _is_empty_cell(cell)
    if cond == "not_empty":
        return not _is_empty_cell(cell)
    if cell is None:  # 普通比较不匹配 null（B.3：空值走 is_empty 族）
        return False
    if cond in _NUMERIC_CONDS:
        a, b = _as_number(cell), _as_number(value)
        if a is None or b is None:
            return False
        if cond == "gt":
            return a > b
        if cond == "gte":
            return a >= b
        if cond == "lt":
            return a < b
        return a <= b  # lte
    if cond == "contains":
        return str(value) in str(cell)
    if cond == "eq":
        return _loose_eq(cell, value)
    return not _loose_eq(cell, value)  # ne（cell 非 null 已保证）


def _run_filter(
    config: dict[str, Any], inputs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ds = inputs[0]
    cols = ds.get("cols", [])
    field = config.get("field")
    if field not in cols:
        raise WorkspaceValidationError(
            f"filter: field not in input columns: {field!r}")
    cond = config.get("cond")
    if cond in _VALUE_CONDS and "value" not in config:
        raise WorkspaceValidationError(
            f"filter: cond {cond!r} requires a value")
    if cond in _NUMERIC_CONDS and _as_number(config.get("value")) is None:
        raise WorkspaceValidationError(
            f"filter: cond {cond!r} requires a numeric value:"
            f" {config.get('value')!r}")
    hits = [dict(row) for row in ds.get("rows", [])
            if _filter_match(cond, row.get(field), config.get("value"))]
    total = len(ds.get("rows", []))
    return ({"main": {"kind": "table", "cols": list(cols), "rows": hits}},
            {"input_rows": total, "filtered": total - len(hits)})


# ------------------------------------------------------------ join 算子


def _join_right_index(
    right: dict[str, Any], rkey: str,
) -> dict[Any, dict[str, Any]]:
    """右表索引（Task 7 质量收口 C 自 _run_join 拆出）：null 键不参与匹配
    也不参与重复判定；非标量键不可 hash 拒；右键值重复 → Validation 且不产
    出任何输出（B.3 铁律：一对多膨胀必须显式声明，不做）。"""
    index: dict[Any, dict[str, Any]] = {}
    for row in right.get("rows", []):
        key = row.get(rkey)
        if key is None:
            continue
        try:
            hash(key)
        except TypeError:
            raise WorkspaceValidationError(
                f"join: key values must be scalar: {key!r}") from None
        if key in index:
            raise WorkspaceValidationError(
                f"join: duplicate right key {key!r} (one-to-many expansion"
                f" is undeclared; normalize or aggregate first — B.3)")
        index[key] = row
    return index


def _join_out_col_names(
    lcols: list[str], rcols: list[str],
) -> tuple[list[str], dict[str, str]]:
    """输出列命名（同上拆出）：左列原序 + 右列（与左冲突 → 右表_ 前缀，
    仍冲突再前缀一次，再撞即拒——无法构造唯一列名）；返回 (右列输出名序
    列, 右列原名→输出名映射)。"""
    taken = set(lcols)
    r_out_names: list[str] = []
    r_name_map: dict[str, str] = {}
    for col in rcols:
        out = col if col not in taken else f"{_RIGHT_PREFIX}{col}"
        if out in taken:
            out = f"{_RIGHT_PREFIX}{out}"
        if out in taken:
            raise WorkspaceValidationError(
                f"join: cannot derive a unique output column for {col!r}")
        taken.add(out)
        r_name_map[col] = out
        r_out_names.append(out)
    return r_out_names, r_name_map


def _run_join(
    config: dict[str, Any], inputs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """左/内关联（B.3）：右键值重复 → Validation 且不产出任何输出（铁律：
    一对多膨胀必须显式声明）；null 键不参与匹配；未匹配左行 left 保留、
    补充列空；输出 _id=左._id；_source 匹配时=左；右、未匹配仅左；右列
    与左冲突加 右表_ 前缀；quality 记 matched/unmatched_left。"""
    left, right = inputs
    method = config.get("method")
    lkey = config.get("left_key")
    rkey = config.get("right_key")
    lcols: list[str] = list(left.get("cols", []))
    rcols: list[str] = list(right.get("cols", []))
    if lkey not in lcols:
        raise WorkspaceValidationError(
            f"join: left_key not in left columns: {lkey!r}")
    if rkey not in rcols:
        raise WorkspaceValidationError(
            f"join: right_key not in right columns: {rkey!r}")
    index = _join_right_index(right, rkey)
    r_out_names, r_name_map = _join_out_col_names(lcols, rcols)

    matched = unmatched = 0
    rows_out: list[dict[str, Any]] = []
    for lrow in left.get("rows", []):
        key = lrow.get(lkey)
        rrow = index.get(key) if key is not None else None
        if rrow is None:
            unmatched += 1
            if method == "inner":
                continue
            out = {"_id": lrow["_id"], "_source": lrow["_source"]}
            for col in lcols:
                out[col] = lrow.get(col)
            for col in rcols:
                out[r_name_map[col]] = None
        else:
            matched += 1
            out = {"_id": lrow["_id"],
                   "_source": f"{lrow['_source']}{_SOURCE_SEP}"
                              f"{rrow['_source']}"}
            for col in lcols:
                out[col] = lrow.get(col)
            for col in rcols:
                out[r_name_map[col]] = rrow.get(col)
        rows_out.append(out)
    return ({"main": {"kind": "table", "cols": [*lcols, *r_out_names],
                      "rows": rows_out}},
            {"matched": matched, "unmatched_left": unmatched})


# ------------------------------------------------------------ split_sections


def _split_row(row: dict[str, Any], level: int) -> list[dict[str, Any]]:
    """单文档行展开（Task 7 质量收口 C 自 _run_split_sections 拆出）：载荷
    三态——字符串先按 文档内容_JSON 契约尝试 json.loads（Task 13 e2e 集成
    缺口修复：read_kb_dir 决策 4 把章节结构存成 canonical JSON 字符串——
    dict 非六列型合法列值，列名本就承诺 JSON；解析出 dict 即走结构路径，
    否则真实 kb_dir 源接 split_sections 只会产出每文档一行「全文」）；
    解不出 dict 的字符串（纯文本/JSON 数组等）→ 原样进「内容」列（章节
    空串、_id=文档_id:全文——计划明文）；dict 且 sections 为列表 → 逐节
    展开（一行一节，_id=文档_id:章节标题，同文档同级别同标题多节追加
    出现序后缀 #2/#3 防撞号——首个不带；B.3：新行身份由来源文档和章节
    定位组合）；其余（非 dict 非字符串 / dict 无 sections 键 / sections
    非列表 / 无该级别有效节）→ 空列表，由调用方记 no_section_docs。
    _source 继承文档来源。"""
    file_name = row.get(_FILE_COL)
    payload = row.get(_DOC_CONTENT_COL)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    if isinstance(payload, str):
        return [{
            "_id": f"{row['_id']}:全文",
            "_source": row["_source"],
            _FILE_COL: file_name,
            _SECTION_COL: "",
            _CONTENT_COL: payload,
        }]
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, list):
        return []
    out: list[dict[str, Any]] = []
    title_seen: dict[str, int] = {}  # 只计本级命中节的出现序
    for item in sections:
        if not isinstance(item, dict):
            continue
        item_level = item.get("level")
        if isinstance(item_level, bool) \
                or not isinstance(item_level, int) \
                or item_level != level:
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title:
            continue
        occurrence = title_seen.get(title, 0) + 1
        title_seen[title] = occurrence
        out_id = f"{row['_id']}:{title}" if occurrence == 1 \
            else f"{row['_id']}:{title}#{occurrence}"
        text = item.get("text")
        out.append({
            "_id": out_id,
            "_source": row["_source"],
            _FILE_COL: file_name,
            _SECTION_COL: title,
            _CONTENT_COL: text if isinstance(text, str) else "",
        })
    return out


def _run_split_sections(
    config: dict[str, Any], inputs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """拆章节（B.3：按声明级别展开，一文件可多行）：输入必须含
    文档内容_JSON 与 文件 列。逐行展开逻辑在 _split_row（Task 7 质量收口
    C 拆出）；零产出文档进问题账 no_section_docs（畸形/无该级别章节），
    不阻断其余文档。输出长表 cols=[文件,章节,内容]。"""
    ds = inputs[0]
    cols = ds.get("cols", [])
    if _DOC_CONTENT_COL not in cols:
        raise WorkspaceValidationError(
            f"split_sections: input must contain a {_DOC_CONTENT_COL!r}"
            " column")
    if _FILE_COL not in cols:
        raise WorkspaceValidationError(
            f"split_sections: input must contain a {_FILE_COL!r} column")
    level = config.get("level")
    rows_out: list[dict[str, Any]] = []
    no_section: list[str] = []
    for row in ds.get("rows", []):
        file_name = row.get(_FILE_COL)
        account = file_name if isinstance(file_name, str) else str(file_name)
        emitted = _split_row(row, level)
        rows_out.extend(emitted)
        if not emitted:
            no_section.append(account)  # 畸形/无该级章节：不产出行，记账
    return ({"main": {"kind": "table",
                      "cols": [_FILE_COL, _SECTION_COL, _CONTENT_COL],
                      "rows": rows_out}},
            {"no_section_docs": no_section})


# ------------------------------------------------------------ 注册表


OPERATOR_DEFS: dict[str, dict[str, Any]] = {
    "select": {
        "op": "select", "name": "选择列/改名", "input_arity": 1,
        "kind": "deterministic",
        "ports": [PortSpec("main", "选择结果", "table")],
        "config_schema": {
            "columns": {"type": "array", "required": True,
                        "item_fields": {"id": "string", "name": "string"}},
        },
        "run": _run_select,
    },
    "filter": {
        "op": "filter", "name": "筛选", "input_arity": 1,
        "kind": "deterministic",
        "ports": [PortSpec("main", "筛选结果", "table")],
        "config_schema": {
            "field": {"type": "string", "required": True},
            "cond": {"type": "string", "required": True,
                     "enum": list(_FILTER_CONDS)},
            "value": {},  # any：eq/ne/contains/数值条件必带（算子内判）
        },
        "run": _run_filter,
    },
    "join": {
        "op": "join", "name": "按键关联", "input_arity": 2,
        "kind": "deterministic",
        "ports": [PortSpec("main", "关联结果", "table")],
        "config_schema": {
            "method": {"type": "string", "required": True,
                       "enum": ["left", "inner"]},
            "left_key": {"type": "string", "required": True},
            "right_key": {"type": "string", "required": True},
        },
        "run": _run_join,
    },
    "split_sections": {
        "op": "split_sections", "name": "拆章节", "input_arity": 1,
        "kind": "deterministic",
        "ports": [PortSpec("main", "章节长表", "table")],
        "config_schema": {
            "level": {"type": "integer", "required": True, "enum": [1, 2]},
        },
        "run": _run_split_sections,
    },
    "llm_extract": {
        "op": "llm_extract", "name": "LLM 逐行抽取", "input_arity": 1,
        "kind": "llm",
        "ports": [PortSpec("main", "抽取结果", "table")],
        "config_schema": {
            "input_column": {"type": "string", "required": True},
            "prompt_template": {"type": "string", "required": True},
            "output_fields": {"type": "array", "required": True,
                              "item_fields": {"name": "string",
                                              "type": "string"}},
            "model": {"type": "string"},
        },
        # run=None：执行由 executor 按 kind="llm" 分派
        # workspace_llm_operator.run_full/run_trial（LlmTaskClient 异步批；
        # 元素级语义校验也在该模块——output_fields 六列型词表等）。
        "run": None,
    },
}
