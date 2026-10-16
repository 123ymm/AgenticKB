# Databases

本目录统一放 CoreMasterKB 的数据库契约。

## 目录结构

```text
databases/
  asset_core/         # Mining 写、Serving 读的知识资产库
  mining_runtime/     # Mining 自身运行态与断点续跑状态库
  agent_llm_runtime/  # 独立 LLM 服务运行态库
  kb/                 # KB 身份与知识域表（用户/库/成员/域/MCP 钥匙/访问账本/知识制品）
  ontology/           # 本体概念层（FK 指向 asset_* / mining_runs，DDL 必须最后执行）
  migrations/         # 存量库版本化迁移（manifest.yaml + core/*.sql，与启动链同文的 DDL 双份）
```

## 不在本目录的表：serving 自有表

`agent_serving_java` 自己读写、自己建表的几张表**不放这里**，DDL 在
`agent_serving_java/src/main/resources/db/` 下：

| 表 | DDL | 建表时机 |
|---|---|---|
| `operator_paradigm` / `operator_paradigm_version` | `db/operator/001_operator_paradigm.sql` | `ParadigmSchemaInitializer`，启动时建在非路由的 `defaultDataSource` 上 |
| `knowledge_access_records` | `databases/kb/schemas/015_knowledge_access_records.sql` | versioned database upgrade/bootstrap |
| `serving_query_cache` | `db/serving/002_serving_query_cache.sql` | 同上 |

后两张表走 `@Primary` 的 `DomainRoutingDataSource`，所以**每个域会写进自己的库**
（`domain_registry.yaml` 里那个域的 `database:` 块）。因此它们不能由 `reset_db.py` 建——
那个脚本只连 `.env` 指向的单个库，域一旦分库就覆盖不到。改为
`DomainPoolManager` 每次建池时调 `DomainSchemaEnsurer` 补齐。

`db_tables.py` 把这几张表列进 `OPTIONAL_TABLES`，所以 Java 从未启动过的纯挖掘库上
`export_db.py` / `import_db.py` 会跳过它们。

## kb/：知识制品六张表（61号表格知识制品 MVP）

启动链 DDL：`kb/schemas/017_kb_artifacts.sql`；存量库版本化迁移：
`migrations/core/20261016_007_kb_artifacts.sql`（两份内容同文，改表必须双改并刷新
manifest 三处镜像校验和——`contract.py` `CURRENT_SCHEMA_CHECKSUM` 与
`ServingRuntimeSchemaInitializer.java` `EXPECTED_SCHEMA_CHECKSUM`）。

| 表 | 职责 |
|---|---|
| `kb_artifacts` | 制品头：挂 `domain_id`（与库权限解绑，策展开放），kind=table/wiki，status=draft/published/deprecated，`current_revision`+`draft_open` 指向草稿/已发布轨 |
| `kb_artifact_revisions` | 发布版本账：每版 `schema_json` 快照 + 行数 + 摘要（修订号 0 保留给草稿轨） |
| `kb_artifact_members` | 制品协作者：owner/editor/viewer（策展 ACL，与 KB 成员无关） |
| `kb_artifact_batches` | 数据进入批次账：channel=upload/kb_import，来源文件/sheet/行数审计 |
| `kb_table_columns` | 列定义：类型 CHECK（text/int/float/date/enum/bool）+ enum_values + 别名 + active/dropped |
| `kb_table_rows` | 行数据：行地址=业务主键 `row_key`（ordinal 仅展示）；jsonb 分工 `data`=业务值 / `provenance`=来源账 / `conflicts`=质量标记，永不混装 |

身份列（`domain_id` / `owner_user_id` / `artifact_id` 等）故意不建强外键——对齐 kb 表
惯例：跨表身份用 TEXT 引用，由服务层保证一致。

## 当前决定

### 1. 三个库逻辑上分开

当前正式设计不合并为一个总库。

原因：

1. `asset_core` 是稳定知识资产 + build/release 控制面。
2. `mining_runtime` 是挖掘过程状态，带有大量中间态、失败态、重试态。
3. `agent_llm_runtime` 是独立服务的任务队列、请求、attempt、结果和审计日志。

三者职责不同、生命周期不同、读写模式也不同。

### 2. Mining 涉及的两个 DB 不合并为正式设计

Mining 同时会碰到：

- `asset_core`
- `mining_runtime`

这两个库**逻辑上必须分开**，不建议把它们定义成一个正式数据库。

原因：

1. Serving 只应该看到发布后的资产，不应该看到 Mining 运行态噪音表。
2. `mining_runtime` 的写入频率、失败重试、清理策略和 `asset_core` 完全不同。
3. `asset_core` 现在承载的是 `document / shared snapshot / build / release` 主链，需要保持稳定。

### 3. 物理层面的折中

如果将来为了本地开发方便，短期把 `asset_core` 和 `mining_runtime` 放进**同一个 SQLite 文件**，可以接受，但只应视为：

```text
dev / 单机调试便利
```

不能把这种做法当成正式架构基线，也不能影响表命名和职责边界。

## 当前推荐

| 数据库 | 正式建议 |
|---|---|
| `asset_core` | 单独数据库 |
| `mining_runtime` | 单独数据库 |
| `agent_llm_runtime` | 单独数据库，由 claude-llm 独立维护 |

如果只看 1.1 当前阶段，最稳妥的结论就是：

```text
逻辑分库必须坚持；
物理合库只允许作为 dev 便利，不作为正式设计。
```

## 当前统一主链

当前三库合起来的统一主链是：

```text
source_batch
  -> mining_run
  -> document
  -> shared snapshot
  -> document_snapshot_link
  -> raw_segments / relations / retrieval_units
  -> build
  -> release
  -> serving
```

其中：

- `mining_runtime` 负责“怎么生产出来”
- `asset_core` 负责“生产出了什么，以及哪些正式生效”
- `agent_llm_runtime` 负责“LLM 在这个过程中做了什么”
