"""Deterministic operators over immutable workbench snapshots (no database writes)."""
from __future__ import annotations

import copy
import json
import math
import re
from difflib import SequenceMatcher

from knowledge_mining.mining.kb.services.artifact_service import InvalidOperation
from knowledge_mining.mining.kb.services.artifact_import_service import _cast_value

CATALOG = [
    ("drop_columns", "删除列"),
    ("select", "筛选 / 保留列"), ("fields", "字段改名 / 类型 / 排序"),
    ("deduplicate", "去重"), ("expand", "展开表格 JSON"),
    ("classify", "表格分类"), ("merge", "同类表格合并"),
    ("join", "关联对照表"), ("edit", "人工调整"), ("llm_columns", "大模型加列"),
]
RESERVED = {"row_id", "provenance", "document_id", "_llm", "_llm_history"}
TYPES = {"text", "int", "float", "bool", "date", "json"}


def display(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else ("" if value is None else str(value))


def materialize(snapshot):
    result = copy.deepcopy(snapshot)
    if "columns" not in result:
        from .artifact_tables import COLUMNS
        result["columns"] = copy.deepcopy(COLUMNS)
    if any(c["key"] == "file_content" for c in result["columns"]):
        for row in result["rows"]:
            row.setdefault("file_content", result.get("documents", {}).get(row.get("document_id"), ""))
    return result


def selection(snapshot, params):
    result = materialize(snapshot)
    keys = {c["key"] for c in result["columns"]}
    filters = params.get("filters", [])
    keep = params.get("keep_columns") or [c["key"] for c in result["columns"]]
    if not set(keep) <= keys or len(set(keep)) != len(keep):
        raise InvalidOperation("保留列不存在或重复")
    for f in filters:
        if f.get("column") not in keys or f.get("op") not in {"contains", "eq", "ne", "empty", "not_empty", "gt", "lt"}:
            raise InvalidOperation("筛选条件无效")

    def matches(row, f):
        v, wanted, op = row.get(f["column"]), f.get("value", ""), f["op"]
        if op == "empty": return v is None or v == ""
        if op == "not_empty": return v is not None and v != ""
        if op == "contains": return display(wanted).casefold() in display(v).casefold()
        if op == "eq": return display(v) == display(wanted)
        if op == "ne": return display(v) != display(wanted)
        try: return float(v) > float(wanted) if op == "gt" else float(v) < float(wanted)
        except (ValueError, TypeError): return False

    result["rows"] = [{k: v for k, v in r.items() if k in keep or k in RESERVED}
                      for r in result["rows"] if all(matches(r, f) for f in filters)]
    result["columns"] = [next(c for c in result["columns"] if c["key"] == key) for key in keep]
    return result


def cast(value, kind):
    if kind == "json":
        return json.loads(value) if isinstance(value, str) else value
    if kind == "text" and isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    ok, converted = _cast_value(value, kind)
    if not ok: raise ValueError(f"无法转换为 {kind}")
    if isinstance(converted, float) and not math.isfinite(converted): raise ValueError("数值必须有限")
    return converted


def validate_columns(columns, existing=()):
    keys = [c.get("key", "").strip() for c in columns]
    if not keys or len(keys) > 200 or len(set(keys)) != len(keys):
        raise InvalidOperation("请配置 1–200 个不重复的字段")
    for c, key in zip(columns, keys):
        if not key or len(key) > 200 or key in RESERVED or key in existing or c.get("type", "text") not in TYPES:
            raise InvalidOperation(f"字段名称或类型无效：{key}")


def nested_records(table):
    if not isinstance(table, dict): raise InvalidOperation("所选列不是表格 JSON")
    if "数据" in table:
        rows = table["数据"]
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise InvalidOperation("表格 数据 必须是对象数组")
        return rows
    if "rows" in table:
        rows = table["rows"]
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise InvalidOperation("表格 rows 必须是对象数组")
        return rows
    cells = table.get("cells", [])
    names = {}
    for cell in cells:
        index = cell["column_index"]
        name = cell.get("column_name") or f"列{index + 1}"
        names.setdefault(index, name)
    # Preserve duplicate column titles without overwriting cells.
    used = set()
    for index in sorted(names):
        name = names[index]
        if name in used: name = f"{name}_{index + 1}"
        names[index] = name
        used.add(name)
    rows = {}
    for cell in cells:
        if cell.get("is_header"): continue
        rows.setdefault(cell["row_index"], {})[names[cell["column_index"]]] = cell.get("value")
    return [rows[i] for i in sorted(rows)]


def transform(snapshot, operator, params, lookup=None):
    out = selection(snapshot, params)
    rows, columns = out["rows"], out["columns"]
    keys = {c["key"] for c in columns}
    def require(names):
        if not names or not set(names) <= keys: raise InvalidOperation("请选择有效字段")
    if operator == "select": pass
    elif operator == "drop_columns":
        names = params.get("drop_columns", [])
        require(names)
        remaining = [c for c in columns if c["key"] not in names]
        if not remaining:
            raise InvalidOperation("至少保留一列，不能删除全部列")
        out["columns"] = remaining
        out["rows"] = [{k: v for k, v in row.items() if k not in names} for row in rows]
    elif operator == "fields":
        specs = params.get("fields", [])
        validate_columns(specs)
        require([c["source"] for c in specs])
        new_rows = []
        for row in rows:
            try:
                new_rows.append({k: v for k, v in row.items() if k in RESERVED} |
                                {c["key"]: cast(row.get(c["source"]), c.get("type", "text")) for c in specs})
            except (ValueError, TypeError) as exc:
                raise InvalidOperation(f"行 {row['row_id']} 类型转换失败：{exc}") from None
        out["rows"] = new_rows
        out["columns"] = [{"key": c["key"], "name": c.get("name") or c["key"], "type": c.get("type", "text")} for c in specs]
    elif operator == "deduplicate":
        names = params.get("keys", [])
        require(names)
        seen = set()
        out["rows"] = []
        for row in rows:
            key = json.dumps([row.get(k) for k in names], ensure_ascii=False, sort_keys=True)
            if key not in seen: out["rows"].append(row)
            seen.add(key)
    elif operator == "classify":
        field = params.get("name_column", "table_name")
        require([field])
        target = params.get("output_column", "表格分类")
        validate_columns([{"key": target}], keys)
        threshold = float(params.get("threshold", .8))
        if not 0 <= threshold <= 1: raise InvalidOperation("相似度应在 0–1 之间")
        groups = []
        for row in rows:
            name = re.sub(r"[\W_]+", "", display(row.get(field))).casefold()
            label = next((label for canonical, label in groups if SequenceMatcher(None, canonical, name).ratio() >= threshold), None)
            if label is None:
                label = display(row.get(field)) or "未命名表"
                groups.append((name, label))
            row[target] = params.get("overrides", {}).get(row["row_id"], label)
        columns.append({"key": target, "name": target, "type": "text"})
    elif operator == "expand":
        field = params.get("table_column", "table_content")
        require([field])
        out["rows"] = []
        added = []
        prefix = params.get("prefix", "数据.")
        for row in rows:
            for i, record in enumerate(nested_records(row.get(field))):
                values = {prefix + k: v for k, v in record.items()}
                if set(values) & (keys | RESERVED): raise InvalidOperation("展开字段与原字段重名，请设置前缀")
                for key in values:
                    if key not in added: added.append(key)
                sources = row[field].get("row_sources", [])
                provenance = sources[i] if i < len(sources) else {"input_row_id": row["row_id"], "table_row": i, "source": row.get("provenance")}
                out["rows"].append(row | values | {"row_id": f"{row['row_id']}:{i}", "provenance": provenance})
        columns.extend({"key": k, "name": k, "type": "text"} for k in added)
    elif operator == "merge":
        group, field = params.get("group_column"), params.get("table_column", "table_content")
        require([group, field])
        groups = {}
        for row in rows:
            label = display(row.get(group))
            entry = groups.setdefault(label, {"row_id": row["row_id"], group: label, field: {"rows": [], "row_sources": []}, "provenance": []})
            records = nested_records(row.get(field))
            entry[field]["rows"].extend(records)
            entry[field]["row_sources"].extend({"input_row_id": row["row_id"], "table_row": i, "source": row.get("provenance")} for i in range(len(records)))
            entry["provenance"].append({"input_row_id": row["row_id"], "source": row.get("provenance")})
        out["rows"] = list(groups.values())
        out["columns"] = [c for c in columns if c["key"] in (group, field)]
    elif operator == "join":
        if lookup is None: raise InvalidOperation("请上传对照表")
        left, right = params.get("left_key"), params.get("right_key")
        require([left])
        if right not in {c["key"] for c in lookup["columns"]}: raise InvalidOperation("对照表关联键不存在")
        prefix = params.get("prefix", "对照.")
        new = [{**c, "key": prefix + c["key"], "name": prefix + c["name"]} for c in lookup["columns"] if c["key"] != right]
        validate_columns(new, keys)
        index = {}
        for row in lookup["rows"]:
            key = display(row.get(right))
            if not key: continue
            if key in index: raise InvalidOperation(f"对照表关联键重复：{key}，请先去重")
            index[key] = row
        for row in rows:
            matched = index.get(display(row.get(left)), {})
            row.update({c["key"]: matched.get(c["key"][len(prefix):]) for c in new})
        columns.extend(new)
    elif operator == "edit":
        changes = params.get("changes", [])
        index = {r["row_id"]: r for r in rows}
        for change in changes:
            if change.get("row_id") not in index or change.get("column") not in keys:
                raise InvalidOperation("修改行或列不存在")
            kind = next(c["type"] for c in columns if c["key"] == change["column"])
            index[change["row_id"]][change["column"]] = cast(change.get("value"), kind)
    else: raise InvalidOperation("未知表格算子")
    if len(out["rows"]) > 100000 or len(out["columns"]) > 200:
        raise InvalidOperation("结果超过 100000 行或 200 列，请缩小范围")
    return out
