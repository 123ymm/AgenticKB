# Mining Workflow 范式库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供七种平级挖掘 Workflow 范式，并在首次启动时把六种非兼容默认范式作为普通、已发布 Workflow 补入全局库，使 Workflow 管理页与上传页可以直接使用。

**Architecture:** 扩展现有 `builtin_templates()` 为七张可编译 DAG；用一份普通 Workflow 范式清单驱动启动期的“按名称缺失才创建并发布”，不设置 `is_system`、不保护、不覆盖同名记录。保留 `system-full-baseline` 的兼容默认职责和原 ID，不把新范式做成新的系统类型。前端继续消费现有列表和发布选项 API，只扩展新建对话框的七种模板键与中文说明。

**Tech Stack:** Python 3.11、FastAPI、Pydantic 2、pytest/pytest-asyncio、Vue 3、TypeScript、Element Plus、Vitest。

---

## 文件结构

- `knowledge_mining/mining/workflow/templates.py`：定义七种 DAG 的节点、分支和边。
- `knowledge_mining/mining/workflow/paradigms.py`：定义需要自动补齐的六种普通 Workflow 名称、说明和模板键。
- `knowledge_mining/mining/workflow/service.py`：启动时按名称幂等创建并发布缺失范式，同时保留兼容默认 Workflow。
- `knowledge_mining/mining/api/models/workflows.py`：接受七种 `template_key`。
- `knowledge_mining/tests/test_mining_operator_catalog.py`：约束七种模板的能力集合。
- `knowledge_mining/tests/test_mining_workflow_compiler.py`：验证七张图均可发布编译以及全局审核链差异。
- `knowledge_mining/tests/test_mining_workflow_service.py`：验证普通范式初始化、发布、幂等、不覆盖、可归档。
- `kb-ui/src/types/miningWorkflow.ts`：同步七种模板键类型。
- `kb-ui/src/views/mining/WorkflowListView.vue`：平级展示七种新建范式。
- `kb-ui/src/views/mining/__tests__/WorkflowListView.spec.ts`：验证七种模板都能提交给后端。

### Task 1: 七种可编译 DAG 模板

**Files:**
- Modify: `knowledge_mining/mining/workflow/templates.py`
- Test: `knowledge_mining/tests/test_mining_operator_catalog.py`
- Test: `knowledge_mining/tests/test_mining_workflow_compiler.py`

- [ ] **Step 1: 写失败测试**

断言模板键精确为：

```python
{
    "minimal",
    "fast_retrieval",
    "discourse_only",
    "entity_graph",
    "hybrid_knowledge",
    "ontology_only",
    "full",
}
```

并分别断言：快速检索只有 `retrieval_unit_build + embedding`；固定本体图谱没有本体归纳/审核；联合构建同时有篇章链和实体链但没有本体归纳；所有模板可按 publish 模式编译。

- [ ] **Step 2: 验证测试按预期失败**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_operator_catalog.py knowledge_mining/tests/test_mining_workflow_compiler.py -q
```

Expected: FAIL，缺少 `fast_retrieval`、`entity_graph`、`hybrid_knowledge`。

- [ ] **Step 3: 实现最小模板构建器**

将模板配置拆成篇章分支、实体分支和全局本体归纳开关。全局链只允许三种：

```text
asset_persist -> mining_finalize
asset_persist -> entity_review_gate -> graph_write -> mining_finalize
asset_persist -> entity_review_gate -> ontology_induction
              -> ontology_review_gate -> graph_write -> mining_finalize
```

- [ ] **Step 4: 验证模板测试通过**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_operator_catalog.py knowledge_mining/tests/test_mining_workflow_compiler.py -q
```

Expected: PASS。

### Task 2: 普通 Workflow 范式初始化

**Files:**
- Create: `knowledge_mining/mining/workflow/paradigms.py`
- Modify: `knowledge_mining/mining/workflow/service.py`
- Modify: `knowledge_mining/mining/api/app.py`
- Test: `knowledge_mining/tests/test_mining_workflow_service.py`

- [ ] **Step 1: 写失败测试**

测试首次初始化后包含一个兼容默认和六个普通 Workflow；六个普通项均满足：

```python
item["is_system"] is False
item["is_system_default"] is False
item["current_version"] == 1
```

再次初始化不得增加版本或重复记录；同名已有 Workflow 不得被覆盖；普通范式允许归档且归档后不会被启动逻辑恢复。

- [ ] **Step 2: 验证测试按预期失败**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_workflow_service.py -q
```

Expected: FAIL，目前只初始化 `system-full-baseline`。

- [ ] **Step 3: 实现范式清单和初始化服务**

六种普通记录为：

```text
基础文档入库 -> minimal
快速向量检索 -> fast_retrieval
篇章增强检索 -> discourse_only
固定本体图谱构建 -> entity_graph
检索与图谱联合构建 -> hybrid_knowledge
本体演化专项 -> ontology_only
```

调用 `create()` 时不传固定 ID、不设置系统标识；随后发布 v1。初始化只按名称判断是否已存在，任何已有同名记录都保持原样。

- [ ] **Step 4: 验证服务测试通过**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_workflow_service.py -q
```

Expected: PASS。

### Task 3: API 与前端七种平级模板

**Files:**
- Modify: `knowledge_mining/mining/api/models/workflows.py`
- Modify: `kb-ui/src/types/miningWorkflow.ts`
- Modify: `kb-ui/src/views/mining/WorkflowListView.vue`
- Modify: `kb-ui/src/views/mining/__tests__/WorkflowListView.spec.ts`

- [ ] **Step 1: 写失败测试**

把 Workflow 列表页测试改为依次创建七种模板，并断言提交顺序与七个模板键一致；补后端模型测试或 API 测试，确认新增键不返回 422。

- [ ] **Step 2: 验证测试按预期失败**

Run:

```powershell
Set-Location kb-ui
npm test -- src/views/mining/__tests__/WorkflowListView.spec.ts
```

Expected: FAIL，当前前端只声明四种模板。

- [ ] **Step 3: 同步七种键和中文展示**

模板选择器按以下顺序平级展示：基础文档入库、快速向量检索、篇章增强检索、固定本体图谱构建、检索与图谱联合构建、本体演化专项、全量知识构建。不得增加“高级”分组或系统预置标记。

- [ ] **Step 4: 验证前后端定向测试通过**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_workflow_api.py -q
Set-Location kb-ui
npm test -- src/views/mining/__tests__/WorkflowListView.spec.ts
```

Expected: PASS。

### Task 4: 全量回归与构建

**Files:**
- Verify only

- [ ] **Step 1: 运行 Workflow 后端回归**

Run:

```powershell
python -m pytest knowledge_mining/tests/test_mining_operator_catalog.py knowledge_mining/tests/test_mining_workflow_compiler.py knowledge_mining/tests/test_mining_workflow_service.py knowledge_mining/tests/test_mining_workflow_api.py knowledge_mining/tests/test_mining_run_binding.py -q
```

Expected: PASS，0 failures。

- [ ] **Step 2: 运行前端相关测试**

Run:

```powershell
Set-Location kb-ui
npm test -- src/views/mining/__tests__/WorkflowListView.spec.ts src/views/mining/__tests__/CreateRunView.spec.ts
```

Expected: PASS，0 failures。

- [ ] **Step 3: 运行前端生产构建**

Run:

```powershell
Set-Location kb-ui
npm run build
```

Expected: exit code 0；允许现有 chunk-size 警告，不允许 TypeScript 或 Vite 编译错误。

- [ ] **Step 4: 审查差异**

确认没有改写用户现有 Workflow、没有新增系统保护逻辑、没有引入 Domain 维度，并记录真实数据库验证由用户执行。
