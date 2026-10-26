# 知识制品 Pipeline 工作台：数据库表与字段说明

更新日期：2026-10-26

配套需求：[知识制品 Pipeline 工作台需求文档](./知识制品-Pipeline工作台-需求文档.md)。本文按当前代码说明使用的表、关键字段、读写时机及关联方式，不是数据库迁移方案或完整数据字典。

## 1. 总体边界

- 本次没有新增数据库表或字段，也不修改 schema。
- 来源表只读；步骤定义、执行记录、快照引用使用现有工作流及存储表。
- 完整中间表格 JSON 保存在 MinIO，不保存在 `asset_storage_objects` 的某个正文列中。
- 普通加工及发布预览不写正式制品行、列和版本；用户确认发布后复用现有制品服务写入。
- 制品自身创建和成员管理仍可在发布前发生，因此不能理解为发布前完全不写 `kb_artifacts`。
- `parent_run_id`、`step_index`、`operator`、`output` 等是已有 JSON 列中的键，不是新增物理列。

### 数据库位置

| 数据 | 使用的数据库连接 |
| --- | --- |
| 来源资产、工作流定义、存储对象、制品及权限 | 主数据库连接 `pool` |
| `mining_runs`、`mining_workflow_node_events` | 对应业务域的运行库连接 `runtime_pool` |
| `agent_llm_*` | LLM 服务自身的数据库连接 |

本地部署可能共库，不能假设各环境都能跨这些表直接 JOIN。跨库排查应先查询引用 ID，再到对应库查询。

## 2. 来源读取：5 张核心表

这些表在工作台抽取链路中只读，不更新源文档和源表格。

| 表 | 相关字段 | 用途 |
| --- | --- | --- |
| `asset_documents` | `id`、`kb_id`、`domain`、`document_name`、`directory_path`、`content_revision`、`source_raw_hash`、`deleted_at` | 确认所选文件、路径及当前版本，排除已删除文件 |
| `asset_document_snapshot_links` | `document_id`、`document_snapshot_id`、`linked_at` | 查找文档关联的解析快照 |
| `asset_document_snapshots` | `id`、`domain`、`lifecycle_status`、`raw_content_hash`、`parse_ir_storage_object_id` | 查找同域 READY 快照并取得解析 JSON 引用 |
| `asset_structured_assets` | `snapshot_id`、`asset_type`、`table_ref`、`columns_json`、`row_count`、`sheet_name`、`readiness` | 查找快照中的表格及列定义，仅取 `asset_type='table'` |
| `asset_table_cells` | `snapshot_id`、`table_ref`、`row_index`、`column_index`、`column_name`、`value`、`is_header` | 读取单元格，按行列顺序整理业务表格 |

`asset_table_cells` 还包含 `value_type`、`normalized_value`、`formula`、`row_span`、`column_span`、`source_span_id` 等源结构信息；当前新格式投影主要使用坐标、值和表头标记，不将这些字段全部放入展示 JSON。

来源选择逻辑：若文档存在 `source_raw_hash`，要求解析快照的 `raw_content_hash` 与其匹配；按关联时间选择可用快照。文档未记录该摘要时，现有逻辑不执行摘要匹配过滤。

```text
asset_documents.id
  → asset_document_snapshot_links.document_id
  → document_snapshot_id = asset_document_snapshots.id
      ├─ parse_ir_storage_object_id → asset_storage_objects.id → MinIO Parse IR
      └─ id = asset_structured_assets.snapshot_id / asset_table_cells.snapshot_id
                 通过 table_ref 对应同一张表
```

完整表名及文件正文读取同一 Parse IR 的元素、标题关联和正文，不直接依赖某个数据库“表名”列。整理后的 `table_content` 写入工作台快照，不回写源单元格表。

## 3. 步骤定义与运行：4 张表

### 3.1 `mining_workflows`

每个加工步骤创建自己的工作流记录；外部对照表也会使用工作流元数据登记引用。

| 字段 | 用途 |
| --- | --- |
| `id` | 工作流 ID，被运行记录引用 |
| `name` | 内部工作流名称 |
| `draft_graph_json` | 当前步骤定义 |
| `current_version` | 固定定义后指向版本 |
| `created_by` | 创建用户 |
| `metadata_json` | 制品、业务域、步骤关系等元数据；发布时追加 `publications` |
| `updated_at` | 版本推进及元数据更新所用时间字段 |

`metadata_json.publications` 按发布方案摘要记录发布结果，用于查询发布历史和防止重复发布。

### 3.2 `mining_workflow_versions`

| 字段 | 用途 |
| --- | --- |
| `id`、`workflow_id`、`version` | 标识所属工作流及版本，每个新步骤固定自己的版本 |
| `graph_json`、`compiled_manifest_json` | 固定算子、参数、来源和输入快照引用 |
| `graph_hash` | 定义摘要，用于完整性检查 |
| `schema_version` | manifest 格式版本 |
| `operator_catalog_version` | 表格算子目录版本，例如 `artifact-table@1` |
| `created_by`、`metadata_json` | 提交用户及步骤元数据 |

算子参数位于 manifest 的 `nodes[].params` 中，例如 `input_object_id`、`sources`、`filters`、`keep_columns`、`drop_columns`、对照表引用、模型提示词及新列定义。后端由合法父步骤派生输入快照引用，不直接信任客户端传入的对象 ID。

### 3.3 `mining_runs`

| 字段 | 用途 |
| --- | --- |
| `id` | 独立步骤运行 ID，前端用它查看结果 |
| `input_path` | 制品逻辑来源，例如 `artifact://制品ID` |
| `domain`、`channel`、`execution_engine` | 业务域、`artifact_table` 通道及 `workflow` 执行类型 |
| `workflow_id`、`workflow_version`、`workflow_version_id` | 关联固定的步骤定义 |
| `workflow_graph_hash`、`workflow_manifest_json` | 固定定义及摘要 |
| `status`、`current_stage` | 排队、执行、完成、失败、取消等状态 |
| `started_at`、`finished_at`、`error_summary` | 时间及失败摘要 |
| `worker_id`、`lease_until` | 执行者与租约，阻止过期执行者覆盖结果 |
| `active_node_id`、`active_operator_type` | 当前单节点及算子 |
| `metadata_json` | 步骤父子关系、输出快照、LLM 进度等 |

当前入队时不把来源知识库写到物理列 `mining_runs.kb_id`，而是写入 `metadata_json.kb_id`，避免与原知识库挖掘运行约束混用。`preflight_manifest_json` 由通用入队接口支持，当前表格步骤未提供该内容。

典型 `metadata_json`（示意值，`progress` 按任务类型出现）：

```json
{
  "profile": "artifact_table",
  "artifact_id": "制品ID",
  "user_id": "用户ID",
  "kb_id": "来源知识库ID",
  "domain": "业务域",
  "parent_run_id": "父步骤运行ID",
  "step_index": 3,
  "operator": "llm_columns",
  "output": {
    "storage_object_id": "结果快照对象ID",
    "sha256": "摘要",
    "row_count": 100,
    "issue_count": 0
  },
  "progress": {
    "done": 100,
    "total": 100,
    "task_id": "模型任务ID",
    "checkpoint": "检查点对象ID"
  }
}
```

起始步骤没有父步骤。相同 `parent_run_id` 可以有多个子步骤。界面 step2、step3 来自 `step_index`；每个单节点工作流内部的 `node_id` 仍为 `step1`，两者含义不同。

### 3.4 `mining_workflow_node_events`

| 字段 | 用途 |
| --- | --- |
| `id`、`run_id`、`node_id` | 事件 ID、所属运行和节点 |
| `operator_type`、`operator_version` | 实际执行的算子及版本 |
| `status`、`attempt_no` | 节点事件状态和执行尝试次数 |
| `started_at`、`finished_at`、`duration_ms` | 执行时间及耗时 |
| `input_summary_json` | 输入摘要，当前记录 `manifest_run_id` |
| `output_summary_json` | 输出对象 ID、摘要、行数及问题数 |
| `error_code`、`error_message` | 中断、取消等错误码及错误信息 |

执行恢复保留旧尝试并新建事件。普通表格步骤不创建逐文档的 `mining_run_documents` 记录，也不进入原文档 Build 收尾流程。

## 4. 快照定位：`asset_storage_objects`

| 字段 | 用途 |
| --- | --- |
| `id` | 工作台引用的存储对象 ID |
| `provider` | 例如 `minio` |
| `bucket`、`object_key` | 实际对象定位 |
| `object_version_id` | 对象存储版本标识，可空；不是 Pipeline 步骤号 |
| `sha256`、`size` | 内容校验和大小限制 |
| `mime` | 工作台快照为 `application/json` |
| `etag` | 存储服务返回的标识，不代替 SHA-256 |
| `artifact_class` | 工作台复用 `binary_asset` |
| `state` | 读取要求 `AVAILABLE` |
| `created_at` | 对象登记时间 |

`last_verified_at`、`encryption`、`retention_until` 是表中既有字段，不是本次工作台步骤状态字段；当前快照注册未显式提供这些值。

```text
mining_runs.metadata_json.output.storage_object_id
  → asset_storage_objects.id
  → bucket + object_key + object_version_id
  → MinIO 完整 JSON
```

LLM 检查点使用 `metadata_json.progress.checkpoint` 引用同一张存储对象表。外部对照表也使用同一套存储能力。

快照路径为 `artifact-tables/v1/...`，内容包含 `schema_version`、`columns`、`rows`、`documents`、`issues`，按需增加 `preview` 和行级 `_llm`、`_llm_history`。这些是文件中的 JSON 键，不是数据库列。

该表也登记 `source`、`parse_ir`、`backend_raw` 等其他对象，不能将每条记录都当作一个工作台步骤。内容寻址可复用相同对象，不能假设运行记录与存储对象严格一对一。

## 5. 制品权限与正式发布：5 张核心表

| 表 | 相关字段 | 读写时机 |
| --- | --- | --- |
| `kb_artifacts` | `artifact_id`、`domain_id`、`kind`、`title`、`description`、`usage_note`、`owner_user_id`、`visibility`、`status`、`current_revision`、`draft_open`、`created_at`、`updated_at` | 工作台读制品信息；新建制品、更新说明、确认发布时写入 |
| `kb_artifact_members` | `artifact_id`、`user_id`、`role` | 读取 owner/editor/viewer 权限；新建制品时登记成员 |
| `kb_table_columns` | `artifact_id`、`column_id`、`column_name`、`column_type`、`enum_values`、`is_primary_key`、`is_indexed`、`alias`、`status`、`ordinal` | 读取已有目标结构；确认发布时按原服务规则写入字段定义 |
| `kb_table_rows` | `artifact_id`、`revision`、`row_key`、`data`、`provenance`、`conflicts`、`ordinal`、`updated_at`、`updated_by` | 检查冲突；确认发布时写入业务行 |
| `kb_artifact_revisions` | `artifact_id`、`revision`、`schema_json`、`row_count`、`change_summary`、`published_by`、`published_at` | 生成正式版本及结构快照 |

### 正式行的 JSON 分工

- `data`：发布后的业务字段和值。
- `provenance`：`channel=pipeline`、`run_id`、`row_id`、源表信息、模型信息、历史模型信息及快照引用。
- `conflicts`：质量/冲突标记，当前 Pipeline 准备发布时初始化为 `{}`。

发布复用既有草稿 `revision=0` 写入和正式版本生成逻辑；外层事务统一包住写行、生成版本及工作流发布记录，失败共同回滚。

`kb_artifact_batches` 不存 Pipeline 中间步骤，保留给原 Excel/知识库导入链路。`mcp_key_open_artifacts` 属于现有 Agent/MCP 开放授权，本次步骤加工及发布不自动改写开放范围。

## 6. 权限与知识库基础表

| 表 | 关键字段 | 用途 |
| --- | --- | --- |
| `knowledge_bases` | `id`、`domain`、`status` 等既有库信息 | 来源存在性、业务域及可访问性检查 |
| `kb_members` | `kb_id`、`user_id`、`role` | 来源库成员权限 |
| `kb_users` | `id`、`site_role`、`status` | 用户身份及有效性 |
| `user_domains` | `user_id`、`domain` | 域访问权限 |

本次加工服务不为步骤新增权限记录。登录身份同步、知识库列表、目录浏览和挖掘状态展示继续复用原功能，其完整依赖范围不等同于本文的核心步骤存储清单。

## 7. LLM 加列间接使用的表

工作台通过 `LlmTaskClient` 调用 LLM 服务，不直接操作下列表。

| 表 | 相关字段 | 用途 |
| --- | --- | --- |
| `agent_llm_tasks` | `id`、`task_type`、`caller_service`、`knowledge_domain`、`pipeline_stage`、`idempotency_key`、`status`、`priority`、`available_at`、`attempt_count`、`max_attempts`、`metadata_json`、`created_at`、`updated_at` | 模型任务、幂等及状态 |
| `agent_llm_requests` | `id`、`task_id`、`provider`、`model`、`prompt_template_key`、`messages_json`、`input_json`、`params_json`、`expected_output_type`、`output_schema_json`、`created_at` | 固定模型请求 |
| `agent_llm_attempts` | `id`、`task_id`、`request_id`、`attempt_no`、`status`、`raw_output_text`、`raw_response_json`、`error_type`、`error_message`、token 统计字段、`latency_ms`、`started_at`、`finished_at` | 每次模型调用及排错信息 |
| `agent_llm_results` | `id`、`task_id`、`attempt_id`、`parse_status`、`parsed_output_json`、`text_output`、`parse_error`、`validation_errors_json`、`created_at` | 模型结果及结构校验信息 |
| `agent_llm_events` | `id`、`task_id`、`event_type`、`message`、`metadata_json`、`created_at` | 提交、领取、重试、成功、失败等事件 |

`agent_llm_attempts` 的 token 统计列为 `prompt_tokens`、`completion_tokens`、`total_tokens`。接口返回的 `parsed_output` 对应服务封装后的解析结果，数据库列名是 `parsed_output_json`。

一个加列步骤可以产生多个按行提交的模型任务。其 `task_id` 会进入快照行的 `_llm`，连续加工的旧信息进入 `_llm_history`，发布时带入正式行 `provenance`。

## 8. 只读排查 SQL

### 8.1 查看一个制品的步骤及快照引用（域运行库）

```sql
SELECT id, status, workflow_id,
       metadata_json->>'parent_run_id' AS parent_run_id,
       metadata_json->>'step_index' AS step_index,
       metadata_json->>'operator' AS operator,
       metadata_json->'output'->>'storage_object_id' AS storage_object_id,
       metadata_json->'progress' AS progress,
       error_summary
FROM mining_runs
WHERE metadata_json->>'profile' = 'artifact_table'
  AND metadata_json->>'artifact_id' = '替换为制品ID'
ORDER BY started_at, id;
```

### 8.2 定位 MinIO 对象（主库）

```sql
SELECT id, provider, bucket, object_key, object_version_id,
       sha256, size, mime, artifact_class, state
FROM asset_storage_objects
WHERE id = '替换为上一步查到的storage_object_id';
```

### 8.3 查询发布记录和正式版本（主库）

```sql
SELECT id, metadata_json->'publications' AS publications
FROM mining_workflows
WHERE metadata_json->>'profile' = 'artifact_table'
  AND metadata_json->>'artifact_id' = '替换为工作台所属制品ID';

SELECT artifact_id, revision, schema_json, row_count,
       change_summary, published_by, published_at
FROM kb_artifact_revisions
WHERE artifact_id = '替换为发布目标制品ID'
ORDER BY revision;
```

### 8.4 查询模型结果（LLM 运行库）

```sql
SELECT task_id, parse_status, parsed_output_json, parse_error
FROM agent_llm_results
WHERE task_id = '替换为快照中记录的模型任务ID';
```

以上查询均只读。预览不写正式行、列或版本，但可能读取并锁定相关记录；不能以是否发生 SELECT 判断是否发布。

## 9. 对应代码

- [步骤与来源仓储](../knowledge_mining/mining/kb/artifact_pipeline_repository.py)
- [加工服务与快照读写](../knowledge_mining/mining/kb/services/artifact_pipeline_service.py)
- [发布桥接](../knowledge_mining/mining/kb/services/artifact_pipeline_publish.py)
- [既有制品数据库访问](../knowledge_mining/mining/kb/db_artifacts.py)
- [工作流定义仓储](../knowledge_mining/mining/workflow/repositories/global_workflow_repository.py)
- [运行仓储](../knowledge_mining/mining/workflow/repositories/domain_run_repository.py)
- [LLM 任务服务](../llm_service/runtime/service.py)
- [对象存储表定义](../databases/asset_core/schemas/008_object_storage_foundation_postgresql.sql)
- [制品表定义](../databases/kb/schemas/017_kb_artifacts.sql)
- [LLM 运行表定义](../databases/agent_llm_runtime/schemas/002_agent_llm_runtime_postgresql.sql)
