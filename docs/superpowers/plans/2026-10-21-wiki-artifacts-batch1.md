# Wiki 知识制品 MVP · 批次1「通用底座」实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 63号设计的第一层——数据库通用底座（来源绑定表 + batches 通用化 + wiki 三张专有表 + 检索单元 HNSW）与 kind 创建链路放开（后端路由 + 前端类型选择）。

**Architecture:** 迁移 010（online_expand）+ 启动链 017 改/018 新建（**含 pg_schema.py 显式登记**——启动链是硬编码元组不是自动发现）+ 十处登记三镜像刷新；路由层放开 `kind` 参数（service/db 层 61号已支持，只差路由与前端）；前端创建对话框加类型选择、详情页对 wiki 加占位守卫（内容能力属批次2/3）。本批次**不含** wiki 页面服务域、编译管线、检索——那些是后续批次，各出各的计划。

**Tech Stack:** PostgreSQL 15 + pgvector（HNSW，009 同款守卫）；Python/FastAPI（mining 服务）；Vue3 + Element Plus（kb-ui）；Java 仅改一个校验和常量，无逻辑。

**上游设计：** `docs/下一阶段/63-Wiki类知识制品MVP设计-与表格制品逐层对照-2026-10-21.md`（V1.1，已随本分支提交）
**直接参照：** `docs/superpowers/plans/2026-10-16-table-artifacts-mvp.md`（61号批次1，登记点与测试口径同源）

**铁律（违反即返工）：**
- 依赖冻结：不新增任何依赖包（pyproject/pom/package.json 不动）。
- git add 只写显式路径，禁止 `git add -A`/`git add .`。
- 前端类型验证以 `npm run build` 为准（vue-tsc --noEmit 在本仓库是假通过）。
- Java 改动本批次只动 `ServingRuntimeSchemaInitializer` 一个常量。
- 测试全部跑在 docker（宿主无 Python）：`agentickb-release-validation:a74b940` 镜像（**若本地无此 tag，先 `docker images | grep agentickb` 找现役 tag 替换**）；Java 用 `maven:3.9-eclipse-temurin-21` + 宿主 .m2 挂载。
- Windows checkout 的 CRLF 会坑测试断言与校验和——校验和命令必须带 `\r\n→\n` 归一（manifest.py `_file_checksum` 同口径）。

---

## File Structure（本批次改动地图）

```
databases/
  migrations/core/20261021_010_wiki_artifacts.sql   [新建] 版本化迁移（存量库）
  migrations/manifest.yaml                          [改] 新条目 + 整体校验和
  kb/schemas/017_kb_artifacts.sql                   [改] batches 通用化折入（fresh install 路径）
  kb/schemas/018_wiki_artifacts.sql                 [新建] 四张新表 + HNSW（fresh install 路径）
knowledge_mining/
  mining/infra/pg_schema.py                         [改] ★启动链登记：_WIKI_ARTIFACTS_DDL 常量+domain_schema_paths() 追加（硬编码元组，不登记=新装库永远没有这四张表）
  mining/maintenance/database_upgrade/contract.py   [改] CURRENT_SCHEMA_CHECKSUM / FORMAL_TABLES 60→64 / :142-143 计数守卫 / PREVIOUS_RELEASE_TABLES 减法集
  mining/maintenance/database_upgrade/validation.py [改] REQUIRED_INDEXES += wiki HNSW
  mining/kb/routes/artifacts.py                     [改] ArtifactCreate +kind；create 透传
  tests/maintenance/test_database_upgrade_contract.py [改] :64-65 硬编码 60/61→64/65
  tests/maintenance/test_migration_manifest.py      [改] 010 专属守卫测试（镜像 009 的 targets_formal_table_with_guards）
  tests/kb/test_artifacts_routes.py                 [改] kind 用例（env fixture 口径）
db_tables.py                                        [改] ★EXPORT_TABLES += 四张（bootstrap census 的真源在仓库根这个文件，不在 contract.py）
kb-ui/src/
  api/artifacts.ts                                  [改] ArtifactCreateRequest +kind
  views/artifacts/ArtifactsListView.vue             [改] 创建对话框类型选择 + 分类型提示
  views/artifacts/ArtifactDetailView.vue            [改] wiki 设计态/运行态占位守卫
agent_serving_java/src/main/java/.../observability/
  ServingRuntimeSchemaInitializer.java              [改] EXPECTED_SCHEMA_CHECKSUM 一个常量（无 EXPECTED_FORMAL_TABLES 常量，别找）
```

**测试命令速查**（docker，git-bash；`<WT>` = worktree 绝对路径）：

```bash
PY='docker run --rm -v "<WT>:/w" -w /w agentickb-release-validation:a74b940 sh -c "pip install -q pytest pytest-asyncio && python -m pytest"'
$PY knowledge_mining/tests/maintenance -q            # 迁移/契约/镜像门禁
$PY knowledge_mining/tests/kb -q                     # 制品路由/服务
$PY knowledge_mining/tests/kb/test_artifacts_routes.py -q   # 单文件
MVN='docker run --rm -v "<WT>:/w" -w /w -v "C:\Users\fuzhi\.m2:/root/.m2" maven:3.9-eclipse-temurin-21 mvn -q'
$MVN -f agent_serving_java/pom.xml compile           # Java 常量改动编译验证
cd kb-ui && npm test && npm run build                # 前端单测 + 类型真验证
```

---

## Chunk 1: 数据库底座

### Task 0: 执行前导航（只读，不产码）

- [ ] **Step 1: 通读四个真相源**——`databases/migrations/core/20261019_009_retrieval_v2_hnsw_index.sql`（HNSW 守卫全文，本计划守卫段照它口径）、`databases/README.md`、`databases/kb/schemas/017_kb_artifacts.sql`、`knowledge_mining/mining/infra/pg_schema.py:150-280`（启动链元组与 `_KB_ARTIFACTS_DDL` 登记模式）。
- [ ] **Step 2: 确认启动链里 vector 扩展可用性**——查 kb 启动链装载顺序中 018 之前是否有 `CREATE EXTENSION vector`（asset_core 或装载器内）；不能保证则在 018 顶部加 `CREATE EXTENSION IF NOT EXISTS vector;`。结论写进 commit message。

### Task 1: 迁移 010 + 启动链 017/018 + pg_schema 登记

**Files:**
- Create: `databases/migrations/core/20261021_010_wiki_artifacts.sql`
- Create: `databases/kb/schemas/018_wiki_artifacts.sql`
- Modify: `databases/kb/schemas/017_kb_artifacts.sql`
- Modify: `knowledge_mining/mining/infra/pg_schema.py`

- [ ] **Step 1: 写迁移 SQL**（完整内容；018 同文去掉迁移专属注释；batches 通用化不进 018、折进 017）

```sql
-- 63号批次1：wiki 制品通用底座（online_expand，与启动链 017 改/018 同文）。
-- ①batches 通用化：channel 补 wiki_compile/sql_import（sql_import 为表格类 P2 预留位，
--   枚举现在建全，后续加通道只加代码不改表）；details jsonb 装通道专属细节，
--   file_name/sheet_name 保留（存量 upload 批次兼容）。
-- ②kb_artifact_sources：制品来源绑定（kind 无关——表格 kb_import 与 wiki 编译共用）。
-- ③kb_wiki_pages / kb_wiki_page_links / kb_wiki_page_units：wiki 专有三表（63号 §2.4）。
-- ④kb_wiki_page_units HNSW：009 同款守卫（pgvector>=0.8 + 列形态 fail-closed +
--   同名无效索引自愈 + 串行建索引——64MB /dev/shm 坑）。

-- ---------- ① batches 通用化 ----------
ALTER TABLE kb_artifact_batches
    ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'ck_kb_artifact_batches_channel') THEN
        EXECUTE 'ALTER TABLE kb_artifact_batches'
             || ' DROP CONSTRAINT ck_kb_artifact_batches_channel';
    END IF;
    EXECUTE 'ALTER TABLE kb_artifact_batches ADD CONSTRAINT'
         || ' ck_kb_artifact_batches_channel CHECK (channel IN'
         || ' (''upload'',''kb_import'',''wiki_compile'',''sql_import''))';
END $$;

-- ---------- ② 来源绑定（通用） ----------
CREATE TABLE IF NOT EXISTS kb_artifact_sources (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    kb_id       TEXT NOT NULL,
    scope       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"directories":[...]}
    fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 编译/导入时快照指纹（脏标记）
    status      TEXT NOT NULL DEFAULT 'active',
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_kb_artifact_sources UNIQUE (artifact_id, kb_id)
);
-- 附带索引（63号 §2.2 之外的合理补充：绑定列表按制品查）
CREATE INDEX IF NOT EXISTS idx_kb_artifact_sources_artifact
    ON kb_artifact_sources(artifact_id);

-- ---------- ③ wiki 专有三表 ----------
CREATE TABLE IF NOT EXISTS kb_wiki_pages (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,             -- 0=draft；N=发布快照（与 kb_table_rows 同义）
    page_key    TEXT NOT NULL,                -- 页面身份=标题归一；[[]] 按 page_key 或 alias 解析
    title       TEXT NOT NULL,
    page_type   TEXT NOT NULL DEFAULT 'topic'
                CONSTRAINT ck_wiki_pages_type
                CHECK (page_type IN ('topic','glossary','contradiction','open_question')),
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb, -- 标准键 type/aliases/tags/created/updated+任意
    body_md     TEXT NOT NULL DEFAULT '',     -- 整页 md 原文（存储永不切片）
    origin      TEXT NOT NULL DEFAULT 'machine'
                CONSTRAINT ck_wiki_pages_origin CHECK (origin IN ('machine','manual')),
    provenance  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{kb_id,snapshot_id,unit_ids:[]}]
    flags       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 质量标记（密度告警/draft 宽免）
    ordinal     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artifact_id, revision, page_key)
);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_type
    ON kb_wiki_pages(artifact_id, revision, page_type);

CREATE TABLE IF NOT EXISTS kb_wiki_page_links (
    artifact_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    from_page   TEXT NOT NULL,
    to_page     TEXT NOT NULL,                -- 目标页不存在 → status='broken'（断链账）
    anchor_text TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'valid'
                CONSTRAINT ck_wiki_links_status CHECK (status IN ('valid','broken')),
    PRIMARY KEY (artifact_id, revision, from_page, to_page, anchor_text)
);
-- 反向链接=同表反向查询（WHERE to_page=?），不建第二张表。

CREATE TABLE IF NOT EXISTS kb_wiki_page_units (
    artifact_id  TEXT NOT NULL,
    revision     INTEGER NOT NULL,            -- 只存发布版（>=1）；rev0 不产生检索单元
    unit_key     TEXT NOT NULL,               -- page_key 或分块 page_key#s2
    page_key     TEXT NOT NULL,
    content_text TEXT NOT NULL,               -- 分块文本，前置页标题上下文
    embedding    VECTOR(1024),                -- 发布后异步生成；NULL=未就绪不进 HNSW
    embed_status TEXT NOT NULL DEFAULT 'pending'
                 CONSTRAINT ck_wiki_units_embed
                 CHECK (embed_status IN ('pending','done','failed')),
    PRIMARY KEY (artifact_id, revision, unit_key)
);

-- ---------- ④ HNSW（守卫口径照 009：列查 attrelid+attname 两段式，禁用 '表.列'::regclass） ----------
DO $$
DECLARE
    extver  text;
    coltype text;
    invalid integer;
BEGIN
    SELECT extversion INTO extver FROM pg_extension WHERE extname = 'vector';
    IF extver IS NULL OR string_to_array(extver, '.')::int[] < ARRAY[0,8,0] THEN
        RAISE EXCEPTION 'wiki_units_hnsw_requires_pgvector_0_8: got %', extver;
    END IF;
    SELECT format_type(a.atttypid, a.atttypmod) INTO coltype
    FROM pg_attribute a
    WHERE a.attrelid = 'kb_wiki_page_units'::regclass
      AND a.attname = 'embedding' AND NOT a.attisdropped;
    IF coltype IS DISTINCT FROM 'vector(1024)' THEN
        RAISE EXCEPTION 'wiki_units_embedding_column_invalid: %', coltype;
    END IF;
    -- 同名无效索引（或非 hnsw）自愈：DROP 后由下方 CREATE IF NOT EXISTS 重建
    SELECT count(*) INTO invalid
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_am am ON am.oid = c.relam
    WHERE c.relname = 'idx_wiki_units_hnsw'
      AND (NOT i.indisvalid OR am.amname <> 'hnsw');
    IF invalid > 0 THEN
        EXECUTE 'DROP INDEX idx_wiki_units_hnsw';
    END IF;
END $$;

-- 串行建索引：容器 /dev/shm 默认 64MB，并行建按 maintenance_work_mem 申请 DSM 会 ENOSPC
SET max_parallel_maintenance_workers = 0;
SET maintenance_work_mem = '256MB';
CREATE INDEX IF NOT EXISTS idx_wiki_units_hnsw
    ON kb_wiki_page_units USING hnsw (embedding vector_cosine_ops);
RESET max_parallel_maintenance_workers;
RESET maintenance_work_mem;
```

- [ ] **Step 2: 写启动链 018**——`databases/kb/schemas/018_wiki_artifacts.sql`：上面 ②③④ 全文（①不进 018）。按 Task 0 Step 2 结论决定顶部是否加 `CREATE EXTENSION IF NOT EXISTS vector;`。
- [ ] **Step 3: 改 017**——`kb_artifact_batches` 的 CREATE TABLE：CHECK 改四通道、`details JSONB NOT NULL DEFAULT '{}'::jsonb` 加在 row_count 之后。017 终态与迁移 ① 段必须一致（同文铁律）。
- [ ] **Step 4: pg_schema.py 登记（★不登记=新装库没有这四张表，census 测试会红）**——照 `_KB_ARTIFACTS_DDL`（约 :155-161）既有模式新增 `_WIKI_ARTIFACTS_DDL` 常量指向 018 文件，并在 `domain_schema_paths()`（约 :274）按 017 的追加方式接入。行号以实际文件为准。
- [ ] **Step 5: manifest 占位条目**——`databases/migrations/manifest.yaml` 末尾追加（checksum 先占位 64 个 0，Task 2 刷真值）：

```yaml
  - id: core/20261021_010_wiki_artifacts
    path: core/20261021_010_wiki_artifacts.sql
    mode: online_expand
    checksum: 0000000000000000000000000000000000000000000000000000000000000000
```

- [ ] **Step 6: 单文件校验和**——口径 = manifest.py `_file_checksum`（**CRLF 归一后 sha256**）：

```bash
docker run --rm -v "<WT>:/w" -w /w agentickb-release-validation:a74b940 \
  sh -c "python -c \"import hashlib; data = open('databases/migrations/core/20261021_010_wiki_artifacts.sql','rb').read().decode('utf-8').replace(chr(13)+chr(10), chr(10)).encode('utf-8'); print(hashlib.sha256(data).hexdigest())\""
```

（以 `knowledge_mining/mining/maintenance/database_upgrade/manifest.py:84-88` 的 `_file_checksum` 实现为唯一口径；**最终以 manifest 加载测试绿为准**。）

- [ ] **Step 7: Commit**

```bash
git add databases/migrations/core/20261021_010_wiki_artifacts.sql \
        databases/migrations/manifest.yaml \
        databases/kb/schemas/017_kb_artifacts.sql \
        databases/kb/schemas/018_wiki_artifacts.sql \
        knowledge_mining/mining/infra/pg_schema.py
git commit -m "feat(kb): 迁移010+启动链018+pg_schema登记——wiki制品底座四表+batches通用化+检索单元HNSW（63号批次1）"
```

### Task 2: 十处登记 + 三镜像刷新（红灯导航法）

**Files:**
- Modify: `knowledge_mining/mining/maintenance/database_upgrade/contract.py`
- Modify: `knowledge_mining/mining/maintenance/database_upgrade/validation.py`
- Modify: `knowledge_mining/tests/maintenance/test_database_upgrade_contract.py`
- Modify: `knowledge_mining/tests/maintenance/test_migration_manifest.py`
- Modify: `db_tables.py`（仓库根——EXPORT_TABLES 真源）
- Modify: `agent_serving_java/src/main/java/com/coremasterkb/serving/observability/ServingRuntimeSchemaInitializer.java`

- [ ] **Step 1: 先跑红**——`$PY knowledge_mining/tests/maintenance -q`。预期红：manifest 整体校验和不匹配 + census 缺四张表。红灯信息是登记导航，但**它抓不住 PREVIOUS_RELEASE_TABLES**（Step 3 专门处理，无测试兜底）。
- [ ] **Step 2: contract.py 计数与清单**——①`FORMAL_TABLES` 加四张（**现值 60，改后 64**——以红灯实际报数复核）；②`PREVIOUS_RELEASE_TABLES` 减法集：**若该集合按"上一版（1.1.15）已有表"口径维护，四张 wiki 新表要加进"本版新增"部分**（不加则一跳升级校验 `validate_supported_rebase_source` 会拒认 1.1.15 源库——61号 a59b263 同款坑）；③计数守卫 `!= 60` → `!= 64`（约 :142-143，008 改过位置，以实际为准）。
- [ ] **Step 3: db_tables.py**——`EXPORT_TABLES` 加四张（census 测试 `test_bootstrap_table_census` 经 `db_tables.py` 取值，test_bootstrap_table_census.py:61 可证）。
- [ ] **Step 4: 测试硬编码**——`test_database_upgrade_contract.py:64-65`：`EXPECTED_FORMAL_TABLES == 60`→`== 64`、`EXPECTED_PHYSICAL_TABLES == 61`→`== 65`。
- [ ] **Step 5: 整体校验和三镜像**——

```bash
docker run --rm -v "<WT>:/w" -w /w agentickb-release-validation:a74b940 \
  sh -c "pip install -q pytest pytest-asyncio && python -c \"from knowledge_mining.mining.maintenance.database_upgrade.manifest import manifest_checksum; print(manifest_checksum('databases/migrations/manifest.yaml'))\""
```

输出同值回填：①contract.py `CURRENT_SCHEMA_CHECKSUM`；②`ServingRuntimeSchemaInitializer.java:38` `EXPECTED_SCHEMA_CHECKSUM`（**该文件只有这一个常量要动**）。
- [ ] **Step 6: validation.py 门禁**——`REQUIRED_INDEXES` 加 `idx_wiki_units_hnsw`（tokens 含 `hnsw`/`vector_cosine_ops`，口径照 009 那条抄，含 indisvalid 复查）。
- [ ] **Step 7: 010 专属守卫测试**——`test_migration_manifest.py` 镜像 009 的 `test_hnsw_migration_targets_formal_table_with_guards` 写 010 版：断言 010 内容含 pgvector 守卫、`kb_wiki_page_units`、串行建索引三要素、且目标表在 FORMAL_TABLES。
- [ ] **Step 8: Java 编译**——`$MVN -f agent_serving_java/pom.xml compile`，BUILD SUCCESS。
- [ ] **Step 9: 维护测试全绿**——`$PY knowledge_mining/tests/maintenance -q`，0 failed。
- [ ] **Step 10: Commit**

```bash
git add knowledge_mining/mining/maintenance/database_upgrade/contract.py \
        knowledge_mining/mining/maintenance/database_upgrade/validation.py \
        knowledge_mining/tests/maintenance/test_database_upgrade_contract.py \
        knowledge_mining/tests/maintenance/test_migration_manifest.py \
        db_tables.py \
        agent_serving_java/src/main/java/com/coremasterkb/serving/observability/ServingRuntimeSchemaInitializer.java
git commit -m "feat(kb): wiki底座十处登记与三镜像校验和刷新（FORMAL_TABLES 60→64+PREVIOUS_RELEASE_TABLES+HNSW门禁+010守卫测试）"
```

---

## Chunk 2: kind 创建链路放开

### Task 3: 路由放开 kind（TDD）

**Files:**
- Modify: `knowledge_mining/mining/kb/routes/artifacts.py`（ArtifactCreate 模型约 :69-73、create 路由 :216-245）
- Test: `knowledge_mining/tests/kb/test_artifacts_routes.py`

- [ ] **Step 1: 写失败测试**（该文件口径：**`env` fixture 取 `env.client`；URL 前缀 `/api/artifacts`（无 /v1）；域用模块常量 `DOMAIN`**——照文件既有用例逐字对齐）：

```python
async def test_create_artifact_wiki_kind(env):
    resp = await env.client.post("/api/artifacts", json={
        "title": "云核命令Wiki", "domain_id": DOMAIN, "kind": "wiki",
    })
    assert resp.status_code == 201
    assert resp.json()["kind"] == "wiki"

async def test_create_artifact_invalid_kind_rejected(env):
    resp = await env.client.post("/api/artifacts", json={
        "title": "x", "domain_id": DOMAIN, "kind": "graph",
    })
    assert resp.status_code == 422  # pydantic Literal 层拦截

async def test_create_artifact_default_kind_is_table(env):
    resp = await env.client.post("/api/artifacts", json={
        "title": "y", "domain_id": DOMAIN,
    })
    assert resp.status_code == 201
    assert resp.json()["kind"] == "table"
```

- [ ] **Step 2: 跑红**——`$PY knowledge_mining/tests/kb/test_artifacts_routes.py -q`，预期新用例红（kind 被模型忽略，`kind == "wiki"` 断言失败返回 table）。
- [ ] **Step 3: 实现**——ArtifactCreate 加 `kind: Literal["table", "wiki"] = "table"`；create 路由 `kind="table"` 改 `kind=body.kind`。service 层 `invalid_kind` 拦截（artifact_service.py:269）不动。
- [ ] **Step 4: 跑绿**——同 Step 2，全绿（既有用例不传 kind 走默认 table，不破坏）。
- [ ] **Step 5: Commit**

```bash
git add knowledge_mining/mining/kb/routes/artifacts.py knowledge_mining/tests/kb/test_artifacts_routes.py
git commit -m "feat(kb): 制品创建路由放开kind参数——Literal校验+透传（service/db 61号已支持）"
```

### Task 4: 前端类型链 + wiki 占位守卫

**Files:**
- Modify: `kb-ui/src/api/artifacts.ts:26-31`（ArtifactCreateRequest）
- Modify: `kb-ui/src/views/artifacts/ArtifactsListView.vue`（form :104 / openCreate :117 / createArtifact :125 / 对话框 :52-82）
- Modify: `kb-ui/src/views/artifacts/ArtifactDetailView.vue`（wiki 占位守卫）
- Test: `kb-ui/src/views/artifacts/__tests__/ArtifactsListView.spec.ts`

- [ ] **Step 1: api 类型**——`ArtifactCreateRequest` 加 `kind?: ArtifactKind`（types import）；create 函数透传。
- [ ] **Step 2: ListView 对话框**——
  - `form` 加 `kind: 'table'`（openCreate 重置同款）；
  - 标题项上方加类型项：`el-radio-group v-model="form.kind"` + `el-radio label="table"` 表格制品 / `el-radio label="wiki"` Wiki 制品，`data-test="create-artifact-kind"`；
  - 底部提示按 kind 分流：table 保持现文案；wiki 改「创建后进入设计态：绑定知识库目录 → 编译生成页面 → 人工修订 → 填使用说明 → 发布」；
  - `createArtifact` 请求体加 `kind: form.value.kind`。
- [ ] **Step 3: DetailView 占位守卫**——`artifact.kind === 'wiki'` 时：设计态三面板区与运行态行表格区各替换为一个 `EmptyState`（文案「Wiki 编译与编辑能力随下个批次开放；当前可管理元信息、成员与版本」）；members/versions tab 与生命周期按钮不动（泛用）。`v-if` 分支实现，不动既有 table 分支内部。
- [ ] **Step 4: 单测更新**——ArtifactsListView.spec.ts：现有创建用例补 `kind: 'table'` 默认断言；新增「选 Wiki → create 请求体含 `kind:'wiki'`」（照该文件既有 mock 风格，:104 附近有可扩展的创建用例）。
- [ ] **Step 5: 前端门禁**——`cd kb-ui && npm test`（vitest 全绿）→ `npm run build`（类型真验证，预期 0 ts 错误）。
- [ ] **Step 6: Commit**

```bash
git add kb-ui/src/api/artifacts.ts kb-ui/src/views/artifacts/ArtifactsListView.vue \
        kb-ui/src/views/artifacts/ArtifactDetailView.vue \
        kb-ui/src/views/artifacts/__tests__/ArtifactsListView.spec.ts
git commit -m "feat(ui): 制品创建对话框类型选择（表格/Wiki）+详情页wiki占位守卫"
```

---

## 收尾门禁（全过后才算批次1完成）

- [ ] `$PY knowledge_mining/tests -q`（全量 Python；预期与 1.1.15 基线同绿集）
- [ ] `$PY mcp_server/tests -q`（MCP 无改动回归）
- [ ] `$MVN -f agent_serving_java/pom.xml test`（surefire 只跑单元集，pg-integration IT 不在其中，无需排除参数）
- [ ] `cd kb-ui && npm test && npm run build`
- [ ] `git show --stat` 核对：本批次 4 个功能提交 + 1 个文档提交（设计稿+计划），文件清单与 File Structure 一致，无 -A 卷入
- [ ] 汇报：master 不动；分支 `worktree-wiki-artifact-mvp` 状态；批次2（wiki 生命周期与页面服务域）计划另出

## 批次路线图（本计划只覆盖批次1）

| 批次 | 内容 | 产出 |
|---|---|---|
| **1（本计划）** | 底座四表 + batches 通用化 + HNSW + kind 链路 | 迁移 010/018、可创建 wiki 制品（空壳） |
| 2 | wiki 生命周期服务域：db_wiki（pages/links CRUD、发布事务扩展、单元投影+向量异步）、REST（page/search/links/diff）、前端编辑器/阅读器 | wiki 制品端到端可发布可读 |
| 3 | 编译管线：sources 绑定管理+脏标记、聚类→目录→写作→链接质量（LLM 异步）、origin 保护、编译向导 | 从库语料一键产 wiki |
| 4 | Java ArtifactWikiSearchService（dense/keyword/fusion 同构）+ internal 端点 + MCP art_ kind 分流 + jar 纯源码构建 | Agent 检索/读取 wiki |
| 5 | E2E 验收：三通道检索、引用规范校验、重跑稳定性、origin 保护回归 | 63号 §七 验收全过 |

---

## 批次1 完成记录（2026-10-21，终审定稿）

**提交链**（worktree-wiki-artifact-mvp，基于 24c43d4）：a77d1e8 docs → 8cbafe9 迁移010+启动链018+pg_schema → f40d41d 十处登记+三镜像（校验和 `a40aa97d…`）→ 2e7d988 路由kind → d967804 前端。门禁全过：Python 全量 2037 passed 与 merge-base 基线 test-ID 级零新增失败；MCP 165；Java 484；前端 558+build 0 错。审查链：每任务规格+质量两审，4 轮修复迭代（守卫口径对齐009/索引换kb侧/列序对齐/守卫测试泛化+四补/422定位断言/viewer文案中立+DetailView测试）。

**与计划的偏差（均已审）**：来源绑定索引 `(artifact_id)`→`(kb_id)`（UNIQUE 已覆盖前者）；maintenance_work_mem 512MB（对齐009）；radio 用 `value=`（EP 2.14 约定）。

### 批次2-5 接手注意事项（终审移交）

1. **硬删级联缺口（批次2必做）**：`db_artifacts.py:200-226` delete_artifact 显式级联清单不含新四表——不补则 wiki 制品硬删留孤儿（含 HNSW unit 行）。KB 清退/重挖时按 `kb_id` 清 `kb_artifact_sources`（`idx_kb_artifact_sources_kb` 为此而建，批次3接线）。
2. **发布事务归一（批次2）**：publish 链路 kind 盲——wiki 空壳填了 usage_note 可发布成 row_count=0 空版本（不产脏数据，窗口期可接受）。批次2 要么先按 kind 拦，要么直接接管统一发布事务（pages/links 快照+units 投影+向量异步）；容忍可能已存在的空 revision。
3. **JSONB 契约未定**：batches.details（wiki_compile 载荷）、sources.scope/fingerprint（脏标记结构）形状归批次2/3 定义；`sql_import` 枚举已预登，表格 P2 加通道不得再动 CHECK。
4. **占位守卫拆除点（批次2）**：`ArtifactDetailView.vue` 两处 EmptyState（:80-84/:131-135）+ watch 守卫（:365）+ spec 断言文案；`KIND_CREATE_HINTS` 是 Record，新 kind 时 TS 强制补文案。
5. **消费侧空读（批次4）**：MCP art_ 与 Java serving 均 kind 盲——wiki 制品经 Agent 读得空表不报错；wiki 向量查询沿用 009 结案经验（ANN 改写+force_custom_plan），量起来后 872c445 预热情闸门口径同样适用 idx_wiki_units_hnsw。
6. **发版提醒（1.1.16）**：Java 常量变了，jar 必须随包重build（纯源码构建+zipfile 校验铁律）；010 的 pgvector≥0.8 门禁与 009 同条件，1.1.15 存量库已满足，无新增 deploy 前置。
