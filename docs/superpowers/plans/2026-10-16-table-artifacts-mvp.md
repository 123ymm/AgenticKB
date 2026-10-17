# 表格知识制品 MVP 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 61号设计端到端落地表格知识制品：六张表迁移 → 制品服务域（CRUD/Excel 导入向导/发布快照版本/domain 成员权限）→ serving+mcp 消费面（`art_` 查询+artifacts 说明卡视图）→ kb-ui 前端 → 九个验收用例。

**Architecture:** 制品管理在 knowledge_mining（三层：routes/services/db，复用 KB 域模式与 openpyxl 基建）；消费面 mcp_server 不直连库、经 serving internal 端点（新 `art_` ref 前缀 + 复用 st_ 的 QuerySpec DSL 白名单解析）；版本=快照式（发布=行复制 rev0→N+schema 快照+拨指针）；权限=制品挂 domain+成员制，MCP 由密钥→用户→制品角色派生，serving 侧 SQL 谓词+404 反探测同口径。

**Tech Stack:** Python/FastAPI/psycopg(async)/openpyxl（均已有）；Java Spring/MyBatis（serving）；Vue3/element-plus/vitest（kb-ui）。**零新依赖（铁律）。**

**设计源:** `D:\mywork\AgenticKB\docs\下一阶段\61-表格知识制品MVP设计-2026-10-16.md`（61号）；探索结论已内嵌本计划。

**关键口径对齐（61号→落地修订，含审查修订）:**
- REST 前缀 `/api/artifacts`（mining 路由族无 `v1` 版本段，61号 §十 的 `/api/v1/` 对齐为 `/api/`；路径其余不变）。
- Agent 查询走新 `art_<artifact_id>` ref 前缀（不动 st_ 的 HMAC(snapshot) 体系），query DSL 与 st_ 同构（白名单字段/操作符，Java 侧复用 `StructureQueryDsl.parseSpec` 解析规则）。
- **发现通道**：serving internal 加 `/api/internal/artifact-list`（按用户列 published+成员的说明卡），mcp get_knowledge 顶层浏览附 `artifacts` 段——Agent 无上游 ref 也能发现制品（用例②前半）。
- **反探测统一 404**：非成员（无论同域跨域）一律 `ArtifactNotFound→404`，不暴露存在性（与 MCP 侧 artifact_not_found 同口径）；site_admin 与**本域 domain_admin** 均旁路全权（`db.py` `get_domain_role` 现成）。
- **乐观锁（61号 §七.2）**：所有编辑类端点（PATCH rows/批量赋值/列变更/import/publish/discard）可选带 `expected_updated_at`（ISO），服务端比对 `kb_artifacts.updated_at`，失配 409 `stale_artifact`。
- **alias 兼容读（用例⑨）**：Task 3 列定义读取携带 alias；Task 7 DSL 白名单先把旧列名经 alias 映射到当前列名再校验/查询。
- 追加导入的"模糊匹配自动建议"为增强项，MVP 手动映射。
- id 全 TEXT（uuid4().hex），对齐 kb 表惯例（61号 UUID 类型落地为 TEXT）。

---

## 文件结构总览

**Chunk 1 数据库迁移（7 处登记点）**
- Create: `databases/kb/schemas/017_kb_artifacts.sql`
- Create: `databases/migrations/core/20261016_007_kb_artifacts.sql`
- Modify: `databases/migrations/manifest.yaml`
- Modify: `knowledge_mining/mining/infra/pg_schema.py`（注册 017）
- Modify: `knowledge_mining/mining/maintenance/database_upgrade/contract.py`（CURRENT_SCHEMA_CHECKSUM、FORMAL_TABLES 53→59、:115 守卫、EXPECTED_FORMAL_TABLES）
- Modify: `agent_serving_java/src/main/java/com/coremasterkb/serving/observability/ServingRuntimeSchemaInitializer.java`（EXPECTED_SCHEMA_CHECKSUM）
- Modify: `db_tables.py`（EXPORT_TABLES +6）、`reset_db.py`、`tools/reset_domain_assets.py`、`databases/README.md`
- Test: `knowledge_mining/tests/maintenance/`（现有门禁自动覆盖，跑绿即可）

**Chunk 2 制品服务域（knowledge_mining/mining/kb/）**
- Create: `db_artifacts.py`（ArtifactDB：六表仓储+发布事务+diff SQL）
- Create: `services/artifact_service.py`（权限断言+生命周期+ensure_draft）
- Create: `services/artifact_import_service.py`（xlsx 解析/类型推断/列映射/校验/预览 token）
- Create: `routes/artifacts.py`（61号 §十 全部端点）
- Modify: `deps.py`（装配）
- Modify: `mining/api/app.py`（include_router；确认 auth_guard 无需白名单——面向用户路由）
- Test: `knowledge_mining/tests/kb/test_artifacts_db.py`、`test_artifacts_service.py`、`test_artifacts_import.py`、`test_artifacts_routes.py`

**Chunk 3 消费面（serving + mcp）**
- Create: `agent_serving_java/.../operator/mapper/ArtifactToolMapper.java` + `resources/mapper/ArtifactToolMapper.xml`
- Create: `agent_serving_java/.../structure/ArtifactQueryService.java`（成员谓词+DSL 校验+jsonb 查询组装）
- Create: `agent_serving_java/.../structure/InternalArtifactController.java`（`/api/internal/artifact-query`，withAuth 同构）
- Modify: `mcp_server/server.py`（get_knowledge 加 `art_` 分支）
- Modify: `mcp_server/tools.py`（query_artifact 转发）
- Test: `agent_serving_java/src/test/java/.../ArtifactQueryServiceTest.java`、`mcp_server/tests/test_artifact_tools.py`

**Chunk 4 kb-ui 前端**
- Create: `src/api/artifacts.ts`、`src/types/artifacts.ts`
- Create: `src/views/artifacts/ArtifactsListView.vue`、`src/views/artifacts/ArtifactDetailView.vue`
- Create: `src/components/artifacts/ArtifactImportWizard.vue`（渐进卡片：选文件→列映射→预览确认）
- Create: `src/components/artifacts/ArtifactSchemaPanel.vue`、`ArtifactRowsPanel.vue`、`ArtifactMembersPanel.vue`、`ArtifactPublishDialog.vue`（diff+usage_note）、`ArtifactVersionsPanel.vue`
- Modify: `src/router/index.ts`（/artifacts、/artifacts/:id）
- Test: `src/views/artifacts/__tests__/ArtifactsListView.spec.ts` 等

**Chunk 5 验收与门禁**
- Create: `knowledge_mining/tests/kb/test_artifact_acceptance.py`（九用例服务级串联）
- 全量门禁：pytest（maintenance+kb）→ vitest → npm run build → mvn test

---

## Chunk 1: 数据库迁移

### Task 1: 017 迁移文件 + core 版本化迁移

**Files:**
- Create: `databases/kb/schemas/017_kb_artifacts.sql`
- Create: `databases/migrations/core/20261016_007_kb_artifacts.sql`（内容=017 同文，双份 DDL 铁律）

- [ ] **Step 1: 写 017_kb_artifacts.sql**

```sql
-- 017（61号表格知识制品 MVP）：知识制品六张表。
-- 制品挂 domain、与库权限解绑（策展开放）；行地址=业务主键 row_key（ordinal 仅展示）；
-- jsonb 三字段分工：data=业务值 / provenance=来源账 / conflicts=质量标记，永不混装。
-- 身份列故意无 FK（对齐 kb 表惯例：跨表身份用 TEXT 引用，不建强外键）。

CREATE TABLE IF NOT EXISTS kb_artifacts (
    artifact_id      TEXT PRIMARY KEY,
    domain_id        TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'table'
                     CONSTRAINT ck_kb_artifacts_kind CHECK (kind IN ('table','wiki')),
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    usage_note       TEXT NOT NULL DEFAULT '',
    owner_user_id    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CONSTRAINT ck_kb_artifacts_status CHECK (status IN ('draft','published','deprecated')),
    current_revision INTEGER NOT NULL DEFAULT 0,
    draft_open       BOOLEAN NOT NULL DEFAULT FALSE,   -- draft 是否已 checkout（区分"从未检出"与"检出后删光"）
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_artifacts_domain ON kb_artifacts(domain_id);

CREATE TABLE IF NOT EXISTS kb_artifact_revisions (
    artifact_id    TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    schema_json    JSONB NOT NULL,
    row_count      INTEGER NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    published_by   TEXT NOT NULL,
    published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id, revision)
);

CREATE TABLE IF NOT EXISTS kb_artifact_members (
    artifact_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer'
                CONSTRAINT ck_kb_artifact_members_role CHECK (role IN ('owner','editor','viewer')),
    PRIMARY KEY (artifact_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_artifact_members_user ON kb_artifact_members(user_id);

CREATE TABLE IF NOT EXISTS kb_artifact_batches (
    batch_id    TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'upload'
                CONSTRAINT ck_kb_artifact_batches_channel CHECK (channel IN ('upload','kb_import')),
    file_name   TEXT NOT NULL DEFAULT '',
    sheet_name  TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_artifact_batches_artifact ON kb_artifact_batches(artifact_id);

CREATE TABLE IF NOT EXISTS kb_table_columns (
    artifact_id    TEXT NOT NULL,
    column_id      TEXT NOT NULL,
    column_name    TEXT NOT NULL,
    column_type    TEXT NOT NULL DEFAULT 'text'
                   CONSTRAINT ck_kb_table_columns_type
                   CHECK (column_type IN ('text','int','float','date','enum','bool')),
    enum_values    JSONB,
    is_primary_key BOOLEAN NOT NULL DEFAULT FALSE,
    is_indexed     BOOLEAN NOT NULL DEFAULT FALSE,
    alias          JSONB NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT NOT NULL DEFAULT 'active'
                   CONSTRAINT ck_kb_table_columns_status CHECK (status IN ('active','dropped')),
    ordinal        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (artifact_id, column_id)
);

CREATE TABLE IF NOT EXISTS kb_table_rows (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,          -- 0=draft 工作区；N=第 N 次发布快照
    row_key     TEXT NOT NULL,             -- 业务主键值拼接；未声明主键时为 uuid4.hex
    data        JSONB NOT NULL,
    provenance  JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicts   JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artifact_id, revision, row_key)
);
-- 查询主路径 (artifact_id, revision) 已被 PK 前缀覆盖；发布时按 is_indexed 列建表达式索引（服务层动态命名 idx_kbtr_{artifact}_{column}）。
```

- [ ] **Step 2: 复制同文到 core/20261016_007_kb_artifacts.sql**（首行注释改"存量库版本化迁移，与启动链 017 同文"）
- [ ] **Step 3: manifest.yaml 追加条目**（id `core/20261016_007_kb_artifacts`，mode `online_expand`，checksum 由 Step4 命令生成后填入）
- [ ] **Step 4: 生成 checksum 并三处镜像刷新**

```bash
python -c "from knowledge_mining.mining.maintenance.database_upgrade.manifest import manifest_checksum; print(manifest_checksum('databases/migrations/manifest.yaml'))"
```
把结果写入：① manifest.yaml 新条目 checksum 字段（先填占位再重算整体）→ ② `knowledge_mining/mining/maintenance/database_upgrade/contract.py` 的 `CURRENT_SCHEMA_CHECKSUM` → ③ `ServingRuntimeSchemaInitializer.java` 的 `EXPECTED_SCHEMA_CHECKSUM`。注意：manifest 整体 checksum 与单条 checksum 是两个值——按 `test_migration_manifest.py:276-300` 的断言口径刷（先看该测试怎么取值，照做）。

### Task 2: 七处登记点

**Files:**
- Modify: `knowledge_mining/mining/infra/pg_schema.py`（常量 `KB_ARTIFACTS_SCHEMA = .../017_kb_artifacts.sql` 并按依赖顺序插入 `domain_schema_paths()` 与 primary 清单，位置在 016 之后）
- Modify: `knowledge_mining/mining/maintenance/database_upgrade/contract.py`：`FORMAL_TABLES` 六张新表加入（53→59 项）；**第 112-113 行守卫** `EXPECTED_FORMAL_TABLES != 53` 改 `!= 59`
- Modify: `knowledge_mining/tests/maintenance/test_database_upgrade_contract.py:64-65`（**第八处登记**）：硬编码 `EXPECTED_FORMAL_TABLES == 53`/`EXPECTED_PHYSICAL_TABLES == 54` 改 59/60
- Modify: `db_tables.py` `EXPORT_TABLES` 六表加入
- Modify: `reset_db.py`、`tools/reset_domain_assets.py`（后者维护**自己的** `V2_TABLES`/`TRUNCATE_TABLES` 清单——六张制品表是 domain 范围数据，纳入其清单并注释）
- Modify: `databases/README.md`（无现成表登记清单——新增 kb artifacts 小节说明六表；顺手补目录结构缺的 `kb/` 条目）

- [ ] **Step 1: 逐文件修改**（先 Read 再 Edit，全部显式路径）
- [ ] **Step 2: 跑静态门禁**

Run: `python -m pytest knowledge_mining/tests/maintenance -q`
Expected: 全绿（test_bootstrap_table_census 校验 59 张=FORMAL_TABLES=EXPORT_TABLES；test_migration_manifest 校验三镜像一致）。红则按断言信息补漏登记。

- [ ] **Step 3: 真库 schema 冒烟（如有 kb_db_test 凭据）**

Run: `PG_DBNAME=kb_db_test KB_ALLOW_TEST_TRUNCATE=1 KB_RUN_POSTGRES_ACCEPTANCE=1 python -m pytest knowledge_mining/tests/maintenance -m postgres -q`
Expected: 迁移在测试库幂等应用成功。（凭据不可用则记 TODO，最终验证阶段补）

- [ ] **Step 4: Commit**

```bash
git add databases/kb/schemas/017_kb_artifacts.sql databases/migrations/core/20261016_007_kb_artifacts.sql databases/migrations/manifest.yaml knowledge_mining/mining/infra/pg_schema.py knowledge_mining/mining/maintenance/database_upgrade/contract.py db_tables.py reset_db.py tools/reset_domain_assets.py databases/README.md agent_serving_java/src/main/java/com/coremasterkb/serving/observability/ServingRuntimeSchemaInitializer.java
git commit -m "feat(kb): 制品六表迁移017+core007（61号批次1）——三处镜像与门禁同步"
```

---

## Chunk 2: 制品服务域

### Task 3: ArtifactDB 仓储层

**Files:**
- Create: `knowledge_mining/mining/kb/db_artifacts.py`
- Test: `knowledge_mining/tests/kb/test_artifacts_db.py`

仓储风格照抄 `mining/kb/db.py` 文件头契约（TEXT id/参数化 SQL/dict_row/多语句显式 `conn.transaction()`）。核心方法清单（TDD 顺序）：

```
类 ArtifactDB(pool)
  create_artifact(domain_id, kind, title, description, owner) -> dict   # 同时插 members(owner)
  get_artifact(artifact_id) -> dict | None
  update_artifact_meta(artifact_id, title?, description?, usage_note?) -> dict
  delete_artifact(artifact_id)                                          # 级联删五表（显式 DELETE 顺序）
  list_artifacts_for_user(user_id) -> [dict]                            # join members；含 role
  list_members / upsert_member / remove_member
  # 生命周期
  publish(artifact_id, change_summary, published_by) -> int             # ★发布事务（见下）
  rollback_to(artifact_id, revision)                                    # 拨指针
  deprecate(artifact_id)
  discard_draft(artifact_id)                                            # DELETE revision=0
  ensure_draft(artifact_id)                                             # rev0 空 且 current>0 → copy
  # schema
  list_columns / upsert_column / drop_column(软删) / set_primary_key(重算 row_key)
  # 行
  insert_rows(artifact_id, rows, batch_meta) -> dict                    # 批量+UPSERT by row_key
  update_cell / batch_assign / delete_row / list_rows(rev, page, size, sort)
  count_rows(revision)
  diff_draft_vs_current(artifact_id) -> {added,removed,changed:[{row_key,col,old,new}]}
  artifact_role(artifact_id, user_id) -> str | None                     # 权限判定用
  create_expression_indexes(artifact_id, revision, columns)             # 发布后动态建
```

- [ ] **Step 1: 写失败测试**（tests/kb/test_artifacts_db.py，fixture 用 `tests/kb/conftest.py` 的 `async_pool`；真库门控标 `@pytest.mark.postgres` 与现有 kb 测试同款）：create→insert_rows→publish→行数与指针断言；diff 三个场景；discard/rollback；delete 级联。
- [ ] **Step 2: 跑测试确认失败**（`python -m pytest knowledge_mining/tests/kb/test_artifacts_db.py -q`，ModuleNotFoundError 预期）
- [ ] **Step 3: 实现 db_artifacts.py**。发布事务核心（其余方法直白）：

```python
async def publish(self, artifact_id: str, change_summary: str, published_by: str) -> int:
    async with self._pool.connection() as conn:
        async with conn.transaction():
            art = await self._lock_artifact(conn, artifact_id)  # SELECT ... FOR UPDATE
            if art["status"] not in ("draft", "published", "deprecated"):
                raise ArtifactError("invalid_status")
            if not art["usage_note"].strip():
                raise ArtifactError("usage_note_required")
            new_rev = art["current_revision"] + 1
            cols = await self._active_columns(conn, artifact_id)
            n = await conn.fetchval(
                "INSERT INTO kb_table_rows (artifact_id, revision, row_key, data, provenance, conflicts, ordinal, updated_by) "
                "SELECT artifact_id, %s, row_key, data, provenance, conflicts, ordinal, %s FROM kb_table_rows "
                "WHERE artifact_id=%s AND revision=0 RETURNING 1", (new_rev, published_by, artifact_id))
            # fetchval 只取首行——用 EXECUTE ...row_count 或 cursor；按 psycopg 写法取 rowcount
            await conn.execute(
                "INSERT INTO kb_artifact_revisions (artifact_id, revision, schema_json, row_count, change_summary, published_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)", (artifact_id, new_rev, json.dumps(cols), rowcount, change_summary, published_by))
            await conn.execute("DELETE FROM kb_table_rows WHERE artifact_id=%s AND revision=0", (artifact_id,))
            await conn.execute("UPDATE kb_artifacts SET current_revision=%s, status='published', updated_at=now() WHERE artifact_id=%s", (new_rev, artifact_id))
            return new_rev
    # 事务外：create_expression_indexes(artifact_id, new_rev, indexed_cols)
```

diff 核心（双侧 CTE + FULL JOIN，过滤全部移入 CTE——WHERE 过滤会丢 removed 行，审查修正）：

```sql
WITH d AS (SELECT row_key, data FROM kb_table_rows WHERE artifact_id=%(id)s AND revision=0),
     c AS (SELECT row_key, data FROM kb_table_rows WHERE artifact_id=%(id)s AND revision=%(cur)s)
SELECT COALESCE(d.row_key, c.row_key) AS row_key,
       (d.row_key IS NULL) AS added, (c.row_key IS NULL) AS removed,
       d.data AS old_data, c.data AS new_data
FROM d FULL JOIN c ON d.row_key=c.row_key
WHERE d.row_key IS NULL OR c.row_key IS NULL OR d.data IS DISTINCT FROM c.data
```
列级 changed 在 Python 侧对比 jsonb。`ensure_draft` 用 `kb_artifacts.draft_open` 显式状态（FALSE 且 current>0 → checkout 复制+置 TRUE；publish/discard 置 FALSE），避免"检出后删光"被误判复活。

- [ ] **Step 4: 跑测试至绿**
- [ ] **Step 5: Commit**（`git add` 显式两路径；`feat(kb): 制品仓储层——发布事务/diff/ensure_draft`）

### Task 4: ArtifactService 服务层（权限+生命周期编排）

**Files:**
- Create: `knowledge_mining/mining/kb/services/artifact_service.py`
- Test: `knowledge_mining/tests/kb/test_artifacts_service.py`

照 `kb_service.py` 模式：业务异常类（ArtifactNotFound/ArtifactForbidden/UsageNoteRequired/PrimaryKeyConflict/StaleArtifact/InvalidOperation），权限断言（审查修正：反探测统一 404+域管理员旁路）：

```python
def _assert_role(self, user, artifact, *allowed):   # owner>editor>viewer
    if user.site_role == "admin": return
    # 域管理员对本域制品全权（db.py get_domain_role 现成）
    if await self._is_domain_admin(user, artifact.domain_id): return
    role = self._role_of(user, artifact)   # members 行 or owner_user_id
    if role is None:
        raise ArtifactNotFound()           # ★非成员一律 404（含同域非成员——不暴露存在性）
    if role not in allowed:
        raise ArtifactForbidden()
# 设计态动作允许 ("owner","editor")；运行态读允许 ("owner","editor","viewer")；删除/成员管理仅 owner
# 所有变更动作（编辑/列/导入/publish/discard）可选 expected_updated_at 校验 → 失配 StaleArtifact(409)
```

服务方法编排仓储（publish 前调 import/编辑产生的 conflicts 汇总提示；set_primary_key 重算；list_rows 附 schema）。

- [ ] **Step 1: 失败测试**（权限矩阵全组合：site_admin/owner/editor/viewer/非成员×动作；usage_note 阻断；404 反探测——非本域用户拿 NotFound 而非 Forbidden）
- [ ] **Step 2/3/4: 实现至绿**
- [ ] **Step 5: Commit**（`feat(kb): 制品服务层——domain成员权限矩阵与生命周期编排`）

### Task 5: Excel 导入向导服务

**Files:**
- Create: `knowledge_mining/mining/kb/services/artifact_import_service.py`
- Test: `knowledge_mining/tests/kb/test_artifacts_import.py`

复用 `user_import_service.py` 的 openpyxl 模式（zipfile 炸弹检查/公式注入检查照抄），护栏改为：**≤200 列、≤100,000 行、文件 ≤50MB（宽表 10万行×200列可超 20MB，取 50MB）、解压 ≤200MB**。

三个入口（TDD）：

```
parse_upload(file_bytes, name) -> preview_token, preview
  # 临时文件存 tempfile.gettempdir()/kb_artifact_upload_{token}.xlsx；token=uuid4.hex
  # 内存 dict _PREVIEWS[token]={path, file_name, sheets:[...], parsed_at}（TTL 1h 惰性清理）
  # preview = {sheets, header(row1 默认), columns:[{source_name, inferred_type, enum_candidates}], sample_rows(50)}
  # 类型推断：抽样 200 行——全 int→int；全数值→float；全 ISO 日期→date；
  #           distinct≤50 且非全数字→enum（列候选值）；否则 text
build_mapping(preview_token, sheet, header_row, column_ops) -> mapping_plan
  # 建表模式：ops=[{source, target_name, type, is_primary_key?, is_indexed?, action:'new'}]
  # 追加模式：action∈{map_to:<column_id>, new, drop}；schema 缺列→提示置 null 清单
execute_import(token, plan, conflict_policy) -> import_report
  # 重读临时文件流式解析 → 类型 cast（失败按 policy：'strict' 阻断 / 'tolerant' 落 conflicts）
  # row_key=主键值 join('/')；无主键→uuid4.hex
  # 主键冲突（文件内重复）：'block'(默认,报错) / 'keep_first'+conflicts
  # 与已有行冲突：UPSERT（命中→data 覆盖+provenance append；未命中→插入）
  # 建 batch 记录；返回 {inserted, updated, conflicts:[...], diff_vs_current(仅追加且已发布)}
```

- [ ] **Step 1: 失败测试**——openpyxl 现造两份 xlsx（列名不同的 MOP 表，61号用例数据）；断言类型推断（枚举列 L1分类）、映射三选、UPSERT+provenance 双来源、公式注入拒绝、超限拒绝。
- [ ] **Step 2/3/4: 实现至绿**（openpyxl `read_only=True` 流式读）
- [ ] **Step 5: Commit**（`feat(kb): Excel三步导入向导服务——类型推断/列映射/UPSERT`）

### Task 6: 路由层 + 装配

**Files:**
- Create: `knowledge_mining/mining/kb/routes/artifacts.py`
- Modify: `knowledge_mining/mining/kb/deps.py`（get_artifact_db/get_artifact_service/get_import_service）
- Modify: `knowledge_mining/mining/api/app.py:264-287`（include_router，**静态段 `/api/artifacts` 无动态段冲突，但必须排在 `/api/kb/{kb_id}` 之前无所谓——前缀不同；仍按"静态先于动态"插入**）
- Test: `knowledge_mining/tests/kb/test_artifacts_routes.py`

端点全表（61号 §十；前缀 `APIRouter(prefix="/api/artifacts")`）：POST ``、GET ``、GET/PATCH/DELETE `/{artifact_id}`、POST `/{id}/publish`、`/{id}/rollback/{revision}`、`/{id}/deprecate`、`/{id}/discard`、GET `/{id}/diff`、GET/POST/PATCH/DELETE `/{id}/members`、POST `/{id}/table/upload`(multipart)、GET `/{id}/table/preview/{token}`、POST `/{id}/table/import`、GET `/{id}/table/columns`、POST `/{id}/table/columns`、PATCH `/{id}/table/columns/{column_id}`、GET `/{id}/table/rows`(query: draft/page/size)、PATCH `/{id}/table/rows/{row_key}`、POST `/{id}/table/rows/batch-assign`、DELETE `/{id}/table/rows/{row_key}`。

全部薄 handler：`Depends(current_user)` + service 调用 + `_map_error`（ArtifactNotFound→404、ArtifactForbidden→403、UsageNoteRequired→422、PrimaryKeyConflict→409、StaleArtifact→409 stale_artifact）。upload 端点必须走 content-length 预检（对齐 `auth_guard.py:85-92` 模式，50MB）。

- [ ] **Step 1: 失败测试**（`_client()` 模式：手工 FastAPI+include_router+httpx ASGITransport；覆盖：创建→上传→import→publish→rows 查询全链 200/403/404；usage_note 422）
- [ ] **Step 2/3/4: 实现至绿**
- [ ] **Step 5: Commit**（`feat(kb): 制品REST全端点+装配（61号批次2完）`）

---

## Chunk 3: serving + mcp 消费面

### Task 7: serving ArtifactQueryService + Mapper + Internal Controller

**Files:**
- Create: `agent_serving_java/src/main/java/com/coremasterkb/serving/structure/ArtifactQueryService.java`
- Create: `agent_serving_java/src/main/java/com/coremasterkb/serving/operator/mapper/ArtifactToolMapper.java`
- Create: `agent_serving_java/src/main/resources/mapper/ArtifactToolMapper.xml`
- Create: `agent_serving_java/src/main/java/com/coremasterkb/serving/structure/InternalArtifactController.java`
- Test: `agent_serving_java/src/test/java/com/coremasterkb/serving/structure/ArtifactQueryServiceTest.java`

行为契约（对齐 st_ 语义，不动现有类；**审查修订：发现端点+provenance+alias 展开**）：

```
POST /api/internal/artifact-query   (withAuth: X-Internal-Auth 常时比较)
body: {artifact_ref:"art_<hex>", query:{select,where,order_by,limit,cursor,aggregate}, domain, username, is_admin}
1. 解析 ref（art_ 前缀 → artifact_id）
2. 权限谓词（mapper 单 SQL，404 反探测——不存在/无权/已停用同抛 artifact_not_found）：
   SELECT a.kind,a.status,a.current_revision,a.title,a.description,a.usage_note, m.role
   FROM kb_artifacts a LEFT JOIN kb_artifact_members m
     ON m.artifact_id=a.artifact_id AND m.user_id=#{username}
   WHERE a.artifact_id=#{id} AND a.status='published'
     AND (#{is_admin} OR m.role IS NOT NULL OR a.owner_user_id=#{username})
     -- 域校验：body.domain 必须等于 a.domain_id（域路由已在 DomainContext）
3. 只传 ref → capabilities 视图：{view:"artifact_card", artifact_ref, title, description, usage_note,
   revision, row_count, columns:[{name,type,sortable,can_aggregate,operations}]}（列定义读 revisions.schema_json）
4. 带 query → DSL 校验（复用 StructureQueryDsl.parseSpec 的字段白名单与操作符集）：
   ★alias 展开：DSL 中的字段名先经 alias 映射到当前列名再过白名单（用例⑨：旧列名兼容读）
   → mapper 查 kb_table_rows WHERE artifact_id=? AND revision=指针值：
     select 字段白名单= schema 列名；data ->> #{field} 文本比较 / (data->>#{field})::numeric 数值聚合
     （模板照抄 StructureToolMapper.xml 的 criterion 片段改 jsonb 路径；cursor=base64(row_key) 轻量编解码）
5. 返回 {view:"artifact_rows", artifact_ref, columns, rows:[{row_key, data, provenance}], cursor, has_more}
   ★provenance 随行返回（用例②通过标准"返回整行+provenance"）
   aggregate → {view:"artifact_aggregate", aggregate:{op,field,value,row_count}}

POST /api/internal/artifact-list    (withAuth 同上)  ★发现通道
body: {domain, username, is_admin}
→ {view:"artifacts", artifacts:[{artifact_ref:"art_<hex>", title, description, usage_note,
   revision, row_count, kind}], cursor, has_more}
   SQL：SELECT ... FROM kb_artifacts a JOIN/权限谓词同上 WHERE status='published' AND domain_id=#{domain}
        ORDER BY updated_at DESC LIMIT...（分页走 updated_at+artifact_id keyset）

Task7 测试必须显式断言：is_admin=true 越权面（跨域 is_admin 查询仍放行——与 KbAccessService 站点admin 全通一致）、
deprecated 被谓词滤除（用例⑧ MCP 不可见）、rollback 后 status 保持 published、provenance 字段在 rows 里、
alias 旧名查询映射正确。
```

- [ ] **Step 1: 失败测试**（ServiceTest：mock mapper，覆盖权限矩阵/DSL 拒绝未知列/视图三形态；纯 JUnit 无 DB）
- [ ] **Step 2/3/4: 实现至绿**（mvn -q -pl . test -Dtest=ArtifactQueryServiceTest 或整模块）
- [ ] **Step 5: Commit**（`feat(serving): art_制品查询通道——DSL白名单/成员谓词/三视图`）

### Task 8: mcp_server get_knowledge 接线

**Files:**
- Modify: `mcp_server/server.py`（`_get_by_ref` 加分支：ref.startswith("art_") → `query_artifact(ref, query)`，响应 `{**上游, "view": 上游已带}`；**顶层浏览（`_browse_top`）响应附 `artifacts` 段**：调 `list_artifacts()` 取说明卡清单拼入 kb_tree 同款响应——Agent 无 ref 也能发现制品，用例②前半）
- Modify: `mcp_server/tools.py`（`query_artifact` → POST `/api/internal/artifact-query`；`list_artifacts` → POST `/api/internal/artifact-list`；`_post_serving` 同族，带 identity headers）
- Test: `mcp_server/tests/test_artifact_tools.py`

- [ ] **Step 1: 失败测试**（httpx mock：art_ ref 转发路径/身份头/view 透传/artifact-list 转发与 artifacts 段拼接/错误转译 artifact_not_found→用户可见错误）
- [ ] **Step 2/3/4: 实现至绿**（`python -m pytest mcp_server/tests -q`）
- [ ] **Step 5: Commit**（`feat(mcp): get_knowledge接art_制品视图（61号批次3完）`）

---

## Chunk 4: kb-ui 前端

### Task 9: API 层 + 类型

**Files:**
- Create: `kb-ui/src/types/artifacts.ts`、`kb-ui/src/api/artifacts.ts`
- Test: `kb-ui/src/api/__tests__/artifacts.spec.ts`

`useArtifactsApi()` 工厂（createProxyClient('mining')）：全端点方法 + multipart（FormData 就地建，对齐 kb.ts 模式）。

- [ ] **Step 1-4: TDD**（类型+方法存在性/路径/Formdata 组装断言）
- [ ] **Step 5: Commit**（`feat(ui): 制品API层`）

### Task 10: 列表页 + 详情页骨架 + 路由

**Files:**
- Create: `kb-ui/src/views/artifacts/ArtifactsListView.vue`（el-table：标题/kind/状态 tag/当前版本/我的角色/更新时间；新建 dialog=标题+描述；行进详情）
- Create: `kb-ui/src/views/artifacts/ArtifactDetailView.vue`（el-tabs：设计态/运行态/成员/版本；设计态=向导+Schema 面板+行编辑面板；运行态=说明卡+只读行+发布按钮入口）
- Modify: `kb-ui/src/router/index.ts`（children 加 `/artifacts`、`/artifacts/:artifactId`，普通成员可达，不进 ADMIN_ROUTES）
- Modify: `kb-ui/src/components/layout/Sidebar.vue:66-77`（菜单表加 `/artifacts` 项 access:'all'——硬编码菜单不加则只能 URL 直达；同步检查 `Sidebar.spec.ts` 菜单断言）
- Test: `src/views/artifacts/__tests__/ArtifactsListView.spec.ts`（vi.mock api+element-plus 模板照 WorkflowListView.spec.ts）

- [ ] **Step 1-4: TDD → 实现**（`npm test -- artifacts`）
- [ ] **Step 5: Commit**（`feat(ui): 制品列表+详情骨架+路由`）

### Task 11: 导入向导（渐进卡片）+ 行面板 + Schema 面板

**Files:**
- Create: `kb-ui/src/components/artifacts/ArtifactImportWizard.vue`——三张编号 el-card 渐进披露（范式=OnenetAdminView）：①选文件（el-upload http-request 自定义+accept .xlsx）→②列映射（左=文件列、右=目标列 el-select：追加模式选项来自已有 schema；action 三选 radio：映射/新建/丢弃；主键 checkbox 唯一）→③预览确认（前 50 行 el-table + 类型失败/主键冲突 tag + 冲突策略 select）→执行 import
- Create: `kb-ui/src/components/artifacts/ArtifactRowsPanel.vue`（服务端分页 el-table；行操作：编辑 dialog（el-form 逐列输入，enum 列 el-select）/删除；批量赋值 dialog（列+条件+值）；draft 切换开关）
- Create: `kb-ui/src/components/artifacts/ArtifactSchemaPanel.vue`（列卡片列表：改名/类型 select/枚举值 tag 编辑/主键/索引/软删）
- Test: `__tests__/ArtifactImportWizard.spec.ts`、`ArtifactRowsPanel.spec.ts`（映射三选交互/主键互斥/批量赋值 payload）

- [ ] **Step 1-4: TDD → 实现**
- [ ] **Step 5: Commit**（`feat(ui): 三步导入向导+行编辑+schema面板`）

### Task 12: 发布 diff 对话框 + 成员/版本面板

**Files:**
- Create: `kb-ui/src/components/artifacts/ArtifactPublishDialog.vue`（打开先 GET diff：新增/修改/删除三个 collapse 计数+明细表（修改=列级 old→new）；usage_note 必填 textarea；确认调 publish）
- Create: `kb-ui/src/components/artifacts/ArtifactMembersPanel.vue`（照 KbMembersPanel：加成员+角色 select+移除）
- Create: `kb-ui/src/components/artifacts/ArtifactVersionsPanel.vue`（版本 el-table：rev/行数/摘要/发布人/时间；当前 tag；回滚按钮 confirm）
- Test: `__tests__/ArtifactPublishDialog.spec.ts`（diff 渲染/usage_note 空阻断）

- [ ] **Step 1-4: TDD → 实现**
- [ ] **Step 5: Commit**（`feat(ui): 发布diff+成员+版本面板（61号批次4完）`）

---

## Chunk 5: 九用例验收与最终门禁

### Task 13: 九用例验收测试（61号 §十二）

**Files:**
- Create: `knowledge_mining/tests/kb/test_artifact_acceptance.py`（服务级串联，真库门控同款）

用例映射：①建表导入发布 rev1（schema 快照含 enum+索引存在断言）②Agent 侧由 Task7/8 测试覆盖（artifact-list 发现+art_ 查询返回 provenance，引用说明）③追加导入列映射 UPSERT+双 provenance+diff 数字 ④回滚往返 ⑤discard 复原 ⑥权限矩阵（Task4 已单测，此处路由级复验 viewer 404 设计态+非成员 404）⑦usage_note 阻断 ⑧停用→不可见→恢复 ⑨alias 改名兼容读（Task3 列定义携带 alias+Task7 白名单展开，双侧断言）。

- [ ] **Step 1-3: 写测试跑绿**（`PG_DBNAME=kb_db_test ... -m postgres`；无凭据则 CI 标记跳过并记录）
- [ ] **Step 4: Commit**（`test(kb): 61号九用例验收串联`）

### Task 14: 全量门禁 + 最终验证

- [ ] `python -m pytest knowledge_mining/tests -q`（全量 Python）
- [ ] `python -m pytest mcp_server/tests -q`
- [ ] `cd agent_serving_java && mvn -q test`（与其它门禁**串行**跑——铁律）
- [ ] `cd kb-ui && npm test`、`npm run build`（**类型验证以 build 为准**——vue-tsc --noEmit 假通过坑）
- [ ] `git add`（显式路径，**绝不 -A**）+ 最终 commit `feat(kb): 表格知识制品MVP端到端（61号）`
- [ ] 汇报：九用例结果表+门禁清单+分支位置

---

## 风险与坑（探索确认，执行者必读）

1. **三处镜像校验和**漏刷=服务启动 fail-closed（fa49f1c 事故先例）——Task1 Step4 是硬门禁。
2. `test_bootstrap_table_census` 要求启动链表集合**精确等于** FORMAL_TABLES=EXPORT_TABLES——六张全登记，多一张少一张都红。
3. app.py 路由注册顺序：静态字面量先于动态段（`/api/artifacts` 与 `/api/kb/{kb_id}` 前缀不同无冲突，但仍按惯例插入）。
4. `auth_guard` 的 `_SERVICE_ONLY_ROUTES` 只登记服务间内部端点——面向用户 `/api/artifacts/*` 不登记；upload 端点自带 50MB content-length 预检。
5. 临时预览文件落系统 temp 目录+TTL 1h 惰性清理；确认导入**重读文件**（不把 10 万行驻内存）。
6. psycopg `fetchval` 对 INSERT...SELECT 不返回行数——用 `cursor.rowcount` 或 `RETURNING 1` 计数模式。
7. Java 测试与 mvn 门禁、vitest 与 build：**串行执行**（资源与输出冲突）。
8. git add 一律显式路径（历史密钥事故）；`main_control_service/config/system/llm_service.yaml` 永不碰。
