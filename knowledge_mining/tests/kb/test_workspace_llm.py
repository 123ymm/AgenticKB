# -*- coding: utf-8 -*-
"""LLM 逐行抽取算子（67号工作台 MVP Task 7）——纯宿主 fake client 测试。

不打真库、不打真 llm_service：``FakeLlmTaskClient`` 按幂等键回放可编程
终态（succeeded 带 result / failed / timeout），记录 submit 全参供断言。
执行器真库接线（fake llm_factory 全区跑）在 test_workspace_executor.py。

覆盖：
- 注册表条目：kind=llm、run=None、run_operator 对其明确拒绝（不走同步执行）。
- 模板渲染：占位符命中 / 未命中保留原文 / 非 str 值 str() / _id·_source 不参与。
- run_full 全成：输出=输入全列+新列、int/enum cast、quality 全账、submit 传参
  （expected_output_type=json_object、幂等键 wsop:{run}:{node}:{row}、
  knowledge_domain、model）。
- 部分失败重试：首轮失败子集二次提交（幂等键加 :retry）→ 二轮成功全成；
  两轮都败 → 该行原值+新列 None+failed_row_ids+partial；retried 计数。
- 缺键 / 非法 JSON / 非 dict JSON → 行 failed（含重试后仍败）。
- 全失败 → WorkspaceValidationError。
- run_trial：前 3 行切片、幂等键 wstrial:{node}:{row}:{config_hash}、单轮
  等待 timeout=60、无重试、trial_meta input_rows=切片实数。
- config 校验：output_fields 空列表/非法类型/重名、input_column 不在列、
  模板格式非法、新列撞输入列/_id/_source。
"""
from __future__ import annotations

import re

import pytest

from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.workspace_llm_operator import (
    LlmOperatorContext,
    _config_hash,
    _render_prompt,
    run_full,
    run_trial,
)
from knowledge_mining.mining.kb.workspace_operators import (
    OPERATOR_DEFS,
    run_operator,
)

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------ fake client


class FakeLlmTaskClient:
    """三态可编程桩：submit_chat 登记幂等键，wait_for_tasks 按剧本回放。

    - ``outcomes``：幂等键 → 终态 entry；未编程的键用 ``default_outcome``
      （缺省 None → 回放 failed——测试漏编即显式失败，不静默当成成功；
      执行器真库用例用 default_outcome 覆盖「run_id 不可预知」的全键成功）。
    - ``submits``：每次 submit 的全参记录（断言幂等键形态/传参用）。
    - ``waits``：每次 wait_for_tasks 的 (task_ids, timeout) 记录。
    """

    def __init__(self, *, default_outcome: dict | None = None) -> None:
        self.outcomes: dict[str, dict] = {}
        self.default_outcome = default_outcome
        self.submits: list[dict] = []
        self.waits: list[dict] = []
        self._tasks: dict[str, str] = {}
        self._n = 0

    def submit_chat(
        self, messages, *, expected_output_type=None, idempotency_key=None,
        pipeline_stage="mining_enrichment", max_attempts=3, priority=100,
        model=None, knowledge_domain=None,
    ):
        self._n += 1
        task_id = f"t{self._n}"
        self.submits.append({
            "task_id": task_id,
            "messages": messages,
            "expected_output_type": expected_output_type,
            "idempotency_key": idempotency_key,
            "max_attempts": max_attempts,
            "model": model,
            "knowledge_domain": knowledge_domain,
        })
        self._tasks[task_id] = idempotency_key
        return task_id

    def wait_for_tasks(self, task_ids, *, timeout=None, poll_interval=None):
        self.waits.append({"task_ids": list(task_ids), "timeout": timeout})
        out: dict[str, dict] = {}
        for tid in task_ids:
            key = self._tasks.get(tid, "")
            entry = self.outcomes.get(key) or self.default_outcome
            out[tid] = (dict(entry, task_id=tid) if entry
                        else {"task_id": tid, "status": "failed"})
        return out

    @staticmethod
    def ok(key: str, payload_text: str) -> tuple[str, dict]:
        """编程一个 succeeded 终态（result.text_output=payload_text）。"""
        return key, {
            "status": "succeeded",
            "result": {"text_output": payload_text, "parse_status": "ok"},
        }

    @staticmethod
    def failed(key: str) -> tuple[str, dict]:
        return key, {
            "status": "failed",
            "error": {"error_type": "provider", "error_message": "boom"},
        }


def _ctx(fake: FakeLlmTaskClient) -> LlmOperatorContext:
    return LlmOperatorContext(llm=fake)


def _dataset(rows: int = 3) -> dict:
    """行数可裁的产品表（B.3 材料变体，加一列非 str 值供渲染断言）。"""
    all_rows = [
        {"_id": "p1", "_source": "产品表.xlsx/Sheet1", "型号": "P1",
         "功率_W": 3000},
        {"_id": "p2", "_source": "产品表.xlsx/Sheet1", "型号": "P2",
         "功率_W": 2000},
        {"_id": "p3", "_source": "产品表.xlsx/Sheet1", "型号": "P3",
         "功率_W": 1200},
        {"_id": "p4", "_source": "产品表.xlsx/Sheet1", "型号": "P4",
         "功率_W": 500},
        {"_id": "p5", "_source": "产品表.xlsx/Sheet1", "型号": "P5",
         "功率_W": 100},
    ]
    return {"kind": "table", "cols": ["型号", "功率_W"],
            "rows": all_rows[:rows]}


def _config(**over) -> dict:
    config = {
        "input_column": "型号",
        "prompt_template": "从下文抽取标签：{型号}",
        "output_fields": [{"name": "标签", "type": "enum"},
                          {"name": "分数", "type": "int"}],
    }
    config.update(over)
    return config


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["_id"]: r for r in rows}


# ------------------------------------------------------------ 注册表


async def test_registry_entry_and_sync_gate() -> None:
    """llm_extract 注册：kind=llm、arity=1、run=None；run_operator 拒绝。"""
    entry = OPERATOR_DEFS["llm_extract"]
    assert entry["kind"] == "llm"
    assert entry["input_arity"] == 1
    assert entry["run"] is None
    assert entry["ports"][0].id == "main"
    schema = entry["config_schema"]
    for field in ("input_column", "prompt_template", "output_fields"):
        assert schema[field]["required"] is True
    assert "model" in schema
    with pytest.raises(WorkspaceValidationError, match="llm"):
        run_operator("llm_extract", _config(), [_dataset()])


# ------------------------------------------------------------ 模板渲染


def test_render_prompt_placeholder_semantics() -> None:
    """命中替换 / 未命中保留原文 / 非 str 先 str() / _id·_source 不参与。"""
    row = {"_id": "p1", "_source": "s", "型号": "P1", "功率_W": 3000,
           "备注": None}
    out = _render_prompt("{型号} 功率 {功率_W} 瓦，备注 {备注}，"
                         "未知 {不存在的列}，身份 {_id}", row)
    assert out == ("P1 功率 3000 瓦，备注 None，"
                   "未知 {不存在的列}，身份 {_id}")


def test_render_prompt_bad_template_rejected() -> None:
    """模板格式非法（裸 { / 位置槽 {} / 属性穿越 {a.b}）→ Validation。"""
    with pytest.raises(WorkspaceValidationError, match="prompt_template"):
        _render_prompt("裸开 { 结束", {"型号": "P1"})
    with pytest.raises(WorkspaceValidationError, match="prompt_template"):
        _render_prompt("位置槽 {}", {"型号": "P1"})
    with pytest.raises(WorkspaceValidationError, match="prompt_template"):
        _render_prompt("属性穿越 {型号.功率}", {"型号": "P1"})


# ------------------------------------------------------------ run_full


async def test_run_full_all_succeed() -> None:
    """3 行全成：输出=输入列+新列（int/enum cast）、quality 全账、传参钉。"""
    fake = FakeLlmTaskClient()
    for rid, tag in (("p1", "高端"), ("p2", "主流"), ("p3", "入门")):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}", f'{{"标签": "{tag}", "分数": "88"}}')
        fake.outcomes[key] = entry
    outputs, quality = await run_full(
        _ctx(fake), _config(rows=3), _dataset(rows=3),
        domain="dom-1", run_id="run-1", node_id="lx")

    main = outputs["main"]
    assert main["cols"] == ["型号", "功率_W", "标签", "分数"]
    rows = _by_id(main["rows"])
    assert rows["p1"]["标签"] == "高端"      # enum：字符串 strip
    assert rows["p1"]["分数"] == 88          # int："88" cast 成 88
    assert rows["p2"]["型号"] == "P2"        # 原列原值保留
    assert quality == {"total": 3, "succeeded": 3, "failed_row_ids": [],
                       "retried": 0, "partial": False}

    assert len(fake.submits) == 3
    first = fake.submits[0]
    assert first["idempotency_key"] == "wsop:run-1:lx:p1"
    assert first["expected_output_type"] == "json_object"
    assert first["knowledge_domain"] == "dom-1"
    assert first["model"] is None
    assert first["messages"] == [
        {"role": "user", "content": "从下文抽取标签：P1"}]
    # 全成不发起第二轮等待
    assert len(fake.waits) == 1


async def test_run_full_model_passthrough() -> None:
    """config.model 显式传入 submit（None 不带——客户端缺省）。"""
    fake = FakeLlmTaskClient()
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p1", '{"标签": "x", "分数": 1}')
    fake.outcomes[key] = entry
    await run_full(_ctx(fake), _config(model="glm-4.7"), _dataset(rows=1),
                   domain="dom-1", run_id="run-1", node_id="lx")
    assert fake.submits[0]["model"] == "glm-4.7"


async def test_run_full_retry_recovers_second_round() -> None:
    """第 2 行首轮 failed、重试 succeeded → 全成；幂等键带 :retry 后缀。"""
    fake = FakeLlmTaskClient()
    for rid, tag in (("p1", "高端"), ("p3", "入门")):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}", f'{{"标签": "{tag}", "分数": 1}}')
        fake.outcomes[key] = entry
    fake.outcomes["wsop:run-1:lx:p2"] = FakeLlmTaskClient.failed(
        "wsop:run-1:lx:p2")[1]
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p2:retry", '{"标签": "主流", "分数": 2}')
    fake.outcomes[key] = entry

    outputs, quality = await run_full(
        _ctx(fake), _config(rows=3), _dataset(rows=3),
        domain="dom-1", run_id="run-1", node_id="lx")
    rows = _by_id(outputs["main"]["rows"])
    assert rows["p2"]["标签"] == "主流"
    assert quality == {"total": 3, "succeeded": 3, "failed_row_ids": [],
                       "retried": 1, "partial": False}
    assert len(fake.waits) == 2              # 首轮 + 重试轮
    assert fake.submits[3]["idempotency_key"] == "wsop:run-1:lx:p2:retry"


async def test_run_full_double_failure_row_kept_with_none() -> None:
    """某行两轮都败 → 原值+新列 None+failed_row_ids+partial；retried 计数。"""
    fake = FakeLlmTaskClient()
    for rid in ("p1", "p3"):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}", f'{{"标签": "标签{rid}", "分数": 1}}')
        fake.outcomes[key] = entry
    for key in ("wsop:run-1:lx:p2", "wsop:run-1:lx:p2:retry"):
        fake.outcomes[key] = FakeLlmTaskClient.failed(key)[1]

    outputs, quality = await run_full(
        _ctx(fake), _config(rows=3), _dataset(rows=3),
        domain="dom-1", run_id="run-1", node_id="lx")
    rows = _by_id(outputs["main"]["rows"])
    assert rows["p2"]["型号"] == "P2"         # 失败行保留原值（B.4 完整性）
    assert rows["p2"]["标签"] is None and rows["p2"]["分数"] is None
    assert rows["p1"]["标签"] == "标签p1"
    assert quality == {"total": 3, "succeeded": 2,
                       "failed_row_ids": ["p2"], "retried": 1,
                       "partial": True}


async def test_run_full_timeout_and_dead_letter_failed() -> None:
    """timeout / dead_letter 终态 → 行 failed（进重试；重试成功算成）。"""
    fake = FakeLlmTaskClient()
    fake.outcomes["wsop:run-1:lx:p1"] = {
        "status": "timeout", "task_id": "x"}
    fake.outcomes["wsop:run-1:lx:p2"] = {"status": "dead_letter", "task_id": "y"}
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p3", '{"标签": "入门", "分数": 3}')
    fake.outcomes[key] = entry
    # 两行重试均成功
    for rid, tag in (("p1", "高端"), ("p2", "主流")):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}:retry", f'{{"标签": "{tag}", "分数": 9}}')
        fake.outcomes[key] = entry

    _, quality = await run_full(
        _ctx(fake), _config(rows=3), _dataset(rows=3),
        domain="dom-1", run_id="run-1", node_id="lx")
    assert quality["succeeded"] == 3
    assert quality["retried"] == 2
    assert quality["partial"] is False


async def test_run_full_missing_key_and_bad_json() -> None:
    """succeeded 但缺输出键 / 非法 JSON / 非 dict JSON → 行 failed（重试
    一轮仍残缺；p4/p5 成功锚——全失败会抛，见独立用例）。"""
    fake = FakeLlmTaskClient()
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p1", '{"标签": "高端"}')       # 缺 分数 键
    fake.outcomes[key] = entry
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p2", '不是 JSON')              # 非法 JSON
    fake.outcomes[key] = entry
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p3", '[1, 2]')                 # JSON 但非 dict
    fake.outcomes[key] = entry
    # 重试轮同样三种残缺（钉「缺键/坏 JSON 也走重试一次」）
    for rid, text in (("p1", '{"标签": "高端"}'), ("p2", "坏"),
                      ("p3", '"str"')):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}:retry", text)
        fake.outcomes[key] = entry
    # 成功锚（p4/p5）：避免全失败触发节点级抛错，观察失败行输出形态
    for rid in ("p4", "p5"):
        key, entry = FakeLlmTaskClient.ok(
            f"wsop:run-1:lx:{rid}", '{"标签": "锚", "分数": 1}')
        fake.outcomes[key] = entry

    outputs, quality = await run_full(
        _ctx(fake), _config(rows=5), _dataset(rows=5),
        domain="dom-1", run_id="run-1", node_id="lx")
    rows = _by_id(outputs["main"]["rows"])
    for rid in ("p1", "p2", "p3"):
        assert rows[rid]["标签"] is None and rows[rid]["分数"] is None
    assert rows["p4"]["标签"] == "锚"
    assert quality == {"total": 5, "succeeded": 2,
                       "failed_row_ids": ["p1", "p2", "p3"], "retried": 3,
                       "partial": True}


async def test_run_full_cast_failure_fails_row() -> None:
    """int 字段给了不可转值 → cast 失败 → 行 failed（新列 None；p2 成功锚
    避免全失败抛错）。"""
    fake = FakeLlmTaskClient()
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p1", '{"标签": "高端", "分数": "不是数"}')
    fake.outcomes[key] = entry
    fake.outcomes["wsop:run-1:lx:p1:retry"] = FakeLlmTaskClient.failed(
        "wsop:run-1:lx:p1:retry")[1]
    key, entry = FakeLlmTaskClient.ok(
        "wsop:run-1:lx:p2", '{"标签": "主流", "分数": 2}')
    fake.outcomes[key] = entry

    outputs, quality = await run_full(
        _ctx(fake), _config(rows=2), _dataset(rows=2),
        domain="dom-1", run_id="run-1", node_id="lx")
    rows = _by_id(outputs["main"]["rows"])
    assert rows["p1"]["分数"] is None
    assert rows["p2"]["分数"] == 2
    assert quality == {"total": 2, "succeeded": 1, "failed_row_ids": ["p1"],
                       "retried": 1, "partial": True}


async def test_run_full_parsed_output_shortcut() -> None:
    """llm_service 直返 parsed_output（dict）时优先取——真实服务行为。"""
    fake = FakeLlmTaskClient()
    fake.outcomes["wsop:run-1:lx:p1"] = {
        "status": "succeeded",
        "result": {"parsed_output": {"标签": "高端", "分数": 7},
                   "parse_status": "ok"},
    }
    outputs, quality = await run_full(
        _ctx(fake), _config(rows=1), _dataset(rows=1),
        domain="dom-1", run_id="run-1", node_id="lx")
    assert _by_id(outputs["main"]["rows"])["p1"]["分数"] == 7
    assert quality["succeeded"] == 1


async def test_run_full_all_failed_raises() -> None:
    """全失败 → WorkspaceValidationError（节点失败语义）。"""
    fake = FakeLlmTaskClient()
    for rid in ("p1", "p2"):
        for key in (f"wsop:run-1:lx:{rid}", f"wsop:run-1:lx:{rid}:retry"):
            fake.outcomes[key] = FakeLlmTaskClient.failed(key)[1]
    with pytest.raises(WorkspaceValidationError, match="llm_extract"):
        await run_full(_ctx(fake), _config(rows=2), _dataset(rows=2),
                       domain="dom-1", run_id="run-1", node_id="lx")


async def test_run_full_zero_rows_ok() -> None:
    """空表输入：零提交、零行输出、partial=False（空表不是失败）。"""
    fake = FakeLlmTaskClient()
    outputs, quality = await run_full(
        _ctx(fake), _config(rows=0), _dataset(rows=0),
        domain="dom-1", run_id="run-1", node_id="lx")
    assert outputs["main"]["cols"] == ["型号", "功率_W", "标签", "分数"]
    assert outputs["main"]["rows"] == []
    assert quality == {"total": 0, "succeeded": 0, "failed_row_ids": [],
                       "retried": 0, "partial": False}
    assert fake.submits == []


# ------------------------------------------------------------ config 校验


async def test_run_full_config_validation() -> None:
    """output_fields 空/元素畸形/类型词表外/重名、input_column 不在列、
    模板非法、新列撞输入列与保留字段 → Validation。"""
    fake = FakeLlmTaskClient()
    ds = _dataset(rows=1)
    ctx = _ctx(fake)
    cases = [
        _config(output_fields=[]),
        _config(output_fields=[{"name": "标签"}]),               # 缺 type
        _config(output_fields=[{"name": "标签", "type": "string"}]),  # 词表外
        _config(output_fields=[{"name": "标签", "type": "int"},
                               {"name": "标签", "type": "text"}]),  # 重名
        _config(output_fields=[{"type": "text"}]),               # 缺 name
        _config(input_column="不存在的列"),
        _config(prompt_template="裸 { 坏"),
        _config(output_fields=[{"name": "型号", "type": "text"}]),  # 撞输入列
        _config(output_fields=[{"name": "_id", "type": "text"}]),  # 撞保留字段
        {"input_column": "型号", "prompt_template": "x"},        # 缺 output_fields
    ]
    for config in cases:
        with pytest.raises(WorkspaceValidationError):
            await run_full(ctx, config, ds, domain="dom-1",
                           run_id="run-1", node_id="lx")
    assert fake.submits == []                 # 校验失败零提交


# ------------------------------------------------------------ run_trial


async def test_run_trial_slices_three_and_key_shape() -> None:
    """前 3 行切片；幂等键 wstrial:{node}:{row}:{config_hash}（8hex）；
    单轮等待 timeout=60；无重试。"""
    fake = FakeLlmTaskClient()
    config_hash = _config_hash(_config()["prompt_template"],
                               _config()["output_fields"])
    assert re.fullmatch(r"[0-9a-f]{8}", config_hash)
    for rid in ("p1", "p2", "p3"):
        key, entry = FakeLlmTaskClient.ok(
            f"wstrial:lx:{rid}:{config_hash}",
            '{"标签": "试", "分数": 1}')
        fake.outcomes[key] = entry

    outputs, quality, meta = await run_trial(
        _ctx(fake), _config(), _dataset(rows=5),
        domain="dom-1", node_id="lx")
    assert meta == {"input_rows": 3}
    assert len(outputs["main"]["rows"]) == 3
    assert quality["total"] == 3 and quality["succeeded"] == 3
    assert len(fake.submits) == 3             # p4/p5 不提交
    for sub in fake.submits:
        assert sub["idempotency_key"].startswith(f"wstrial:lx:p")
        assert sub["idempotency_key"].endswith(f":{config_hash}")
    assert len(fake.waits) == 1               # 单轮，无重试
    assert fake.waits[0]["timeout"] == 60.0


async def test_run_trial_short_input_rows() -> None:
    """短输入（2 行）→ input_rows=2、提交 2 行。"""
    fake = FakeLlmTaskClient()
    config_hash = _config_hash(_config()["prompt_template"],
                               _config()["output_fields"])
    for rid in ("p1", "p2"):
        key, entry = FakeLlmTaskClient.ok(
            f"wstrial:lx:{rid}:{config_hash}", '{"标签": "试", "分数": 1}')
        fake.outcomes[key] = entry

    _, _, meta = await run_trial(_ctx(fake), _config(), _dataset(rows=2),
                                 domain="dom-1", node_id="lx")
    assert meta == {"input_rows": 2}
    assert len(fake.submits) == 2


async def test_run_trial_failure_no_retry() -> None:
    """试跑失败行不重试（单轮）：失败行新列 None+partial=True。"""
    fake = FakeLlmTaskClient()
    config_hash = _config_hash(_config()["prompt_template"],
                               _config()["output_fields"])
    key, entry = FakeLlmTaskClient.ok(
        f"wstrial:lx:p1:{config_hash}", '{"标签": "试", "分数": 1}')
    fake.outcomes[key] = entry
    fake.outcomes[f"wstrial:lx:p2:{config_hash}"] = {
        "status": "timeout", "task_id": "x"}
    fake.outcomes[f"wstrial:lx:p3:{config_hash}"] = FakeLlmTaskClient.failed(
        f"wstrial:lx:p3:{config_hash}")[1]

    outputs, quality, meta = await run_trial(
        _ctx(fake), _config(), _dataset(rows=5),
        domain="dom-1", node_id="lx")
    rows = _by_id(outputs["main"]["rows"])
    assert rows["p1"]["分数"] == 1
    assert rows["p2"]["标签"] is None and rows["p3"]["分数"] is None
    assert quality == {"total": 3, "succeeded": 1,
                       "failed_row_ids": ["p2", "p3"], "retried": 0,
                       "partial": True}
    assert len(fake.submits) == 3             # 超时行也不二次提交
    assert len(fake.waits) == 1


async def test_run_trial_all_failed_raises() -> None:
    """试跑全失败 → 同样 WorkspaceValidationError。"""
    fake = FakeLlmTaskClient()
    config_hash = _config_hash(_config()["prompt_template"],
                               _config()["output_fields"])
    for rid in ("p1", "p2", "p3"):
        fake.outcomes[f"wstrial:lx:{rid}:{config_hash}"] = \
            FakeLlmTaskClient.failed(f"wstrial:lx:{rid}:{config_hash}")[1]
    with pytest.raises(WorkspaceValidationError, match="llm_extract"):
        await run_trial(_ctx(fake), _config(), _dataset(rows=5),
                        domain="dom-1", node_id="lx")


def test_config_hash_stable_and_sensitive() -> None:
    """同 config 同哈希；prompt 或字段集变化 → 哈希变化（试跑键防撞）；
    字段顺序无关（sorted 口径）。"""
    fields = [{"name": "标签", "type": "enum"}]
    h1 = _config_hash("模板A", fields)
    assert h1 == _config_hash("模板A", fields)
    assert h1 != _config_hash("模板B", fields)
    two = [{"name": "标签", "type": "enum"},
           {"name": "分数", "type": "int"}]
    assert h1 != _config_hash("模板A", two)
    assert _config_hash("模板A", two) == _config_hash("模板A", two[::-1])
