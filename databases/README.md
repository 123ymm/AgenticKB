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

### 008 增量（1.1.15 权限模型，随替换包）

启动链 017 已含同构 DDL；存量库走 `migrations/core/20261019_008_artifact_visibility_key_open.sql`
（online_expand）：

- `kb_artifacts.visibility`（private=仅成员 / public=本域全体绑定用户**可读**；
  写权限仍由 members 角色 + owner 决定）。
- `mcp_key_open_artifacts`（钥匙级制品开放子集，对齐 `mcp_key_open_kbs` 模式）：
  public 不自动暴露给 Agent，每把钥匙显式勾选；读时校验开放行 ⊆ 钥匙主人可见面。

## kb/：整库导出任务表（020，打包下载通道）

启动链 DDL：`kb/schemas/020_kb_export_tasks.sql`（原 018——2026-11-10 合并
fzl194/master 时与上游 wiki 制品的 018 撞号让位改号，仅改文件名与启动链引用，
无 ledger 影响）；存量库版本化迁移：
`migrations/core/20261026_010_kb_export_tasks.sql`（online_expand，两份同文；
迁移 id 不变——已按该 id 上过生产 ledger）。

`kb_export_tasks` 照 `kb_purge_tasks` 形制（status/phase/progress_json/error +
lifespan 恢复），补产物列：`artifact_bucket/object_key/size/sha256/doc_count/expires_at`。
产物是 staging 桶里的裸 zip（原始文档 STORED 不压缩 + manifest.json 清单），
**不入 `asset_storage_objects`**——不受引用计数/GC 波及；过期由后台 sweep 删对象
并置 `status='expired'`（行保留供列表回看）。copy_kb 超 5000 文档即引导走本通道。

## kb/：知识制品工作台 7 表（021，67号 MVP）

启动链 DDL：`kb/schemas/021_kb_workspaces.sql`；存量库版本化迁移：
`migrations/core/20261116_012_kb_workspaces.sql`（online_expand，两份同文，
DDL 主体漂移由 `tests/maintenance/test_schema_mirror_sync.py` 钉死）。

语义与决策依据：67号规划 §4.1——模板驱动步骤流工作台的数据底座：

- 登记：`kb_workspaces`（graph_json 设计态草稿 + graph_revision 乐观锁）+
  `kb_workspace_members`（owner/editor/viewer）。
- 来源：`kb_workspace_sources`（connector=kb_dir/excel；`input_manifest` 冻结输入，
  运行可复现的根）。
- 运行：`kb_workspace_runs`（`graph_snapshot_json` 冻结图 + 七态状态机；
  scope=node/workspace/subgraph，trial 试跑不建 run 行）。
- 输出：`kb_workspace_run_outputs`（`is_current` 单活指针——部分唯一索引
  `uq_ws_output_current` 保证同 (workspace,node,port) 至多一行 current；
  `stale_since_run` 记失效来源）。
- 绑定/导出：`kb_workspace_output_bindings`（port→artifact 导出去处）+
  `kb_workspace_exports`（`request_id` 幂等键；mode 现仅 create_draft，
  绝不直接发布）。

时间戳口径=TEXT ISO（同 002/020 任务记录族；017 制品族是 TIMESTAMPTZ，两族不混）。
`actor_kind`/`actor_id` 在登记/运行/导出三表落位——Agent 通道（mcp_key）从第一张
表就带 actor 字段（67号 §0 禁止事项）。表间外键仅限组内（members/sources→
workspaces、run_outputs→runs，均 ON DELETE CASCADE），身份列不建跨域强外键。

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
