# Mining Upload Preflight and Snapshot Workflow Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在创建挖掘 Run 前完成逐文件预检，并保证一个 Snapshot 只绑定一个精确 Workflow 版本、每个逻辑文件同时只有一个发布生效版本。

**Architecture:** PostgreSQL Snapshot 增加不可变 Workflow 指纹，允许相同内容针对不同 Workflow 图生成不同 Snapshot。新增纯判定服务和 `/api/runs/preflight`，Run 冻结用户确认的逐文件决策；Workflow 摄取按该决策复用、恢复、保留或重挖，发布事务继续作为唯一生效切换边界。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、PostgreSQL/psycopg、Vue 3、TypeScript、Element Plus、pytest、Vitest。

---

### Task 1: Snapshot Workflow 指纹与数据库迁移

**Files:**
- Create: `databases/asset_core/schemas/004_asset_snapshot_workflow_binding.sql`
- Modify: `knowledge_mining/mining/infra/pg_schema.py`
- Modify: `knowledge_mining/mining/infra/db.py`
- Modify: `knowledge_mining/mining/snapshot/__init__.py`
- Modify: `knowledge_mining/mining/pipeline.py`
- Test: `knowledge_mining/tests/test_mining_snapshot_workflow_binding.py`

- [ ] 先写失败测试：同内容同图哈希复用 Snapshot，不同图哈希生成不同 Snapshot；缺失半套 Workflow 字段被拒绝。
- [ ] 运行 `pytest knowledge_mining/tests/test_mining_snapshot_workflow_binding.py -q`，确认因 API/迁移缺失失败。
- [ ] 增加 `workflow_id`、`workflow_version`、`workflow_version_id`、`workflow_graph_hash`，删除旧的 `(domain, normalized_content_hash)` 唯一约束，并建立 legacy 与 Workflow 两组部分唯一索引。
- [ ] 将冻结指纹从 `PipelineConfig` 传入 `select_or_create_snapshot()`；legacy 调用保持字段为空。
- [ ] 重跑测试确认通过。

### Task 2: 预检领域服务与 API

**Files:**
- Create: `knowledge_mining/mining/preflight.py`
- Modify: `knowledge_mining/mining/api/routes/runs.py`
- Test: `knowledge_mining/tests/test_mining_preflight.py`
- Test: `knowledge_mining/tests/test_mining_run_submission.py`

- [ ] 先写失败测试覆盖 `NEW`、`REUSED`、`RESTORED`、`WORKFLOW_CONFLICT`，并断言冲突默认决策是 `KEPT_CURRENT`。
- [ ] 运行定向 pytest，确认端点和判定服务缺失导致失败。
- [ ] 实现上传批次逐文件 SHA-256、精确 Workflow Binding 解析、当前发布 Snapshot/历史 Snapshot 查询和稳定的预检响应。
- [ ] 预检响应返回 `preflight_id`、Workflow 指纹、汇总、逐文件允许动作和默认动作。
- [ ] 重跑定向测试确认通过。

### Task 3: Run 冻结预检决策并校验过期状态

**Files:**
- Create: `databases/mining_runtime/schemas/006_mining_run_preflight.sql`
- Modify: `knowledge_mining/mining/infra/pg_schema.py`
- Modify: `knowledge_mining/mining/api/routes/runs.py`
- Modify: `knowledge_mining/mining/workflow/repositories/domain_run_repository.py`
- Test: `knowledge_mining/tests/test_mining_run_submission.py`

- [ ] 先写失败测试：Workflow 上传批次没有 `preflight_id + document_decisions` 时拒绝创建；决策 Workflow 指纹不一致时返回 409。
- [ ] 增加 `preflight_manifest_json` 并在 queued Run 插入时一次性冻结预检内容。
- [ ] 创建 Run 前重新读取相关 Snapshot 生效状态；状态变化返回 `preflight_stale`，不启动线程。
- [ ] 重跑定向测试确认通过。

### Task 4: Workflow 执行器遵守逐文件决策

**Files:**
- Modify: `knowledge_mining/mining/jobs/run.py`
- Modify: `knowledge_mining/mining/snapshot/__init__.py`
- Test: `knowledge_mining/tests/test_mining_preflight_execution.py`

- [ ] 先写失败测试：`REUSED/RESTORED/KEPT_CURRENT` 不进入文档算子，`REMINED/NEW` 进入文档算子；不同 Workflow 不再因内容哈希相同被 SKIP。
- [ ] 摄取时按 `relative_path + raw_content_hash` 读取冻结决策；保留/复用/恢复项写入 Run 文档审计并关联正确 Snapshot。
- [ ] `REMINED` 使用已匹配逻辑 document，但 Snapshot 按新 Workflow 指纹创建候选；失败不影响当前发布选择。
- [ ] 重跑定向测试确认通过。

### Task 5: 新建挖掘页面增加预检确认阶段

**Files:**
- Modify: `kb-ui/src/types/index.ts`
- Modify: `kb-ui/src/api/mining.ts`
- Modify: `kb-ui/src/views/mining/CreateRunView.vue`
- Modify: `kb-ui/src/views/mining/__tests__/CreateRunView.spec.ts`

- [ ] 先写失败测试：第一次点击只上传并预检；页面展示当前/目标 Workflow 版本；冲突默认 `KEPT_CURRENT`；确认后才调用 `createRun()`。
- [ ] 增加预检 TypeScript 类型和 `preflightRun()` API。
- [ ] 页面实现“配置 → 预检确认 → 创建 Run”两阶段交互、汇总、逐文件动作选择和返回修改。
- [ ] Workflow、版本、文件或 Domain 变化时废弃旧预检结果，禁止提交过期决策。
- [ ] 运行 Vitest 定向测试确认通过。

### Task 6: 回归验证

**Files:**
- Test: `knowledge_mining/tests/`
- Test: `kb-ui/src/views/mining/__tests__/`

- [ ] 运行新增后端测试与现有 Workflow/Run 单元测试。
- [ ] 运行 `npm run test -- --run src/views/mining/__tests__/CreateRunView.spec.ts`。
- [ ] 运行 `npm run build` 检查 TypeScript 与生产构建。
- [ ] 检查 `git diff --check`，并核对 legacy 不传 Workflow 时仍按旧流程创建 Run。
