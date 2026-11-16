# -*- coding: utf-8 -*-
"""KB 知识制品工作台 LLM 逐行抽取算子（67号工作台 MVP Task 7）。

``llm_extract`` 是注册表里唯一 ``kind="llm"`` 的算子：**不经
workspace_operators.run_operator 的同步 run**（注册条目 run=None，
run_operator 对其显式拒绝），由 executor 按 kind 分派到本模块的
``run_full``（正式跑）/``run_trial``（试跑）。

机制要点：

- **逐行一任务**：每行渲染 prompt（``{列名}`` 占位，``_SafeDict`` 未命中
  保留原文——零依赖的 format_map）后经 ``LlmTaskClient.submit_chat`` 提交
  llm_service 异步任务通道（``expected_output_type="json_object"``），
  幂等键 ``wsop:{run_id}:{node_id}:{行_id}``（重试轮加 ``:retry``；试跑
  ``wstrial:{node_id}:{行_id}:{config_hash}``——config_hash 含 prompt+
  输出字段集指纹，跨配置试跑不撞旧任务）。
- **LlmTaskClient 是同步阻塞客户端**（httpx.Client）——与 wiki_service
  同款口径：async 侧一律 ``asyncio.to_thread`` 包装，不在事件循环上阻塞。
  ctx 由 executor 构造注入（生产 ``LlmTaskClient(base_url=
  MiningConfig().llm_service_url)``，照 kb/deps._get_task_client 构造式），
  测试注入 fake。
- **B.4 完整性**：失败行**保留原值+新列 None**，不冒充完整——quality 账
  ``{total, succeeded, failed_row_ids, retried, partial}`` 明示残缺；全失败
  抛 WorkspaceValidationError（节点失败语义，走 executor 分支 partial/failed）。
- **失败子集重试一次**：首轮任一失败态（failed/dead_letter/timeout/缺键/
  坏 JSON/cast 失败）的行以 ``:retry`` 幂等键重提一轮；仍败记 failed_row_ids
  （试跑不重试——单轮 ≤60s 等待）。
- **输出 cast 复用 artifact_import 的 _cast_value**（61号 Excel 导入同款
  六列型口径 text/int/float/date/enum/bool；包内私有复用先例：
  workspace_sources 复用 artifact_import 私有四件套）。

提交失败（LlmTaskError）上抛不做行级重试——llm_service 整体不可达属节点
级故障，由 executor 节点级兜底记账（与 wiki embedding「提交失败保持
pending」不同源：本算子无 pending 态可保持）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from knowledge_mining.mining.kb.db_workspaces import WorkspaceValidationError
from knowledge_mining.mining.kb.services.artifact_import_service import (
    _cast_value,
)
from knowledge_mining.mining.kb.workspace_dataset import (
    slice_for_trial,
    validate_dataset,
)

__all__ = [
    "LlmOperatorContext",
    "TRIAL_WAIT_SECONDS",
    "run_full",
    "run_trial",
]

#: 试跑单轮等待上限（秒）——正式跑用客户端默认总超时（900s）。
TRIAL_WAIT_SECONDS = 60.0

#: 输出字段合法类型词表（61号 Excel 导入六列型同款）。
_FIELD_TYPES = ("text", "int", "float", "date", "enum", "bool")


@dataclass(frozen=True)
class LlmOperatorContext:
    """llm 算子执行上下文：``llm`` 持 LlmTaskClient（或测试桩——duck-typed
    submit_chat/wait_for_tasks），executor 构造注入。"""

    llm: Any


class _SafeDict(dict):
    """format_map 守护字典：未命中键回填 ``{原占位符}`` 保留原文。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_prompt(template: str, row: dict[str, Any]) -> str:
    """逐行渲染 prompt：``{列名}`` 用行字段替换（_id/_source 不参与，值非
    str 先 str()）；未命中占位符保留原文。模板结构性错误（裸 ``{`` / 位置
    槽 ``{}``）→ WorkspaceValidationError（确定性配置错，非行数据错）。"""
    values = {key: value if isinstance(value, str) else str(value)
              for key, value in row.items()
              if key not in ("_id", "_source")}
    try:
        return template.format_map(_SafeDict(values))
    except (IndexError, ValueError, KeyError, AttributeError) as exc:
        raise WorkspaceValidationError(
            f"llm_extract: prompt_template 渲染失败: {exc}") from exc


def _config_hash(
    prompt_template: str, output_fields: list[dict[str, Any]],
) -> str:
    """试跑幂等键的配置指纹：sha256(prompt_template + output_fields 按 name
    排序的 canonical json) 前 8 hex——prompt 或字段名/类型任一变化即换键
    （跨配置试跑不撞旧任务、不误复用旧结果），字段顺序无关。"""
    canonical = json.dumps(
        [prompt_template,
         sorted(output_fields, key=lambda f: str(f["name"]))],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------ config 校验


def _validate_config(
    config: Any, dataset: Any,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """config 深校验（注册表 config_schema 只做顶层 json-schema-lite，元素
    级语义在此），返回 (prompt_template, output_fields, model)：

    - input_column 必填非空 str 且 ∈ 输入列（抽取主体列须存在——同 filter
      的 field 门禁口径；模板仍可引用任意列）；
    - prompt_template 必填非空 str，且空表渲染通过（结构性格式错误在此拦
      下，不拖到逐行）；
    - output_fields 必填 list ≥1，元素 {name: 非空 str 唯一, type ∈ 六列
      型}；name 不得撞输入列与 _id/_source（输出列唯一性）；
    - model 可选非空 str。"""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise WorkspaceValidationError(
            "llm_extract: config must be an object")
    cols = dataset.get("cols") if isinstance(dataset, dict) else None
    col_set: set[str] = set(cols) if isinstance(cols, list) else set()

    input_column = config.get("input_column")
    if not isinstance(input_column, str) or not input_column:
        raise WorkspaceValidationError(
            "llm_extract: config.input_column must be a non-empty string")
    if input_column not in col_set:
        raise WorkspaceValidationError(
            "llm_extract: input_column not in input columns:"
            f" {input_column!r}")

    template = config.get("prompt_template")
    if not isinstance(template, str) or not template:
        raise WorkspaceValidationError(
            "llm_extract: config.prompt_template must be a non-empty string")
    try:
        template.format_map(_SafeDict({}))  # 结构性格式预检
    except (IndexError, ValueError, AttributeError) as exc:
        raise WorkspaceValidationError(
            f"llm_extract: prompt_template 非法: {exc}") from exc

    fields_in = config.get("output_fields")
    if not isinstance(fields_in, list) or not fields_in:
        raise WorkspaceValidationError(
            "llm_extract: config.output_fields must be a non-empty list")
    fields: list[dict[str, Any]] = []
    names: set[str] = set()
    for i, entry in enumerate(fields_in):
        if not isinstance(entry, dict):
            raise WorkspaceValidationError(
                f"llm_extract: output_fields[{i}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise WorkspaceValidationError(
                f"llm_extract: output_fields[{i}].name must be a non-empty"
                " string")
        if name in names:
            raise WorkspaceValidationError(
                f"llm_extract: duplicate output field: {name!r}")
        if name in col_set or name in ("_id", "_source"):
            raise WorkspaceValidationError(
                "llm_extract: output field must not shadow input column"
                f" or reserved field: {name!r}")
        ftype = entry.get("type")
        if ftype not in _FIELD_TYPES:
            raise WorkspaceValidationError(
                f"llm_extract: output_fields[{i}].type must be one of"
                f" {list(_FIELD_TYPES)}: {ftype!r}")
        names.add(name)
        fields.append({"name": name, "type": ftype})

    model = config.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise WorkspaceValidationError(
            "llm_extract: config.model must be a non-empty string or absent")
    return template, fields, model


# ------------------------------------------------------------ 行结果解析


def _row_values(
    entry: dict[str, Any], fields: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """单行结果解析+cast：终态非 succeeded / result 缺 / 文本非 JSON / 非
    dict JSON / 缺输出键 / cast 失败 → None（行 failed）；否则返回
    {字段名: cast 后值}（output_fields 全量校验过——部分可用也不算成）。

    llm_service 对 json_object 任务可直返 ``result.parsed_output``（dict）
    ——优先取；否则 ``result.text_output`` 文本自行 json.loads（规格主路）。"""
    if str(entry.get("status")) != "succeeded":
        return None
    result = entry.get("result")
    result = result if isinstance(result, dict) else {}
    parsed = result.get("parsed_output")
    if not isinstance(parsed, dict):
        parsed = None
        text = result.get("text_output")
        if isinstance(text, str):
            try:
                loaded = json.loads(text)
            except ValueError:
                return None
            parsed = loaded if isinstance(loaded, dict) else None
    if parsed is None:
        return None
    values: dict[str, Any] = {}
    for field in fields:
        name = str(field["name"])
        if name not in parsed:
            return None
        ok, value = _cast_value(parsed[name], str(field["type"]))
        if not ok:
            return None
        values[name] = value
    return values


# ------------------------------------------------------------ 提交-等待-重试


async def _execute_rows(
    ctx: LlmOperatorContext,
    rows: list[dict[str, Any]],
    template: str,
    fields: list[dict[str, Any]],
    model: str | None,
    *,
    domain: str,
    first_key: Callable[[str], str],
    retry_key: Callable[[str], str] | None,
    wait_timeout: float | None,
) -> tuple[dict[int, dict[str, Any]], int]:
    """逐行提交（to_thread——LlmTaskClient 同步阻塞）→ wait_for_tasks →
    （retry_key 非 None 时）失败子集以重试键再提一轮；返回 (行序号→已校验
    字段值, 进入重试的行数)。提交失败上抛（LlmTaskError——llm_service 整体
    不可达是节点级故障，不做行级吸收）。"""
    if not rows:
        return {}, 0
    tasks: dict[int, str] = {}
    for i, row in enumerate(rows):
        task_id = await _submit_row(
            ctx, row, template, model, domain,
            idempotency_key=first_key(str(row["_id"])))
        tasks[i] = task_id
    entries = await asyncio.to_thread(
        ctx.llm.wait_for_tasks, list(tasks.values()), timeout=wait_timeout)
    values: dict[int, dict[str, Any]] = {}
    for i, tid in tasks.items():
        row_values = _row_values(entries.get(tid) or {}, fields)
        if row_values is not None:
            values[i] = row_values

    retried = 0
    if retry_key is not None:
        failed_idx = [i for i in range(len(rows)) if i not in values]
        if failed_idx:
            retried = len(failed_idx)
            retry_tasks: dict[int, str] = {}
            for i in failed_idx:
                row = rows[i]
                task_id = await _submit_row(
                    ctx, row, template, model, domain,
                    idempotency_key=retry_key(str(row["_id"])))
                retry_tasks[i] = task_id
            entries = await asyncio.to_thread(
                ctx.llm.wait_for_tasks, list(retry_tasks.values()),
                timeout=wait_timeout)
            for i, tid in retry_tasks.items():
                row_values = _row_values(entries.get(tid) or {}, fields)
                if row_values is not None:
                    values[i] = row_values
    return values, retried


async def _submit_row(
    ctx: LlmOperatorContext, row: dict[str, Any],
    template: str, model: str | None, domain: str, *,
    idempotency_key: str,
) -> str:
    """单行提交：渲染 prompt → submit_chat(json_object)。返回 task_id。"""
    messages = [{"role": "user", "content": _render_prompt(template, row)}]
    return await asyncio.to_thread(
        ctx.llm.submit_chat, messages,
        expected_output_type="json_object",
        idempotency_key=idempotency_key,
        model=model, knowledge_domain=domain)


def _assemble(
    dataset: dict[str, Any], fields: list[dict[str, Any]],
    values: dict[int, dict[str, Any]], retried: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """组装输出与质量账：输出=输入全列+新字段列（列序=输入列序+声明序）；
    失败行保留原值+新列 None（B.4 完整性——不冒充完整，quality 明示）。"""
    cols: list[str] = list(dataset.get("cols", []))
    rows = dataset.get("rows", [])
    new_names = [str(f["name"]) for f in fields]
    rows_out: list[dict[str, Any]] = []
    failed_row_ids: list[str] = []
    succeeded = 0
    for i, row in enumerate(rows):
        out: dict[str, Any] = {"_id": row["_id"], "_source": row["_source"]}
        for col in cols:
            out[col] = row.get(col)
        row_values = values.get(i)
        if row_values is None:
            failed_row_ids.append(str(row["_id"]))
            for name in new_names:
                out[name] = None
        else:
            succeeded += 1
            out.update(row_values)
        rows_out.append(out)
    quality = {
        "total": len(rows),
        "succeeded": succeeded,
        "failed_row_ids": failed_row_ids,
        "retried": retried,
        "partial": bool(failed_row_ids),
    }
    return ({"main": {"kind": "table", "cols": [*cols, *new_names],
                      "rows": rows_out}},
            quality)


def _reject_all_failed(op_label: str, quality: dict[str, Any]) -> None:
    """全失败 → WorkspaceValidationError（节点失败语义；空表 total=0 不算
    失败——无行可失败）。"""
    if quality["total"] and quality["succeeded"] == 0:
        raise WorkspaceValidationError(
            f"{op_label}: 全部 {quality['total']} 行抽取失败"
            f"（failed_row_ids={quality['failed_row_ids']}）")


# ------------------------------------------------------------ 两入口


async def run_full(
    ctx: LlmOperatorContext, config: Any, dataset: Any, *,
    domain: str, run_id: str, node_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """正式跑（executor 分派）：逐行提交（幂等键 ``wsop:{run_id}:{node_id}:
    {行_id}``）→ 等待（客户端默认总超时 900s）→ 失败子集重试一轮（键加
    ``:retry``）→ 组装。返回 (outputs, quality)。"""
    template, fields, model = _validate_config(config, dataset)
    validate_dataset(dataset)
    rows: list[dict[str, Any]] = dataset.get("rows", [])
    values, retried = await _execute_rows(
        ctx, rows, template, fields, model, domain=domain,
        first_key=lambda rid: f"wsop:{run_id}:{node_id}:{rid}",
        retry_key=lambda rid: f"wsop:{run_id}:{node_id}:{rid}:retry",
        wait_timeout=None)
    outputs, quality = _assemble(dataset, fields, values, retried)
    _reject_all_failed("llm_extract", quality)
    return outputs, quality


async def run_trial(
    ctx: LlmOperatorContext, config: Any, dataset: Any, *,
    domain: str, node_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """试跑（executor 分派；不落库由 executor 保证）：前 3 行
    （slice_for_trial）单轮等待 ≤ TRIAL_WAIT_SECONDS、不重试；幂等键
    ``wstrial:{node_id}:{行_id}:{config_hash}``。返回 (outputs, quality,
    trial_meta={input_rows: 切片后实数})。"""
    template, fields, model = _validate_config(config, dataset)
    validate_dataset(dataset)
    sliced = slice_for_trial(dataset)
    rows: list[dict[str, Any]] = sliced.get("rows", [])
    config_hash = _config_hash(template, fields)
    values, _ = await _execute_rows(
        ctx, rows, template, fields, model, domain=domain,
        first_key=lambda rid: f"wstrial:{node_id}:{rid}:{config_hash}",
        retry_key=None, wait_timeout=TRIAL_WAIT_SECONDS)
    outputs, quality = _assemble(sliced, fields, values, 0)
    _reject_all_failed("llm_extract", quality)
    return outputs, quality, {"input_rows": quality["total"]}
