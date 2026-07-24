# Mining Runtime Schema

本目录定义 Mining 运行态数据库契约。

## 作用

`mining_runtime` 不存发布后的知识资产，只存 Mining 自身的执行状态。

它解决的问题是：

1. 一次 run 处理到哪一步了。
2. 某个文档是 `NEW / UPDATE / SKIP`。
3. 某个文档在哪个阶段失败了。
4. 如何做断点续跑和阶段统计。
5. 哪个 run 最终产出了哪个 build。

## 表

| 表 | 作用 |
|---|---|
| `mining_runs` | 一次挖掘执行 |
| `mining_run_documents` | 某次 run 中每个文档的处理状态 |
| `mining_run_stage_events` | 阶段事件流，既支持文档级，也支持 run 级 |

## 和 `asset_core` 的关系

`mining_runtime` 不是 Serving 的读取入口。

关系是：

```text
source_batch
  -> mining_run
  -> document/snapshot/segments/relations/retrieval_units
  -> build
  -> release
  -> serving
```

其中：

- `mining_run_documents.committed` 只表示文档级稳定内容对象已经落地
- 不表示该内容已经进入 active release
- run 级阶段如 `assemble_build / validate_build / publish_release` 通过 `mining_run_stage_events.run_id` 追溯

## 是否和 `asset_core` 合并

当前结论是不合并为正式设计。

原因：

1. `asset_core` 面向 Serving，要求稳定、干净、可发布。
2. `mining_runtime` 面向挖掘过程，天然包含大量中间态和失败态。
3. 两者生命周期、清理策略、读写模式不同。
