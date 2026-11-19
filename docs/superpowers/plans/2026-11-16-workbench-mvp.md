# 知识制品工作台 MVP 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立分支上建成「知识制品工作台」可扩展 MVP——工作区（图草稿）+ 两类来源（知识库目录/Excel）+ 4 确定性算子 + LLM 抽取算子 + 执行引擎（试跑/执行/快照/失效传播）+ 导出为私有表格制品草稿，行级来源贯穿，7 张表全建含 actor 字段。

**Architecture:** mining 服务内新建工作区子域（`/api/workspaces` 前缀），数据模型按完整 DAG 设计（nodes/ports JSONB），UI 用步骤流渲染（不做画布）。执行引擎复用 wiki 编译三件套模式（advisory lock 串行 + 状态落表前端轮询 + LlmTaskClient 异步批）。导出=单事务建制品草稿（幂等 request_id），**绝不发布**。需求依据=[67号规划](../../下一阶段/67-知识制品工作台-构建规划与Agent演进-2026-11-12.md)（瘦身版：批次1-4 减去 空白表/制品来源/按标题转列/人工修正账/成员UI/画布；另裁：内容视图与聚焦切换、运行记录历史 UI、GET /exports 列表端点——runs 数据面已备，这三项观感层下批补）。

**Tech Stack:** FastAPI + psycopg AsyncConnectionPool（既有）、PostgreSQL（kb 库，新迁移 012）、Vue3+Element Plus+vitest（既有，零新依赖）、LLM 异步通道 LlmTaskClient（既有）。

---

## 0. 全局约定（每个任务的实施者必须先读本节）

**Worktree**: `D:\mywork\AgenticKB\.worktrees\workbench-mvp`，分支 `feat/workbench-mvp`（基于 05ac90e）。**所有提交都在 worktree 内；禁止动主区；禁止 merge master；git add 只用显式路径（铁律，历史事故）。**

**真库测试**（tests/kb 系全部连真实 PG，见 `knowledge_mining/tests/kb/conftest.py` 的 `async_pool` 夹具，环境变量 `PG_HOST/PG_PORT/PG_DBNAME/PG_USER/PG_PASSWORD`，默认 localhost:5432/kb_db_test）：

```bash
# 本机 unified-pg 容器（pgvector/pg16，端口 55442，zdy/zdy1234）——先确认在跑：
docker ps --format '{{.Names}} {{.Ports}}' | grep 55442 || docker start unified-pg
# 容器内跑测试（宿主 GBK 坑铁律：中文测试必须容器内 python）：
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e PG_HOST=host.docker.internal -e PG_PORT=55442 -e PG_DBNAME=kb_db_test \
  -e PG_USER=zdy -e PG_PASSWORD=zdy1234 \
  -v "D:\mywork\AgenticKB\.worktrees\workbench-mvp:/repo" -w /repo \
  agentickb-release-validation:a74b940 \
  python -m pytest knowledge_mining/tests/kb/<测试文件>.py -v
```
（镜像若不在本地，`docker images | grep agentickb` 确认实际 tag；以最近一次成功运行为准。）

**前端门**：`kb-ui/` 下 `npm run build`（=vue-tsc -b && vite build）是类型唯一真相（vue-tsc --noEmit 假通过坑）；测试 `npm run test`（vitest run）。

**代码风格**：跟随既有 kb 域——模块级中文 docstring（引用决策号/任务号）、async/await + `async with self._pool.connection() as conn`、错误用领域异常类+`_map_error` 翻译 HTTP、时间戳/ID 生成口径**先读 `databases/kb/schemas/017_kb_artifacts.sql` 与 `db_artifacts.py` 再照抄**。

**核心复用锚点**（实施前先读对应文件）：

| 模式 | 锚点文件 |
| --- | --- |
| 乐观锁 PUT（expected token 锁内校验） | `mining/kb/db_wiki_toc.py:90 upsert_draft_toc` |
| advisory lock 串行守卫（判定在 DB 层、service 只翻译） | `mining/kb/db_compile_inputs.py:455 try_start_guard` |
| 角色/权限判定序（site admin→domain admin→成员→404 反探测） | `mining/kb/services/artifact_service.py:234 _assert_role` |
| Excel 纯函数（类型推断/cast/表头清洗） | `mining/kb/services/artifact_import_service.py:141/158/196` |
| 路由+服务+deps 注入模式 | `mining/kb/routes/wiki.py`（头部）、`mining/kb/deps.py` |
| 制品创建事务内核 | `mining/kb/db_artifacts.py:114` |
| LLM 异步批（幂等键/批量状态/等待） | `mining/infra/llm_task_client.py:98/178/210` |

**禁止事项**：不新增任何依赖包；不写 `kb_artifact_batches`；不改既有表结构；不做画布/成员UI/修正账；Agent 字段（actor_kind/actor_id）**必须**从第一张表就带上。

---

## 1. 文件结构（分解决策）

```
databases/
  migrations/core/20261116_012_kb_workspaces.sql     # 7 表迁移（下一个号=012，先查重）
  kb/schemas/021_kb_workspaces.sql                   # 全新装库镜像（该目录序列独立，已到 020）
  migrations/manifest.yaml                            # 追加条目
knowledge_mining/mining/kb/
  db_workspaces.py            # 登记层：workspaces/members/graph(乐观锁)/sources
  db_workspace_runs.py        # 运行层：runs/outputs(is_current 指针+stale)/bindings/exports
  workspace_dataset.py        # 数据集信封契约 + 守卫（5万行/20MB）+ hash + trial 切片
  workspace_operators.py      # 算子注册表（封闭 dict）+ 4 确定性算子（纯函数）
  workspace_llm_operator.py   # LLM 逐行抽取（异步批+失败重试+试跑）
  workspace_sources.py        # 来源适配器：kb_dir（文档底表）/ excel（复用导入纯函数）
  workspace_executor.py       # 执行引擎：guard/拓扑执行/快照/stale 传播/试跑/取消
  workspace_service.py        # 服务层：权限/编排/导出事务
  routes/workspaces.py        # REST /api/workspaces
  deps.py                     # +3 注入函数
knowledge_mining/tests/kb/
  test_workspaces_schema.py   # 迁移+镜像守卫
  test_workspaces_db.py       # 登记层真库测试
  test_workspace_runs_db.py   # 运行层真库测试
  test_workspace_operators.py # 纯函数单测（B.3 固定材料）
  test_workspace_sources.py   # 适配器真库测试
  test_workspace_executor.py  # 引擎真库测试
  test_workspace_llm.py       # LLM 算子（mock LlmTaskClient）
  test_workspace_routes.py    # 路由测试（含导出幂等）
  test_workspace_e2e.py       # 端到端（B.3 材料：源→算子→执行→导出→制品草稿）
kb-ui/src/
  api/workspaces.ts
  views/workspaces/WorkspacesListView.vue
  views/workspaces/WorkspaceDetailView.vue
  components/workspaces/WorkspaceStepFlow.vue
  components/workspaces/WorkspaceOutputTable.vue
  components/workspaces/WorkspaceInspector.vue
  components/workspaces/WorkspaceSourceDialog.vue
  components/workspaces/WorkspaceExportDialog.vue
  components/workspaces/__tests__/*.spec.ts
  router/index.ts             # +2 路由
  components/layout/Sidebar.vue  # ALL_NAV +1 项
```

**图（graph_json）契约**（写入 db_workspaces 校验，executor/前端共用）：
```python
{"nodes": [
  {"id": "n1", "op": "source|select|filter|join|split_sections|llm_extract",
   "name": "显示名",
   "config": {...},                    # 算子参数，schema 由注册表定义
   "inputs": ["n0:main"],              # "node:port" 引用；source 节点为 []
   "ports": [{"id": "main", "name": "输出表", "kind": "table"}],  # 由注册表派生，存储冗余
   "x": 0, "y": 0,                     # 布局装饰，执行语义无关
   "pending": false}                   # 配置已改（服务端 graph diff 维护）
]}
```

**数据集信封**（workspace_dataset.py）：
```python
TableDataset = dict   # {"kind":"table","name":str,"cols":[str],"rows":[{"_id":str,"_source":str,**fields}],"grain":str}
# kind 枚举契约含 "documents"/"relations"，但 MVP 只实现 table 渲染与算子
# 信封元数据（存 outputs 行）：row_count / content_hash(sha256 of canonical json) / quality_json
```

---

## Chunk 1：数据层与算子（Task 1–7）

### Task 1: 迁移 012 + kb/schemas 021 + 三镜像注册 + 守卫测试

**Files:**
- Create: `databases/migrations/core/20261116_012_kb_workspaces.sql`
- Create: `databases/kb/schemas/021_kb_workspaces.sql`
- Modify: `databases/migrations/manifest.yaml`（追加条目，mode: online_expand，checksum 占位后用真实 sha256）
- Modify: `knowledge_mining/mining/infra/pg_schema.py`、`knowledge_mining/mining/maintenance/database_upgrade/contract.py`（照 kb_export_tasks 的注册样例）
- Test: `knowledge_mining/tests/kb/test_workspaces_schema.py`

**步骤：**
- [ ] Step 0: 读 `databases/kb/schemas/020_kb_export_tasks.sql` 与 `002_knowledge_bases.sql` 定时间戳口径（**TEXT ISO**，任务记录表同族；注意 017 是 TIMESTAMPTZ **勿抄**，只参考其 CHECK/注释风格）。
- [ ] Step 1: 写失败测试：7 张表存在、关键 CHECK/部分唯一索引存在、镜像文件与迁移 SQL 表集一致（参考既有 `tests/maintenance/test_schema_mirror_sync.py` 的断言方式，把 7 表纳入其清单或新建并行守卫）。
- [ ] Step 2: 容器内跑 → FAIL（表不存在）。
- [ ] Step 3: 写迁移 SQL（7 表 DDL 全文见下）+ 镜像 + manifest + 两处 Python 契约。**镜像与迁移 SQL 内容必须同文**（fresh-install 库只跑 schemas/ 目录）。
- [ ] Step 4: 容器内跑 → PASS；另跑 `python -m pytest knowledge_mining/tests/maintenance/ -v` 确认镜像守卫全绿。
- [ ] Step 5: `git add` 上述 5 个显式路径，commit `feat(kb): 工作台7表迁移012——登记/成员/来源/运行/输出/绑定/导出（含actor字段）`。

**7 表 DDL（实施时以 017 口径微调时间戳/注释，不得改列语义）：**
```sql
CREATE TABLE IF NOT EXISTS kb_workspaces (
    id TEXT PRIMARY KEY,                       -- wsp_<16hex>，应用生成
    domain_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    graph_json JSONB NOT NULL DEFAULT '{"nodes":[]}',
    graph_revision INTEGER NOT NULL DEFAULT 0,
    batch_no INTEGER NOT NULL DEFAULT 1,
    actor_kind TEXT NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user','mcp_key')),
    actor_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_workspaces_domain ON kb_workspaces(domain_id);
CREATE INDEX IF NOT EXISTS idx_ws_workspaces_owner ON kb_workspaces(owner_user_id);

CREATE TABLE IF NOT EXISTS kb_workspace_members (
    workspace_id TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_sources (
    id TEXT PRIMARY KEY,                       -- wss_<16hex>
    workspace_id TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    connector TEXT NOT NULL CHECK (connector IN ('kb_dir','excel')),
    config JSONB NOT NULL DEFAULT '{}',        -- kb_dir:{kb_id,directories[]}; excel:{file_name,sheet_name}
    input_manifest JSONB NOT NULL DEFAULT '{}',-- 冻结输入：kb_dir=文件清单+计数; excel=解析后的整份数据集（受护栏约束）
    last_read_at TEXT,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, node_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_runs (
    id TEXT PRIMARY KEY,                       -- wsr_<16hex>
    workspace_id TEXT NOT NULL REFERENCES kb_workspaces(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('node','workspace','subgraph')),  -- subgraph 预留；trial 不建 run
    target_node_id TEXT,
    graph_revision INTEGER NOT NULL,
    graph_hash TEXT NOT NULL,
    graph_snapshot_json JSONB NOT NULL,        -- 冻结图（含全部节点配置）——重启可判定的根
    batch_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','awaiting_review','completed','partial','failed','cancelled')),
    progress_json JSONB NOT NULL DEFAULT '{}', -- {done:[node_id], total:N, current_node, failed_rows:{node:[]}}
    error TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user','mcp_key')),
    actor_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_runs_ws ON kb_workspace_runs(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kb_workspace_run_outputs (
    run_id TEXT NOT NULL REFERENCES kb_workspace_runs(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    port_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('table','documents','relations')),
    content_inline JSONB,
    row_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    quality_json JSONB NOT NULL DEFAULT '{}',  -- {input_rows,filtered,failed_row_ids,broken_links...} 机器可读
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    stale_since_run TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id, port_id)
);
-- 当前结果指针：同一 (workspace,node,port) 至多一行 current（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_ws_output_current
    ON kb_workspace_run_outputs(workspace_id, node_id, port_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS kb_workspace_output_bindings (
    workspace_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    port_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL DEFAULT 'table',
    last_run_id TEXT,
    last_exported_at TEXT,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, node_id, port_id)
);

CREATE TABLE IF NOT EXISTS kb_workspace_exports (
    id TEXT PRIMARY KEY,                       -- wse_<16hex>
    request_id TEXT NOT NULL UNIQUE,           -- 幂等键（X03）
    workspace_id TEXT NOT NULL,
    node_id TEXT NOT NULL, port_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'create_draft' CHECK (mode IN ('create_draft')),
    target_artifact_id TEXT,
    snapshot_run_id TEXT,
    mapping_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed')),
    result_json JSONB, error TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user','mcp_key')),
    actor_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
```

### Task 2: db_workspaces.py 登记层（真库 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/db_workspaces.py`
- Test: `knowledge_mining/tests/kb/test_workspaces_db.py`

**行为规格（每条=一个测试用例）：**
- `create_workspace(conn, *, domain_id, name, description, owner_user_id, created_by)` → 单事务 INSERT workspaces + INSERT members(owner 行)；返回含 `graph_revision=0`。id=`wsp_`+secrets 16hex。
- `list_workspaces(conn, *, domain_id, user_id, is_site_admin, is_domain_admin)` → admin 全域，余人=成员行 JOIN；只列 `status='active'` + 归档单列。
- `get_workspace` / `patch_workspace(name/description)` / `archive_workspace`（软删，幂等）。
- `put_graph(conn, workspace_id, graph, expected_revision, require_token=True)`：**锁内乐观锁**（模式照 `db_wiki_toc.upsert_draft_toc`）——expected_revision 不符→`WorkspaceConflict`；校验：节点 id 唯一、`inputs` 引用的 node:port 存在、source 节点 inputs 为空、**环检测**（Kahn，有环→`WorkspaceValidationError`）；**服务端 diff**：与旧图比较，config/inputs 变化的节点 `pending=true`、其严格后代 `stale=true`，**两标志随 graph_json 原子写入**（不另动 outputs 行——两标签分工：节点态看图，"上游已变"输出级 `stale_since_run` 仅由执行传播，67号 §5.2）；graph_revision+1。
- `upsert_member/remove_member/list_members`（owner 角色不可移除最后一人）。
- `upsert_source(conn, workspace_id, node_id, connector, config, input_manifest)` / `list_sources` / `delete_source`（UNIQUE(workspace_id,node_id) 冲突→UPSERT 语义）。**upsert_source 同事务推进 `kb_workspaces.batch_no+1`**（"用新资料重跑"=重新 PUT source 触发重读即新批次，WB-07）。

**步骤：** 写测试（含乐观锁冲突/环拒绝/末端成员保护四个负例）→ FAIL → 实现 → PASS → commit `feat(kb): 工作区登记层——CRUD/成员/图乐观锁与环检测/来源绑定`。

### Task 3: db_workspace_runs.py 运行层（真库 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/db_workspace_runs.py`
- Test: `knowledge_mining/tests/kb/test_workspace_runs_db.py`

**行为规格：**
- `try_start_guard(conn, workspace_id, created_by, *, graph_snapshot, graph_hash, scope, target_node_id)` —— **照抄 `db_compile_inputs.try_start_guard` 三段式**：`pg_advisory_xact_lock(hashtext(workspace_id))` 内 ①查在飞 run（status∈queued/running）→ 返回 in_flight（service 翻 409）②归档/无图→validation 错 ③INSERT queued（graph_snapshot 随 INSERT 原子携带）。
- `mark_running/mark_completed/mark_partial/mark_failed/mark_cancel` + `update_progress(run_id, done_node, failed_rows)`。
- `write_outputs(conn, run_id, workspace_id, node_id, outputs:{port:{dataset,quality}})`：单事务——INSERT 新输出行 + **同事务翻转旧 current**（`UPDATE ... SET is_current=FALSE WHERE workspace_id/node/port 相同 AND is_current`）；行数/序列化超护栏（Task 4 的 guard 函数）→ 拒绝整个写入。
- `set_stale(conn, workspace_id, node_port_pairs, since_run_id)`：只更新当前行且未指向同一 run 的（幂等）。
- `get_current_outputs(conn, workspace_id)` / `get_run(conn, run_id)` / `list_runs(workspace_id, limit=10)` / `get_output_for_run`。
- `upsert_binding` / `insert_export(conn, ..., request_id)`（UNIQUE 冲突→返回既有行，幂等入口）+ `mark_export_result`。

**步骤：** 测试（含 in_flight 409 数据、is_current 唯一翻转、stale 幂等、request_id 重放）→ FAIL → 实现 → PASS → commit `feat(kb): 工作区运行层——守卫/生命周期/输出current指针/stale/导出幂等`。

### Task 4: 数据集契约 + 算子注册表 + 4 确定性算子（纯函数 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/workspace_dataset.py`、`knowledge_mining/mining/kb/workspace_operators.py`
- Test: `knowledge_mining/tests/kb/test_workspace_operators.py`（纯函数，无需 DB）

**workspace_dataset.py：**
```python
MAX_ROWS = 50_000; MAX_SERIALIZED_BYTES = 20 * 1024 * 1024; TRIAL_ROWS = 3
def validate_dataset(ds) -> None          # 契约：cols 非空且唯一；每行必带 _id/_source（str）；字段名⊂cols；row cap
def dataset_hash(ds) -> str               # canonical json sha256（cols+rows 顺序敏感）
def serialized_size_ok(ds) -> bool
def slice_for_trial(ds, n=TRIAL_ROWS)     # 复制并截前 n 行
def merge_quality(*reports) -> dict       # 质量账合并
```

**workspace_operators.py 注册表（封闭 dict，新增算子=加条目）：**
```python
OPERATOR_DEFS: dict[str, OperatorDef]
# OperatorDef = {"op","name","input_arity",(多输入算子如 join=2),"ports":[PortSpec],
#                "config_schema": dict(json-schema-lite，前端表单与后端校验共用),
#                "run"(config, inputs: list[TableDataset]) -> (outputs: dict[port, TableDataset], quality: dict)}
def run_operator(op, config, inputs) -> ...   # 校验 config + 输入类型/数量 → 调 def.run → validate_dataset 每个输出
def topological_order(nodes) -> list[str]     # Kahn；环抛 WorkspaceValidationError（graph PUT 复用）
```

**4 算子行为规格（测试必须用 64号 B.3 固定材料：产品表 P1/P3000W、P2/2000W、P3/1200W + 库存表 P1/5、P3/0）：**
1. `select`（列选择/改名）：config `{columns:[{id, name}]}`；至少 1 列；未知列→错；改名目标重复→错；行 `_id/_source` 不变。
2. `filter`：config `{field, cond∈(eq,ne,gt,gte,lt,lte,contains,is_empty,not_empty), value}`；字段缺失→错；数值条件值不可转数值→错；null 普通比较不匹配（空值走 is_empty）；quality 记 `filtered` 计数。
3. `join`：config `{method∈(left,inner), left_key, right_key}`；**右键重复→错且不产出**（B.3 铁律）；B.3 断言：left=3 行（P2 库存空）、inner=2 行；未匹配左行 left 保留、补充列空；输出 `_id=左._id`（一对一）；`_source=左._source；右._source`；右列名与左冲突→前缀 `右表_`。
4. `split_sections`（拆章节）：输入必须含 `文档内容_JSON` 列（值为 `{sections:[{level,title,text}]}` 或字符串——字符串行原样进「内容」列）；config `{level: 1|2}`；输出长表 cols=`[文件,章节,内容]`，一行一节，`_id=doc_id:section_title`，`_source` 继承文档来源；无该级别章节的文档进 quality `{no_section_docs:[...]}`。

**步骤：** B.3 数据写成模块级 fixture → 每算子正例+负例测试 → FAIL → 实现 → PASS → commit `feat(kb): 数据集契约+算子注册表+4确定性算子（B.3口径）`。

### Task 5: 来源适配器 kb_dir + excel（真库 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/workspace_sources.py`
- Test: `knowledge_mining/tests/kb/test_workspace_sources.py`

**规格：**
- `read_kb_dir(conn, *, kb_id, directories, user_ctx)`：
  1. 复用既有 KB 可见性判定（先读 `mining/kb/services/kb_service.py` 与 kbs.py 路由的域/成员检查函数，找现成谓词照抄，不重写 ACL）；无权→`WorkspacePermissionError`。
  2. 枚举 scope 内文档（`asset_documents`；directories 空数组=全库；上限 200 文档，超限→422 语义错误）。**读取路径先读 `mining/api/deps.py:get_parse_result_service` 及其服务实现**，镜像其取数 SQL（表名/权限口径以它为准），每文档取 L1/L2 章节结构。
  3. 产出文档底表：cols `[知识库,目录,文件,文档内容_JSON]`，一行一文档，`_id=document_id`、`_source=f"{kb_name}/{目录}/{文件名}"`；`文档内容_JSON={"document_id",...,"sections":[{level,title,text}]}`。
  4. input_manifest=`{frozen_at, doc_count, documents:[{document_id, title, content_revision}]}`。
- `parse_excel(content: bytes, *, file_name, sheet_name|None)`：复用 `_dedupe_headers/_infer_column_type/_cast_value/_check_zip_bomb`（import 服务私有函数直接 import，不复制）；产出数据集 + `_id=row_<n>` hex（复用 `_row_key` 思路：无主键则序号）+ `_source=f"{file_name}/{sheet}"`；**input_manifest 存整份解析数据集**（冻结语义：重跑不重读文件，重传=更新来源）+ `{file_name, sheet_name, row_count, frozen_at}`。
- `materialize_source(conn, source_row)` → 数据集（kb_dir 按 manifest 重读？**否——MVP 冻结语义统一**：kb_dir 接入时也把底表数据写进 input_manifest（受护栏），「用新资料重跑」= 重新 PUT source 触发重读。这样执行器对两类来源同构：都从 input_manifest 取数）。守卫超限→错误。
- **决策记录（对 67号 §7.1 前半句的显式偏离）**：MVP 冻结语义=接入即冻数据，执行不再重读 KB——撤权后执行不会失败；撤权检查**移至导出门禁**（Task 9：含 kb_dir 血缘的 item 导出前重验 KB 可见性，无权→item 失败，对应 67号 §7.1 后半句）。

**步骤：** 真库 seed 一个 KB+2 文档+解析结构（真库 seed 手法参照 `tests/kb/test_wiki_db.py` 与 `tests/kb/test_export_service.py`——注意 test_artifacts_routes.py 是 fake 路由测试不造库）→ 测试（无权 403 语义/超限/正常产出底表/excel 类型推断）→ FAIL → 实现 → PASS → commit `feat(kb): 工作台来源适配器——kb目录文档底表与Excel冻结解析`。

### Task 6: workspace_executor.py 执行引擎（真库 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/workspace_executor.py`
- Test: `knowledge_mining/tests/kb/test_workspace_executor.py`

**规格（async，复用 wiki 编译后台任务形态）：**
- `async start_run(pool, workspace_id, *, scope, target_node_id, actor_kind='user', actor_id='', created_by)`：读图→按 scope 裁剪执行集（node=仅该节点+其输入依赖链可复用旧 current；workspace=全图）→ graph_hash → `db_workspace_runs.try_start_guard`（409 翻译留给 service）→ `asyncio.create_task(_execute(...))` → 返回 run_id。
- `async _execute(...)`：mark_running → `topological_order` 逐节点：
  - source 节点：从 `kb_workspace_sources.input_manifest` 取数据集（缺来源行→节点失败）。
  - 算子节点：输入=上游端口的**本 run 新输出**（无则取该端口 current 输出并记 quality `reused_snapshot=true`；current 也无→节点失败"输入未执行"）→ `run_operator` → `write_outputs`（单事务+current 翻转）；节点成功后**清除该节点 pending/stale 标志**（jsonb_set 定点清除，仅当 graph_revision 仍等于 run 启动值——运行中被用户改图则跳过清除，标志留给下次 PUT/执行收敛）。**LLM 节点分派**：注册表条目带 `kind:"llm"`，executor 持有 LlmTaskClient 实例经 ctx 调 `run_full`（Task 7 落地该接线；Task 6 先以 not_implemented 桩占位）→ `set_stale`(严格后代节点的 current 输出, since=本 run)。
  - 节点失败：记录 error，**继续执行无依赖关系的其他分支**（partial 语义）；全成=completed，部分成=partial，全败=failed。
  - 每节点边界检查取消（status=cancelled 即停，已写输出保留，行留终态）。
  - `update_progress` 每节点一次。
- `async trial_node(pool, workspace_id, node_id)`：不建 run、不写库——取输入 current（无→错误）→ `slice_for_trial(3)` → `run_operator` → 返回 `{outputs, quality, trial:true, input_rows:3}`。LLM 节点走 Task 7 的同步试跑通道。
- `async cancel_run(pool, run_id)`：置 cancelled（幂等；running 任务在节点边界自停）。
- 崩溃恢复口径：进程重启后 running 残留=启动时（或首次查询时惰性）判 failed，error="interrupted"——不自动续跑。

**步骤：** 测试（两分支图：join 分支+filter 分支；filter 故意失败→partial+join 分支独立完成；stale 传播断言；trial 不落库断言；in_flight 二连 start 409）→ FAIL → 实现 → PASS → commit `feat(kb): 工作台执行引擎——拓扑执行/分支partial/快照current/stale传播/试跑`。

### Task 7: LLM 抽取算子（mock LlmTaskClient TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/workspace_operators.py` 追加注册（或 `workspace_llm_operator.py` 单独文件，若 operators 超 400 行）
- Test: `knowledge_mining/tests/kb/test_workspace_llm.py`

**规格：**
- 算子 `llm_extract`：config `{input_column, prompt_template, output_fields:[{name, type∈六列型}], model?}`；ports=[main]；输入 1 张表。
- 模板渲染：**零依赖**——`prompt_template` 中 `{列名}` 用 `str.format_map(SafeDict(row减_id/_source))` 替换（未知占位符保留原文），不引入 Jinja。
- 全量执行 `run_full(ctx, config, dataset, *, domain, run_id, node_id)`：逐行 `LlmTaskClient.submit_chat(messages, expected_output_type="json_object", idempotency_key=f"wsop:{run_id}:{node_id}:{row._id}", model, knowledge_domain=domain)`（幂等键含 run 身份——67号 §5.2 口径，改配置重跑不会被旧结果去重） → `batch_status` 轮询/`wait_for_tasks` → 行级失败**仅失败子集重提一次**（max_attempts 由 llm_service 兜底 3）→ 输出=输入列+新字段（值经 `_cast_value` 同源 cast；cast 失败记该行 failed）→ quality=`{total, succeeded, failed_row_ids, retried}`；失败行**不混入输出**（输出仅成功行？**否——B.4 完整性铁律：存在失败行→整个输出标记 partial，行保留原值+空新列，quality 列明 failed_row_ids，禁止冒充完整**）。
- 同步试跑 `run_trial(ctx, config, dataset, *, domain, node_id)`：前 3 行 submit+短轮询（≤60s），逐行失败不阻塞其余；幂等键=`f"wstrial:{node_id}:{row._id}:{config_hash}"`（config_hash=prompt+output_fields 的 sha256 前 8 位——试跑与全量、改配置前后互不污染）。
- ctx 注入 client（测试传 fake：可编程返回 succeeded/failed/慢）。
- **步骤：** fake 三态测试（全成/部分失败重试一次/试跑超时降级）→ FAIL → 实现 → PASS → **接线 executor**（OPERATOR_DEFS["llm_extract"] 标 `kind:"llm"`，Task 6 桩替换为真分派并传 run_id/node_id/ctx；引擎真库测试补一条 LLM 节点用 fake client 走通）→ commit `feat(kb): LLM逐行抽取算子——异步批/失败子集重试/试跑三行+执行器接线`。

---

## Chunk 2：服务面、导出与前端（Task 8–13）

### Task 8: workspace_service.py + routes/workspaces.py 基础面（路由 TDD）

**Files:**
- Create: `knowledge_mining/mining/kb/workspace_service.py`、`knowledge_mining/mining/kb/routes/workspaces.py`
- Modify: `knowledge_mining/mining/kb/deps.py`（+`get_workspace_service`）、`knowledge_mining/mining/api/app.py`（include_router 注册处 :326-330 附近，照 artifacts/wiki 的 include 形态追加）
- Test: `knowledge_mining/tests/kb/test_workspace_routes.py`

**服务层：**
- `_assert_ws_role(workspace, user, allowed, *, db)`：**判定序照抄 artifact_service._assert_role**（site admin→domain admin→成员行→owner_user_id 兜底→404 反探测 `WorkspaceNotFound`）；design=(owner,editor)，read=+viewer。域归属校验（workspace.domain_id ∈ 用户可达域）。
- CRUD 薄封装 + graph PUT（乐观锁翻译 409 `graph_conflict`）+ sources PUT（kb_dir 调 Task5 适配器即读即冻结）/DELETE + runs start/status/list/cancel（409 in_flight）+ current outputs 读 + trial。
- run status 响应含 `progress_json` 与节点态（pending/stale 由 graph+outputs 合成，前端轮询用）。

**路由（/api/workspaces，auth=current_user，错误映射照 artifacts._map_error 风格）：**
```
POST/GET ''；GET/PATCH/DELETE '/{wid}'（DELETE=archive）
GET/POST '/{wid}/members'，PATCH/DELETE '/{wid}/members/{uid}'
PUT '/{wid}/graph'
GET/POST '/{wid}/sources'，DELETE '/{wid}/sources/{node_id}'
POST '/{wid}/sources/excel'（multipart 上传→解析冻结）
POST '/{wid}/runs'→202 {run_id}；GET '/{wid}/runs'；GET '/{wid}/runs/{rid}'；POST '/{wid}/runs/{rid}/cancel'
GET '/{wid}/outputs/current'
POST '/{wid}/outputs/{node_id}/{port_id}/trial'
```
（导出端点 Task 9 加。）

**步骤：** httpx AsyncClient+app 测试（404 反探测/409/graph 环 422/来源无权 403）→ FAIL → 实现 → PASS → commit `feat(kb): 工作台服务与路由——CRUD/图/来源/运行面`。

### Task 9: 导出交付（幂等+单事务，路由 TDD）

**Files:**
- Modify: `workspace_service.py`、`routes/workspaces.py`
- Test: `test_workspace_routes.py` 追加导出用例（或新建 `test_workspace_export.py`）

**规格：**
- `POST /{wid}/exports` body `{request_id, items:[{node_id, port_id, artifact_name, owner_user_id?}]}` → 201。
- 服务层 `export_drafts`：①request_id 查重→原样返回既有结果（幂等，200）②逐 item 门禁（任一不过→该 item 失败，不阻断其余）：输出 is_current 且非 stale 且 row_count>0；**含 kb_dir 来源血缘→重跑 Task 5 的 KB 可见性谓词，无权→item 失败**（67号 §7.1 后半句：撤权禁导出）；目标制品已绑定且 `artifact.updated_at > binding.last_exported_at`→阻断（防覆盖人工改动）。③**单 item 单事务（手法）**：db_artifacts 各方法自开事务（每方法一个 pool.connection），**不能直接串调**——在 workspace_service 内联事务中调 conn 级内核：从 `create_artifact`(:99) 提炼 `_create_artifact_in_conn(conn,...)`（原方法改薄封装复用，行为不变），列/行写入同理照 `replace_columns_for_artifact`(:1035)/`insert_rows_upsert`(:1070) 提炼 conn 级内核。同事务完成：创建制品（kind=table、domain=workspace.domain_id、visibility=private、owner=默认 workspace.owner_user_id）+ owner 成员行 +（owner≠操作者时）操作者 editor 成员行 + 建列（`_infer_column_type` 样本推断六列型）+ rev0 行（`_row_key`=hex(_id)，**provenance=`[{"_source":..., "workspace":{"id","node","port","run_id"}}]`——数组口径，db_artifacts 既有不变量，UPSERT 拼接依赖数组**）+ `upsert_binding` + `insert_export`。④不写 batches（67号 §4.3 裁定）、不发布、不拨指针、无 MCP 开放。
- 响应 `{exports:[{node_id,port_id,artifact_id,name,status}]}` + 失败明细。
- **测试：** 正常导出→制品列表可见草稿且 current_revision=0；重复 request_id→同 artifact_id；stale 输出→item 失败；**制品被人工改动（updated_at 晚于 last_exported_at）→阻断**；绑定后重复导出同 port→新草稿写入既有制品（mode 仍 create_draft 首期语义=重写草稿，等价 64号 B.5「丢弃草稿不解除绑定」）。
- **步骤：** 测试→FAIL→实现→PASS→commit `feat(kb): 工作台导出——幂等request_id/单事务建私有草稿/绑定`。

### Task 10: 前端 API 客户端 + 路由菜单 + 列表页（vitest TDD）

**Files:**
- Create: `kb-ui/src/api/workspaces.ts`、`kb-ui/src/views/workspaces/WorkspacesListView.vue`
- Modify: `kb-ui/src/router/index.ts`（`/workspaces`+`/workspaces/:workspaceId`，挂 AppLayout children，照 artifacts 两路由 :80 形态）、`kb-ui/src/components/layout/Sidebar.vue`（ALL_NAV 增「制作工作台」置于知识制品前；**遵循文件头"顺序用户定稿"注释，不重排其他项**）
- Test: `kb-ui/src/api/__tests__/workspaces.spec.ts`（路径/方法断言，mock proxy）

**规格：** api 客户端照 `api/artifacts.ts` 模式（createProxyClient('mining')，端点与 Task 8/9 一一对应，类型定义 WorkSpace/GraphNode/DatasetEnvelope/RunStatus）；列表页=新建对话框（名称/描述）+ 工作区卡片列表（名称/负责人/节点数/更新时间）+ 点击进详情；新建成功 router.push 详情页。
**步骤：** spec→FAIL→实现→`npm run test` 过→`npm run build` 过→commit `feat(kb-ui): 工作台入口——api客户端/路由/菜单/列表页`。

### Task 11: 工作台主页三栏骨架 + 步骤流 + 输出渲染（vitest TDD）

**Files:**
- Create: `kb-ui/src/views/workspaces/WorkspaceDetailView.vue`、`kb-ui/src/components/workspaces/WorkspaceStepFlow.vue`、`kb-ui/src/components/workspaces/WorkspaceOutputTable.vue`
- Test: `components/workspaces/__tests__/WorkspaceStepFlow.spec.ts`、`WorkspaceOutputTable.spec.ts`

**规格：**
- 三栏布局（左 176px 来源+结果树 / 中 步骤流+输出区 / 右 300px Inspector）；顶栏=工作区名+返回列表+「运行工作区」主按钮+「导出制品草稿」；工具条=批次号+运行状态。
- StepFlow：垂直步骤卡（节点图标+名称+状态点：未执行/已完成/待更新 stale/配置待执行 pending）+每卡输出端口按钮（选中→输出区）；「+ 接入资料」「+ 添加算子」入口（弹 SourceDialog/算子选择）；节点输入用**下拉选择**（候选=无环上游端口，复用后端 graph 校验，前端同规则预过滤）。窄屏降级不需要（管理端桌面场景），但布局用 flex 不写死宽度。
- OutputTable：el-table 渲染 cols+行；`_source` 列固定右侧带 link 图标；JSON 单元格显示 `{ } 查看`（点击弹只读 JSON 弹窗）；空态「该输出尚未执行」。
- 详情页状态管理：graph（GET）+current outputs+轮询 run status（复用 usePolling；运行中每 2s；空闲停）。**本任务阶段右栏 Inspector 与 SourceDialog 用禁用占位**（Task 12 才创建接线，build 不许挂）。
- **步骤：** 组件 spec（步骤卡渲染/端口选择事件/状态点 class/JSON 弹窗）→FAIL→实现→test+build 过→commit `feat(kb-ui): 工作台主页——三栏/步骤流/输出表格渲染`。

### Task 12: Inspector 节点配置 + 试跑/执行交互 + 来源对话框（vitest TDD）

**Files:**
- Create: `kb-ui/src/components/workspaces/WorkspaceInspector.vue`、`WorkspaceSourceDialog.vue`
- Test: `__tests__/WorkspaceInspector.spec.ts`

**规格：**
- Inspector 三段：节点信息（op 名/输入端口选择器）→ **按 op 分派的参数表单**（select:列勾选+改名行；filter:字段下拉+条件+值；join:左右表(输入选择)+左右键+方式；split:级别单选；llm_extract:输入列+模板 textarea+输出字段行编辑器；source:类型+范围只读展示）→ 操作区（试跑 3 条 / 执行此节点）。
- 表单变更=本地草稿，「保存配置」PUT graph（expected_revision 乐观锁；409→提示刷新重拉）；保存成功后节点 pending 由后端 diff 标注，前端刷新 graph。
- 试跑=调 trial 端点，结果横幅展示在输出区顶部（试跑预览标签，退出按钮）；执行=POST runs scope=node→轮询到终态→刷新 current outputs；运行工作区=scope=workspace。
- SourceDialog：两类入口——kb 目录（域内 KB 下拉+目录多选 tag，提交即冻结，展示冻结计数）/ Excel（el-upload 选取→multipart 上传→回显行列数）。
- **步骤：** spec（op 分派表单渲染/保存乐观锁冲突提示/试跑横幅）→FAIL→实现→test+build 过→commit `feat(kb-ui): 工作台交互——inspector参数表单/试跑执行/来源接入`。

### Task 13: 导出对话框 + 端到端真库验收 + 收尾

**Files:**
- Create: `kb-ui/src/components/workspaces/WorkspaceExportDialog.vue`
- Create: `knowledge_mining/tests/kb/test_workspace_e2e.py`
- Test: `__tests__/WorkspaceExportDialog.spec.ts`

**导出对话框规格：** 多选已完成且非 stale 的输出端口（复选+禁用态说明原因）→ 逐项名称（默认=端口名）→ 责任人（默认工作区 owner，同域候选下拉）→ 确认→exports 端点→回执（制品名+「未发布」tag+跳转制品详情链接 `router.push(/artifacts/{id})`）。

**端到端验收（真库，B.3 材料）：**
1. 建域/用户/KB+2 文档（产品资料含 L1 章节：基本属性/技术规格/配套关系，P1/P2/P3 内容照 64号 B.3）+ Excel 库存表（P1/5、P3/0，上传）。
2. 建工作区→接 kb_dir 源→split_sections 章节级1→llm_extract（fake client：按文本正则出 型号/功率_W，保证确定性）→join 库存（left）→filter 功率≥2000→运行全区。
3. 断言：join left=3 行（P2 库存空）/filter 后 2 行（P1、P2）/inner=2 行（Task 4 单测已断言，e2e 可选）/**右表重复键**：重传含重复 P1 行的库存 Excel→重新 PUT source（批次+1）→重跑→断言 join 节点失败（partial）且其旧 current 输出行数不变（3 行）。
4. 导出 filter 输出→断言制品草稿 2 行、private、current_revision=0、行 provenance 含 workspace 血缘；重复 request_id 幂等。
5. 回归：`test_artifacts_routes.py`+`test_artifacts_db.py`+新增全部工作台测试文件容器内全绿；前端 `npm run test && npm run build` 全绿。
- **步骤：** e2e 测试→FAIL→补实现缺口→全绿→导出对话框实现→commit `feat(kb): 工作台MVP端到端——B.3材料全链验收+导出对话框`。

---

## 2. 执行顺序与依赖

Task 1→2→3 严格串行（表→登记→运行）；Task 4 依赖 1（无代码依赖可并行但测试需表无——**纯函数无需 DB，可与 2/3 并行**）；5 依赖 2；6 依赖 3+4+5；7 依赖 4；8 依赖 2-7；9 依赖 8；10-12 依赖 8 的路由面（10 只需客户端契约可提前）；13 收口。**默认串行执行（subagent-driven 单实现者），除非控制者明确并行派发无文件冲突的任务。**

## 3. 验收总门（Task 13 完成后）

- [ ] 容器内：工作台全部测试文件 + 既有制品回归（artifacts 四文件）全绿
- [ ] `cd kb-ui && npm run test` 全绿；`npm run build` 零错误
- [ ] 分支提交历史干净（每任务一 commit，无混入）
- [ ] 派最终 code-reviewer 子代理做整体审查（对照 67号+本计划）
- [ ] **不合并 master**（用户指令：不干扰主分支）
