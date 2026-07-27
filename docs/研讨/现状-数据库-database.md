# 现状：数据库（database）

> CoreMasterKB 数据库契约摸底。所有论断基于源码逐行核对（DDL + 读写代码），不依赖项目文档（多数已过时）。论断后标 `(file:line)`。
>
**一句话现状**：三库逻辑分库（`asset_core` / `mining_runtime` / `agent_llm_runtime`）+ 一组跨库的 `ontology` 表 + serving 自有的三张运行时表。挖掘(mining)是 `asset_*` 的唯一写方，检索(serving)对 `asset_*` 全只读——两条主线只通过 PostgreSQL 交接，从不互调 HTTP。当前 registry 把 4 个域都指向同一个物理库 `coremasterkb`，靠每张表的 `domain` 列做逻辑隔离。

---

## 1. 库与表清单（含读写方）

| 逻辑库 | 表 | 写方 | 读方 | DDL 来源 |
|---|---|---|---|---|
| **asset_core** | `asset_source_batches` | mining | mining/serving | `databases/asset_core/schemas/002_asset_core_postgresql.sql:12` |
| | `asset_documents` | mining | mining/serving | `002_*.sql:35` |
| | `asset_document_snapshots` | mining | mining/serving | `002_*.sql:56` |
| | `asset_document_snapshot_links` | mining | mining/serving | `002_*.sql:81` |
| | `asset_raw_segments` | mining | mining/serving | `002_*.sql:104` |
| | `asset_raw_segment_relations` | mining | mining/serving | `002_*.sql:144` |
| | `asset_retrieval_units` | mining | mining/serving | `002_*.sql:175` |
| | `asset_retrieval_embeddings` | mining | serving | `002_*.sql:241` |
| | `asset_builds` | mining | mining/serving | `002_*.sql:262` |
| | `asset_build_document_snapshots` | mining | mining/serving | `002_*.sql:286` |
| | `asset_publish_releases` | mining | mining/serving | `002_*.sql:306` |
| **mining_runtime** | `mining_runs` | mining | mining | `databases/mining_runtime/schemas/002_mining_runtime_postgresql.sql:7` |
| | `mining_run_documents` | mining | mining | `002_*.sql:45` |
| | `mining_run_stage_events` | mining | mining | `002_*.sql:70` |
| **agent_llm_runtime** | `agent_llm_prompt_templates` | 启动注册/POST | llm_service/mining | `databases/agent_llm_runtime/schemas/002_*.sql:4` |
| | `agent_llm_tasks` | llm_service | llm_service | `002_*.sql:33` |
| | `agent_llm_requests` | llm_service | llm_service | `002_*.sql:81` |
| | `agent_llm_attempts` | llm_service | llm_service | `002_*.sql:99` |
| | `agent_llm_results` | llm_service | llm_service | `002_*.sql:124` |
| | `agent_llm_events` | llm_service | llm_service | `002_*.sql:142` |
| | `agent_llm_model_calls` | llm_service | llm_service | `002_*.sql:157` |
| **ontology**（跨库 FK）| `ontology_versions` | mining | mining/serving | `databases/ontology/schemas/001_ontology_concept_postgresql.sql:13` |
| | `ontology_node_types` / `ontology_relation_types` | mining | mining/serving | `001_*.sql:33 / 45` |
| | `ontology_entities` / `ontology_entity_relations` | mining | mining/serving | `001_*.sql:62 / 80` |
| | `ontology_alias_dictionary` | mining | mining/serving | `001_*.sql:102` |
| | `ontology_evidence_nodes` | mining | mining/serving | `001_*.sql:117`（FK→asset_document_snapshots/asset_raw_segments）|
| | `asset_segment_entity_mentions` | mining | mining/serving | `001_*.sql:138`（FK→asset_core 三表 + ontology_entities）|
| | `ontology_candidates` | mining | mining/serving | `001_*.sql:166` |
| **serving 自有**（写方=serving）| `serving_query_logs` | serving | serving | `agent_serving_java/src/main/resources/db/serving/001_serving_query_logs.sql:13` |
| | `serving_query_cache` | serving | serving | `db/serving/002_serving_query_cache.sql:18` |
| | `operator_paradigm` / `operator_paradigm_version` | serving | serving | `db/operator/001_operator_paradigm.sql:10/22`（全局共享库，**非按域路由**）|

**关键不变量**：
- mining 是 `asset_*` 唯一写方；serving 的 8 个 mapper 全是 `<select>`。
- serving 自己只写 `serving_query_logs` / `serving_query_cache` / `operator_paradigm*`。
- `asset_*` 与 `ontology_*` 表都在身份层用 `(domain, *)` 做唯一键，域隔离在数据层强制。
- ontology 的两张 provenance 表（`ontology_evidence_nodes`、`asset_segment_entity_mentions`）FK 跨到 `asset_core`——这就是 CLAUDE.md 所说「ontology DDL 必须最后建、FK 指向 asset_*、物理合库不可逆」的原因。

---

## 2. asset_core 主链（挖掘线骨架）

整条链是「身份层 → 内容层 → 链接层 → 切片层 → 检索层 → 发布层」六层。这是整个系统的脊梁，**也是「知识库管理」新模块必须理解的交接点**。

```
[1 身份]  asset_documents
            id (PK)
            domain + document_key   (UNIQUE)         ← 身份寻址
            document_type (CHECK enum)
            metadata_json (JSONB)

[2 内容]  asset_document_snapshots
            id (PK)
            domain + normalized_content_hash (UNIQUE) ← 内容寻址（同内容复用）
            raw_content_hash / mime_type
            scope_json / tags_json / ...

[3 链接]  asset_document_snapshot_links
            id (PK)
            document_id ──→ asset_documents.id
            document_snapshot_id ──→ asset_document_snapshots.id
            source_batch_id ──→ asset_source_batches.id (SET NULL)
            relative_path / source_uri / linked_at    ← 每次摄取新插一行（事件流）

[4 切片]  asset_raw_segments (FK→snapshot)
            segment_key / segment_index / block_type / semantic_role
            raw_text / normalized_text / entity_refs_json
          asset_raw_segment_relations
            source_segment_id ↔ target_segment_id + relation_type (24 种 RST)

[5 检索]  asset_retrieval_units (FK→snapshot)
            unit_key / unit_type / target_type
            text / search_text / search_vector
            entity_refs_json / source_segment_id / facets_json / weight
          asset_retrieval_embeddings
            embedding_vector_vec vector(1024)  ← mapper 实际用的真向量列
            HNSW idx + trigger 从 embedding_vector(JSON) 自动转换

[6 发布]  asset_builds ── asset_build_document_snapshots ── asset_publish_releases
            id / build_code UNIQUE              (build_id, document_id) PK         id / build_id
            status / build_mode / domain         document_snapshot_id (RESTRICT)  domain + channel
            source_batch_id / parent_build_id    selection_status (active/removed) status (staging/active/
            mining_run_id                        reason (add/update/retain/remove)         retired/failed)
                                                                                 previous_release_id
                                                                                 activated_at / deactivated_at
```

### 主链字段级链路

```
asset_source_batches.id
  └→ mining_runs.source_batch_id              (mining 创建 run 时写入，jobs/run.py:822)
       └→ mining_runs.id ─→ mining_run_documents.run_id (CASCADE)
            └→ mining_run_documents.document_id / document_snapshot_id  (挖掘完成回填，NULLABLE)
  └→ asset_document_snapshot_links.source_batch_id    (provenance，每次摄取记一次)
  └→ asset_builds.source_batch_id / mining_run_id / parent_build_id
       └→ asset_build_document_snapshots.(build_id, document_id) 复合 PK
            └→ asset_publish_releases.build_id ─→ asset_builds.id (RESTRICT，已发布的 build 不许删)
```

### 核心不变量（发布语义）

- `asset_publish_releases` 上的部分唯一索引 `uq_asset_publish_releases_domain_channel_active`（`002_*.sql:418-421` + `003_*.sql:432-436`）保证「一域至多一个 active release」——这是 `no_active_release` / `multiple_active_releases` 报错的来源。
- 挖完的内容**不会立刻可检索**：必须 build 成不可变快照并 publish 成 release，serving 只认 active release。
- 三层文档模型：document（身份）/ snapshot（内容寻址）/ link（事件流）解耦——撤回可逆、内容去重、历史可追溯。

---

## 3. 文档生命周期状态机（研讨「知识库管理」的关键）

### 3.1 当前**没有「知识库(KB)」概念**，文档直接挂 domain

数据模型层面没有 KB 实体。文档归属关系：

- **直接归属**：`asset_documents.domain`（NOT NULL）+ `(domain, document_key)` 复合唯一键。
- **间接归属**：`asset_document_snapshot_links.source_batch_id` → `asset_source_batches.domain`。
- **域即事实上的 KB**：所有 `/api/knowledge/*` 与 `/api/runs` 都强制 `domain=Query(...)`（`require_domain` in `mining/api/domain_scope.py:6`）。
- **域的定义**在 `main_control_service/config/domain_registry.yaml`（4 个域），**没有任何数据库表存域元数据**。域是「散布在 YAML + 每张表的 domain 列」的隐式概念。

> 🔧 **新模块交接点**：当前 1 个 domain = 1 个隐式 KB。若要支持「一域多 KB / KB 分组」，需在 `asset_documents` 加 `kb_id` 列，或新建 `knowledge_bases` 表把 domain 升格成实体。

### 3.2 状态分散在 4 张表，没有单一 `documents.status`

一个文档「能不能被 serving 检索」= `asset_publish_releases.status='active'` **且** 该文档在对应 build 的 `selection_status='active'`。

```
【阶段 0 上传】POST /api/uploads (uploads.py:65)
  → 文件落盘 {upload_root}/{domain}/{upload_batch_id}/，DB 无写入
  → upload_batch_id 是磁盘目录名，不等于 asset_source_batches.id
  → 上传与挖掘完全解耦，可上传后不立刻挖

【阶段 1 登记】mining run 启动 (jobs/run.py:813)
  asset_source_batches INSERT；mining_runs.source_batch_id 反填
  每文件 mining_run_documents INSERT，action ∈ {NEW,UPDATE,SKIP,RESTORE}，status='pending'
  action 决策：decide_document_lifecycle_action() (jobs/run.py:26)

【阶段 2 处理】streaming 管线  parse→segment→enrich→entity_extract→resolve→discourse→retrieval_units→embedding→db_write
  mining_run_documents.status: pending → processing → committed/failed/skipped
  mining_run_stage_events 一路写
  select_or_create_snapshot (snapshot/__init__.py:17):
     asset_documents UPSERT (ON CONFLICT domain+document_key)
     asset_document_snapshots UPSERT (ON CONFLICT domain+normalized_content_hash DO NOTHING)  ← 内容去重
     asset_document_snapshot_links INSERT（每次摄取新一行）
     raw_segments / relations / retrieval_units / embeddings 写入

【阶段 3 构建】publishing.py:assemble_build
  asset_builds INSERT (status='building', build_mode∈{full,incremental})
  asset_build_document_snapshots UPSERT (selection_status∈{active,removed}, reason∈{add,update,retain,remove})
  status: building → 'validated'

【阶段 4 发布】publishing.py:publish_release
  pg_advisory_xact_lock('asset-publish:{domain}') 串行化 (db.py:243)
  旧 active release → 'retired'；新 release → 'active'
  mining_runs.build_id 反填；status → 'completed'；current_stage → 'done'

【阶段 5 可检索】serving 只读 active release 选中的 (document_id, snapshot_id) 集合
```

| 阶段 | 表 | 字段 | 变化 |
|---|---|---|---|
| 上传 | （无表）| — | 仅文件系统 |
| 登记 | `asset_source_batches` | 整行 | `source_type` enum |
| 登记 | `mining_runs` | source_batch_id/status/current_stage | queued→running；queued→ingest |
| 登记 | `mining_run_documents` | action/status | NEW/UPDATE/SKIP/RESTORE；pending |
| 处理 | `mining_run_documents` | status | pending→processing→committed/failed/skipped |
| 处理 | `mining_run_stage_events` | stage/status | stage enum 23 种；started→completed/failed |
| 构建 | `asset_builds` | status | building→validated→published→archived |
| 构建 | `asset_build_document_snapshots` | selection_status/reason | active/removed × add/update/retain/remove |
| 发布 | `asset_publish_releases` | status | staging→active→retired（旧）/ failed |

### 3.3 软撤回（withdrawal）

**没有「删除」，只有「软撤回」**。撤回 = 克隆 active build → 把目标文档标 `removed` → 发布新 release 替换旧 release。原始数据全部保留。

实现：`stages/withdrawal.py:60` `_withdraw()`：
1. `pg_advisory_xact_lock('asset-publish:{domain}')` 串行化（与正常 publish 共用锁）。
2. 读当前 active build + 其 `asset_build_document_snapshots`。
3. `target_ids = requested_ids ∩ active_document_ids`，空则 404。
4. 克隆 build：新 build，`build_mode='incremental'`, `parent_build_id=旧build.id`, `summary_json.operation='withdrawal'`。
5. 目标文档 `selection_status='removed'`/`reason='remove'`，其余原样（`withdrawal.py:122-134`）。
6. `validate_build(allow_empty=True)`——**允许空 build**（撤空仍要可区分于无 active release）。
7. `publish_release()` 切换 active。

API：`DELETE /api/knowledge/documents/{id}` / `DELETE /api/knowledge/batches/{source_batch_id}`（`routes/document_lifecycle.py:49/73`）。原始 `asset_documents` / `asset_document_snapshots` / `asset_raw_segments` 全不动，下次重挖还能 RESTORE。

### 3.4 source_batch 是什么

「一次摄取批次」=「一次 mining run 的输入集」的元信息载体。

- 表：`asset_source_batches`，字段 `id` / `batch_code`（如 `batch-{run_id前8位}`）/ `source_type` enum 8 种 / `domain` / `description` / `created_by` / `metadata_json`。
- 由 mining run 启动时创建（`jobs/run.py:813`），**与 upload 的 `upload_batch_id`（磁盘目录名）完全不对应**——这是新模块最容易踩的坑。
- 与文档关系：通过 `asset_document_snapshot_links.source_batch_id`（每次摄取记一次）。
- 与 mining_run：当前 1:1（schema 允许 1:N）。
- 撤回粒度：`DELETE /api/knowledge/batches/{source_batch_id}` 一次性撤掉该 batch 的所有文档。

### 3.5 多版本/快照机制（最精巧的部分）

三层模型：`asset_documents`（身份）→ `asset_document_snapshot_links`（事件流）→ `asset_document_snapshots`（内容寻址）。

snapshot 的去重键是 `UNIQUE (domain, normalized_content_hash)`——**同内容复用**，这就是 raw_segments / retrieval_units 都 FK→`document_snapshot_id` 而非 `document_id` 的原因。

重新解析同一文档（`decide_document_lifecycle_action` `jobs/run.py:26`）：

| 场景 | 触发条件 | 行为 |
|---|---|---|
| **SKIP** | active 哈希 == 新哈希 | 不重挖；不建新 link，复用 active 那条 |
| **RESTORE** | 无 active selection，但历史 link 命中同哈希 | 不重挖；新建一条 link，下个 build 可选中 |
| **UPDATE** | document_key 存在但哈希变了 | 创建新 snapshot，跑完整管线；旧 snapshot 保留 |

**不变量**：snapshot 是「内容寻址」、document 是「身份寻址」、link 是「事件流」。多版本之间没有版本号字段——时间序由 `linked_at` 提供。撤回不删 snapshot，重挖可直接 RESTORE。

---

## 4. 文档/资产管理 API 端点清单

全部挂 `/api` 前缀（FastAPI 直接注册，`mining/api/app.py:82-89`）。前端经控制面 `/api/control-plane/api/v1/proxy/{domain}/{service}/...`（见 CLAUDE.md「前端调用范式」）。

| 类别 | Method | Path | 作用 |
|---|---|---|---|
| 上传 | POST | `/api/uploads` | multipart 多文件，自动解压归档（`uploads.py:65`）|
| 上传 | GET | `/api/uploads` | 列磁盘上传批次（不入库）（`uploads.py:179`）|
| 上传 | GET | `/api/uploads/config` | 前端校验用大小/扩展名限制 |
| run | POST | `/api/runs` | 异步启动挖掘（202，每域 mutex 防并发）（`runs.py:77`）|
| run | GET | `/api/runs[/...]` | run 详情/阶段/进度/文档/产物/取消/发布/续跑/trace（`runs.py:169-937`）|
| 资产 | GET | `/api/knowledge/stats` | active release 计数（`knowledge.py:78`）|
| 资产 | GET | `/api/knowledge/documents[/...]` | active 文档/segment/unit/relation（基于 `_ACTIVE_SCOPE_CTE`）|
| 资产 | GET | `/api/knowledge/batches` | 按 source_batch 聚合 |
| 生命周期 | GET | `/api/knowledge/documents/{id}/download` | 下载原件（路径遍历防护）|
| 生命周期 | DELETE | `/api/knowledge/documents/{id}` | 软撤回单文档 |
| 生命周期 | DELETE | `/api/knowledge/batches/{source_batch_id}` | 软撤回整批次 |
| 构建 | GET | `/api/builds[/...]` `/api/releases[/...]` | build/release 列表 + active |

### 新模块的空白（研讨参考）

- ❌ 没有 `PATCH /api/documents/{id}`（改 metadata/document_type）
- ❌ 没有 `POST /api/knowledge-bases`、`GET /api/knowledge-bases`（KB CRUD 不存在）
- ❌ 没有 `POST /api/documents/{id}/reparse`（强制重挖单文档；要重挖只能整批 run）
- ❌ 没有文档级「软删除到回收站」概念——撤回立刻 publish 新 release，serving 立刻看不见，无 grace period
- ❌ 上传与挖掘完全解耦，没有「上传即入库」概念

---

## 5. Python 侧分库的真实情况（订正 CLAUDE.md）

CLAUDE.md 说「registry 的 `database:` 块 Python 侧零读取」——**代码层不准确**。

- `mining/infra/domain_db.py:74-89` 的 `resolve_domain_database` **确实读取** `entry.get("database")` 内联块（line 85）。
- 但 `main_control_service/config/domain_registry.yaml` 里 3 个域（cloud_core_network / civil_engineering / odn）的内联 `database:` 块**都指向同一个物理库 `coremasterkb`**（host 121.89.90.178, dbname coremasterkb），generic 走默认（也是 coremasterkb）。
- **结论**：结果上「mining 永远写 coremasterkb 一个库」，但原因是 registry 配置如此，不是代码不读。想让某域真正分库，改 registry 即可，不改代码。
- `pg_config.py:59-78` 的 `conninfo_from_env` 确实是死代码（有定义无调用），这条 CLAUDE.md 论断本身正确。

> Java 侧相反：`DomainPoolManager` 真的会为每个声明了 `database:` 块的域建专用 Hikari 池并建池即验连（`DomainPoolManager.java:146-151`）。**所以 Java 若被指向别的库，会读不到 mining 写的数据**——两侧配置必须一致。

---

## 6. GitHub 协作与 CI 现状

- **远端**：`https://github.com/fzl194/AgenticKB.git`（个人账号），仅 `master` 一个分支，仅 1 条 commit（`0dd66ca init`）。
- **CI**（`.github/workflows/build.yml`）：唯一工作流，**只构建镜像推 GHCR**（push master / `v*` tag / 手动触发）。**不跑任何测试**（无 pytest / mvn test / 前端 build），无 lint / 安全扫描 / 依赖审计 / PR 校验。
- **协作约定文件**：❌ 无 CODEOWNERS / CONTRIBUTING.md / PR 模板 / issue 模板 / LICENSE。
- **结论**：当前是「单人直推 master」最简工作流，PR review / CI 门禁**完全不存在**。CLAUDE.md「常用命令」里的测试命令只能本地手动跑。

> 🔧 议题一（GitHub 协作）会基于这个现状给出 fork+PR 的具体配置方案。

---

## 三色清单

### 🟢 设计稳健 / 工作正常
- asset_core 六层主链（身份/内容/链接/切片/检索/发布）数据模型完整、不变量由 DB 约束保证
- 三层文档模型（document 身份 / snapshot 内容寻址 / link 事件流）——撤回可逆、内容去重、历史可追溯
- 发布语义：部分唯一索引保证「一域至多一个 active release」+ advisory lock 串行化
- 软撤回机制完整（克隆 build + removed + publish，原始数据不动）
- 域隔离在身份层强制（`(domain, *)` 唯一键）
- 三库逻辑分库边界清晰

### 🟡 有坑 / 需注意
- 没有 KB 实体——domain 是隐式 KB，新模块要决定「升格 domain 还是加 kb_id」
- 状态分散在 4 张表，没有单一 status 字段（排查文档状态要 JOIN 多表）
- `source_batch` 与 upload 的 `upload_batch_id` 完全不对应（命名陷阱）
- CI 不做任何测试，schema/API 改动只能靠本地 `pytest` 兜底（且 knowledge_mining 测试强绑真实 PG）

### 🔴 缺失 / 待补
- KB CRUD 完全不存在
- 文档元信息编辑（PATCH）、单文档重挖（reparse）、回收站均不存在
- 上传与挖掘解耦，无「上传即入库」
- 协作约定文件全缺（CODEOWNERS / PR 模板 / issue 模板）
- CI 只构建镜像，零测试门禁
