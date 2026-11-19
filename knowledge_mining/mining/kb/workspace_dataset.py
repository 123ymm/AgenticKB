# -*- coding: utf-8 -*-
"""KB 知识制品工作台数据集信封契约（67号工作台 MVP Task 4）。

数据集（dataset）= 步骤流算子间流动的唯一载荷形态：

``{"kind": "table", "cols": [...], "rows": [{"_id", "_source", <cols 子集>}]}``

- MVP 唯一合法 kind 是 ``table``（``documents``/``relations`` 是 Task 4+
  预留形态，出现即拒）；``kind`` 缺省视为 ``table``（Task 3 时代的数据集
  不带 kind，兼容读）。
- 行级来源贯穿（67号 §3.1）：每行必带 ``_id``（行身份，非空 str）与
  ``_source``（行来源 str）；下游算子原样继承或显式组合（join 合并双方
  ``_source``）。
- 行字段键 ⊆ cols∪{_id,_source}：**多余字段**是契约违规（越界列），
  **缺列键与 None 值**合法（行可以只覆盖部分列）。

护栏：``MAX_ROWS``=5 万行，与 db_workspace_runs.MAX_ROWS_PER_PORT 同值
（write_outputs 逐 port 拒整批的同一口径）；``MAX_SERIALIZED_BYTES``=20MB
（content_inline 落库前的序列化预算）。``dataset_hash`` 用 canonical json
（ensure_ascii=False / sort_keys=True / 紧凑分隔符，rows 含 _id/_source/
全字段）的 sha256——executor 层经 write_outputs(hashes=...) 落
kb_workspace_run_outputs.content_hash（Task 6 接线，形参已就位）。

错误类不另起炉灶：复用登记层 WorkspaceValidationError（db_workspaces）。
本模块纯函数、无 DB 依赖。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from knowledge_mining.mining.kb.db_workspaces import WorkspaceValidationError

__all__ = [
    "MAX_ROWS",
    "MAX_SERIALIZED_BYTES",
    "TRIAL_ROWS",
    "dataset_hash",
    "merge_quality",
    "serialized_size_ok",
    "slice_for_trial",
    "validate_dataset",
]

#: 单数据集行数上限（对齐 db_workspace_runs.MAX_ROWS_PER_PORT）。
MAX_ROWS = 50_000

#: 单数据集序列化字节预算（canonical json 编码后 ≤ 20MB 才可落 content_inline）。
MAX_SERIALIZED_BYTES = 20 * 1024 * 1024

#: 试跑（trial）切片行数——不建 run 的快看口径（67号 §4.1）。
TRIAL_ROWS = 3

_ROW_META_FIELDS = ("_id", "_source")


def _canonical_json(ds: Any) -> str:
    """hash/序列化预算共用的 canonical 形态：键序无关、紧凑、非 ASCII 原样。"""
    return json.dumps(ds, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def validate_dataset(ds: Any) -> None:
    """数据集契约校验（违规 → WorkspaceValidationError）：

    ds 为 dict；kind（缺省 table）必须为 table；cols 非空列表、元素非空
    str 且无重复；rows 为列表且 ≤ MAX_ROWS；每行为 dict，必带 _id（非空
    str）与 _source（str）；行字段键 ⊆ cols∪{_id,_source}（值 None 合法）。
    """
    if not isinstance(ds, dict):
        raise WorkspaceValidationError("dataset must be an object")
    if ds.get("kind", "table") != "table":
        raise WorkspaceValidationError(
            f"dataset.kind must be 'table' (documents/relations are"
            f" reserved): {ds.get('kind')!r}")
    cols = ds.get("cols")
    if not isinstance(cols, list) or not cols:
        raise WorkspaceValidationError("dataset.cols must be a non-empty list")
    col_set: set[str] = set()
    for col in cols:
        if not isinstance(col, str) or not col:
            raise WorkspaceValidationError(
                f"dataset.cols entries must be non-empty strings: {col!r}")
        if col in col_set:
            raise WorkspaceValidationError(f"duplicate column: {col!r}")
        col_set.add(col)
    rows = ds.get("rows")
    if not isinstance(rows, list):
        raise WorkspaceValidationError("dataset.rows must be a list")
    if len(rows) > MAX_ROWS:
        raise WorkspaceValidationError(
            f"dataset.rows exceeds limit {MAX_ROWS}: {len(rows)}")
    allowed = col_set | set(_ROW_META_FIELDS)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkspaceValidationError(
                f"dataset.rows[{i}] must be an object")
        rid = row.get("_id")
        if not isinstance(rid, str) or not rid:
            raise WorkspaceValidationError(
                f"dataset.rows[{i}]._id must be a non-empty string: {rid!r}")
        if not isinstance(row.get("_source"), str):
            raise WorkspaceValidationError(
                f"dataset.rows[{i}]._source must be a string:"
                f" {row.get('_source')!r}")
        for key in row:
            if key not in allowed:
                raise WorkspaceValidationError(
                    f"dataset.rows[{i}] has field outside cols:"
                    f" {key!r}")


def dataset_hash(ds: Any) -> str:
    """canonical json 的 sha256 hex（64 位小写十六进制）。

    纯变换不校验契约（调用方 run_operator 已对输出逐个 validate）；
    值/_id/_source/行数/列序任何变化都会改变哈希。"""
    return hashlib.sha256(_canonical_json(ds).encode("utf-8")).hexdigest()


def serialized_size_ok(ds: Any) -> bool:
    """canonical json 编码字节数 ≤ MAX_SERIALIZED_BYTES（落库预算判定）。"""
    return len(_canonical_json(ds).encode("utf-8")) <= MAX_SERIALIZED_BYTES


def slice_for_trial(ds: dict[str, Any], n: int = TRIAL_ROWS) -> dict[str, Any]:
    """试跑切片：整体深拷贝后取前 ``n`` 行（负数截为 0），原件不可变。"""
    out = copy.deepcopy(ds)
    rows = out.get("rows")
    out["rows"] = list(rows)[:max(n, 0)] if isinstance(rows, list) else rows
    return out


def merge_quality(*reports: Any) -> dict[str, Any]:
    """质量账合并（多 port / 多节点聚合口径）：

    - 数值键求和（bool 不算数值）；
    - 列表键拼接；
    - 标量/异型键后者胜；
    - None 值与 None 报告整份跳过；
    - 返回新 dict，不别名任何入参列表。
    """
    merged: dict[str, Any] = {}
    for report in reports:
        if report is None:
            continue
        if not isinstance(report, dict):
            raise WorkspaceValidationError(
                f"quality report must be an object: {report!r}")
        for key, value in report.items():
            if value is None:
                continue
            if key not in merged:
                merged[key] = copy.deepcopy(value)
                continue
            prev = merged[key]
            if _is_number(prev) and _is_number(value):
                merged[key] = prev + value
            elif isinstance(prev, list) and isinstance(value, list):
                merged[key] = [*prev, *value]
            else:
                merged[key] = copy.deepcopy(value)  # 标量覆盖：后者胜
    return merged


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
