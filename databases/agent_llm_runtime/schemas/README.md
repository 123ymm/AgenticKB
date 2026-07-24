# Agent LLM Runtime Schema

本目录定义独立 LLM 服务的运行态数据库契约。

## 作用

`agent_llm_runtime` 用来记录：

1. prompt 模板
2. 逻辑任务
3. 实际请求
4. 多次 attempt
5. 解析结果
6. 状态事件

它不是知识资产库，也不是 Mining 运行态库。

## 表

| 表 | 作用 |
|---|---|
| `agent_llm_prompt_templates` | prompt 模板与 schema |
| `agent_llm_tasks` | 逻辑任务 / 队列表 |
| `agent_llm_requests` | 实际请求体 |
| `agent_llm_attempts` | provider 调用尝试 |
| `agent_llm_results` | 解析结果 |
| `agent_llm_events` | 事件流 |

## 边界

`claude-llm` 独占维护这套库。

Mining 和 Serving 的关系应是：

```text
Mining / Serving
  -> 调用 LLM Runtime client / service
  -> LLM Runtime 写 agent_llm_runtime
```

当前与资产层的主要关联点是：

- `ref_type / ref_id`
- `build_id`
- `release_id`

也就是说，LLM 任务可以弱关联到：

- 某个文档 / snapshot / retrieval unit
- 某个 build
- 某个 release

而不是：

```text
Mining / Serving 直接写自己的私有 LLM 表
```

## 和其他库是否合并

不建议和 `asset_core` 或 `mining_runtime` 合并。

原因：

1. 这是独立服务边界。
2. 它的写入模式是队列和调用审计，不是知识资产。
3. 后续 provider、重试、限流、幂等等逻辑会持续演进，不应污染资产库。
