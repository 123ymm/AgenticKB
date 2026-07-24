# 挖掘 Workflow 灰度发布与回滚手册

## 1. 目标与不可变边界

本手册用于把新提交的挖掘任务从 legacy Pipeline 分阶段切换到 Workflow Runtime。上线过程必须始终满足：

- Workflow 定义是全局的，不带 Domain；`mining_workflows` 与 `mining_workflow_versions` 只由 Control 连接访问。
- Run、冻结 Manifest、节点事件、审核、资产、Build、Release 都是 Domain 维度；一次请求在选择 Domain 后不得跨连接池。
- 发布的 Workflow 版本不可修改。Run 绑定精确的版本、图哈希和完整 Manifest，恢复时不读取全局最新草稿或最新版本。
- `MINING_RUN_SUBMISSION_ENGINE` 只影响新 Run。切换或回滚时，不得更新已有 Run 的 `execution_engine`、Workflow 绑定字段、Manifest、版本行或节点事件。
- Workflow 发布与知识资产发布是两个动作：前者发布可选择的执行定义，后者由具体 Run 完成 Build/Release。

生产切换前，准备独立的预生产 Control 数据库和至少一个预生产 Domain 数据库。测试工具只接受以 `_test` 结尾的库名。

## 2. 发布前基线

1. 记录当前部署版本、Control/Domain 数据库备份点、活跃 legacy Run 数量和当前 active release。
2. 确认 `.env` 保持安全默认：

   ```dotenv
   MINING_RUN_SUBMISSION_ENGINE=legacy
   ```

3. 执行不连接真实数据库的回归：

   ```powershell
   $env:PG_HOST='127.0.0.1'
   $env:PG_DBNAME='codex_baseline_test'
   $env:PG_USER='codex'
   $env:PG_PASSWORD='not-used'
   .\.venv\Scripts\python.exe -m pytest knowledge_mining/tests/test_full_workflow_equivalence.py knowledge_mining/tests/test_mining_workflow_recovery.py -v
   .\.venv\Scripts\python.exe -m pytest knowledge_mining/tests/test_mining_workflow_performance.py -v -m performance
   ```

停止条件：FULL 出现任何未批准业务差异、恢复重复发布、图谱失败后仍产生 Release，或 Workflow 中位耗时超过 legacy 的 1.20 倍。

## 3. 阶段一：Schema 与系统 Catalog

部署代码但保持新提交走 legacy。Mining API 启动时调用 `ensure_primary_schema()`，幂等创建/升级 Control schema；Domain 连接池首次打开时只应用 Domain schema。启动生命周期还会幂等创建和发布 `system-full-baseline`。

如需在维护窗口提前应用 Control schema，可在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -c "from knowledge_mining.mining.infra.pg_config import MiningDbConfig; from knowledge_mining.mining.infra.pg_schema import ensure_primary_schema; ensure_primary_schema(MiningDbConfig())"
```

冒烟查询：

```sql
SELECT id, current_version, is_system_default, status
FROM mining_workflows
WHERE id = 'system-full-baseline';

SELECT workflow_id, version, graph_hash, schema_version, operator_catalog_version
FROM mining_workflow_versions
WHERE workflow_id = 'system-full-baseline'
ORDER BY version;
```

成功标准：恰好一个 `is_system_default = true`；FULL 当前版本存在、含 16 个节点；Domain schema 有 `mining_workflow_node_events`，且 Domain 库没有 `mining_workflows`。

停止条件：DDL 失败、系统默认不唯一、Domain 出现全局定义表，或旧 Run/Serving 查询回归失败。

回滚：保持 `legacy`，回退应用版本。新增列和表是向后兼容结构，不删除、不改写已有数据。

## 4. 阶段二：管理 API 与编排 UI

开放“挖掘 Workflow”页面和管理 API，但上传页仍按 `legacy` 模式隐藏选择器。验证：从四个模板创建、编辑参数/连线、草稿自动保存、乐观锁冲突保留本地 JSON、服务端校验、发布、历史预览、恢复成新草稿、复制、归档；系统默认不可归档。

成功指标：管理 API 5xx 为 0；发布冲突返回确定的冲突响应；已发布版本内容和哈希不变化；列表、编辑、版本预览不触发 Domain 切换。

停止条件：发布版本可被覆盖、两个用户的草稿互相覆盖、系统默认可删除/归档，或管理页面读取 Domain 资产库。

回滚：隐藏 Workflow 导航和管理入口；保留已发布定义，不删除版本。

## 5. 阶段三：Runtime 暗部署

继续保持 `MINING_RUN_SUBMISSION_ENGINE=legacy`。在预生产使用受保护的双数据库验收：

```powershell
$env:PG_DBNAME='kb_control_test'
$env:MINING_TEST_DOMAIN_PG_DBNAME='kb_plant_a_test'
$env:KB_RUN_POSTGRES_ACCEPTANCE='1'
$env:KB_ALLOW_TEST_TRUNCATE='1'
.\.venv\Scripts\python.exe -m pytest knowledge_mining/tests/test_mining_workflow_postgres.py -v -m postgres
```

这组测试会建表并可清理数据，只能指向两个不同且以 `_test` 结尾的可丢弃数据库。不要把生产凭据或生产库名用于该命令。

验证点：全局定义与 Domain Run 不跨池；发布并发只有一个成功；历史版本不可变；Run 仅从冻结 Manifest 执行；已提交文档跳过，未提交文档从 parse 重试；审核恢复只重跑未完成节点；图谱写失败不进入 finalize；完成后的恢复不会生成第二个 Release。

成功指标：节点事件的 `(run_id, node_id, run_document_id, attempt_no)` 唯一；无跨 Domain 行；恢复后的 Workflow 版本/哈希不变；没有孤立 active release。

停止条件：任何跨池写入、Manifest 漂移、重复 Release、已提交文档重写或未提交事务残留。

回滚：保持新提交走 legacy，停止 Workflow worker；已创建的 workflow 测试 Run 保留供诊断，不改写为 legacy。

## 6. 阶段四：预生产 FULL 与自定义 Workflow

在预生产设置 `MINING_RUN_SUBMISSION_ENGINE=workflow`，重启 Mining 服务后执行四个场景：

1. 旧格式请求只传 `input_path`，不传 Workflow：绑定 `system-full-baseline.current_version` 并完成发布。
2. 新格式请求传 `upload_batch_id` 和自定义 Workflow 精确版本：执行该冻结版本。
3. 无 active ontology 的 Domain：上传页提示但允许提交，本体支线记为 `not_applicable`，仍可发布非本体资产。
4. 有 active ontology 的 Domain：实体审核先于本体审核；图谱成功后才允许 Build/Release。

每个 Run 记录以下不含秘密的证据：

| run_id | domain | execution_engine | workflow_id@version | graph_hash | final_status | build_id | active_release_id |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

成功标准：FULL 与 legacy 规范化业务输出完全一致，`APPROVED_EQUIVALENCE_DELTAS` 为空；100 文档基准中位耗时比不超过 1.20；活跃 `DocumentState` 高水位不超过 `max_workers` 加合流所需常数空间。

停止条件：任何未批准差异、审核顺序错误、发布前图谱失败未阻断、性能或内存越界。

回滚：把新提交模式恢复成 `legacy` 并重启服务。已经存在的 workflow Run 仍由 Workflow Runtime 恢复，不能改成 legacy。

## 7. 阶段五：生产配置切换

只有阶段一至四全部通过并留存证据后，才在部署环境设置：

```dotenv
MINING_RUN_SUBMISSION_ENGINE=workflow
```

重启 Mining 服务，先放量到内部用户或受控 Domain。观察至少一个完整“上传批次 → 选择 Workflow → 执行 → 审核 → Publish”周期。

重点指标：提交/完成/失败/等待审核数量；各算子失败率与 P95 耗时；文档重试次数；Build 校验失败数；每个 channel 的 active release 数；Control 与各 Domain 连接池错误；legacy 与 workflow 的非终态 Run 数。

停止并回滚的条件：5xx 或失败率显著高于基线、跨 Domain 迹象、重复 active release、冻结哈希不一致、审核绕过、Serving 回归或性能超过门槛。

## 8. 不可变回滚规则

操作回滚只有两步：

1. 设置 `MINING_RUN_SUBMISSION_ENGINE=legacy` 并重启 Mining 服务，使新提交回到旧 Pipeline。
2. 上传页根据 `/uploads/config` 隐藏 Workflow 选择器。

回滚后：

- 新 legacy Run 继续由旧 Pipeline 执行。
- 已有 workflow Run 继续由 `MiningWorkflowRuntime` 执行或恢复。
- 已有 legacy Run 继续由旧 Pipeline 执行或恢复。
- 不得更新 `execution_engine`、`workflow_id`、`workflow_version`、`workflow_version_id`、`workflow_graph_hash`、`workflow_manifest_json` 或节点事件。
- 不删除已发布 Workflow 版本；必要时只能归档自定义 Workflow，系统默认不能归档。

可用以下查询核对配置切换前后已有 Run 没有被改写：

```sql
SELECT id, execution_engine, workflow_id, workflow_version,
       workflow_graph_hash, status
FROM mining_runs
WHERE id IN ('<legacy-run-id>', '<workflow-run-id>');
```

## 9. legacy 退役条件

以下条件全部满足前不得删除 legacy 代码：

- 所有 Domain 中不存在非终态 legacy Run。
- 至少完成一个稳定观察周期，FULL 等价清单仍为空，性能、恢复、审核和 Serving 门禁持续通过。
- 回滚演练已完成并留存两种 engine 的恢复证据。
- 产品确认不再需要旧 `input_path` 提交协议，调用方迁移完成。

检查查询：

```sql
SELECT domain, status, COUNT(*)
FROM mining_runs
WHERE execution_engine = 'legacy'
  AND status NOT IN ('completed', 'failed', 'cancelled')
GROUP BY domain, status;
```

## 10. 最终发布门禁清单

- [ ] 16 个 Catalog 算子均有强类型 option、Handler 和单测。
- [ ] Workflow 定义全局，Run/审核/资产/Build/Release 按 Domain 隔离。
- [ ] 不传选择时绑定 FULL 当前发布版本；显式旧版本可复现。
- [ ] Run Trace 展示冻结图、节点 attempt、错误/警告、Build/Release。
- [ ] 实体审核和本体审核均可暂停/恢复。
- [ ] 图谱写失败阻止知识资产发布。
- [ ] FULL 等价无未批准差异，性能比不超过 1.20。
- [ ] 前端测试与构建通过，Java Serving 回归通过。
- [ ] 双数据库 PostgreSQL 验收由负责人在可丢弃环境执行并归档结果。
- [ ] 回滚演练通过，且没有改写任何已有 Run 的冻结执行事实。
