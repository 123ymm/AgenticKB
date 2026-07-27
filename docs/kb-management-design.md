# 知识库管理 —— 技术设计

> **状态**：草案 / 待评审
> **版本**：v0.1（2026-07-27）
> **分支**：`feat/kb-management`
> **配套**：需求文档 `docs/知识库管理-需求文档.md` v0.2。本文是需求确认后的**实现级**设计（DDL / 代码结构 / 接口契约 / 迁移）。
> **范围**：仅 KB 管理本身。本体 LLM-wiki（议题三）、GitHub 协作（议题一）不在本设计内。

---

## 0. 设计总览

```
┌── knowledge_mining (Python) ────────────────────────────┐
│  mining/kb/   ← 新 package（用户面，前端只调这里）         │
│    /api/kb/*            KB CRUD + 文件管理 + 触发挖掘      │
│    写: kb_users / knowledge_bases / kb_members /          │
│        asset_documents（身份+文件位置）                   │
│  mining/{stages,jobs}  ← pipeline，仅 db_write 微调        │
│    写: asset_* 内容表（snapshot/segments/...）；          │
│        对 asset_documents 零写                            │
└──────────────────────────────────────────────────────────┘
                       │ PostgreSQL
                       ▼
┌── agent_serving_java ────────────────────────────────────┐
│  范式 scope_resolve 带 kb_ids → 过滤 active release snapshot │
│  SourceRef 带 kb_id/kb_name（来源标注）                   │
└──────────────────────────────────────────────────────────┘
```

**两条铁律**（来自需求 §3.3）：
1. **每个表单一写方**：KB 独占 `asset_documents`；mining 独占其他 asset_* + 运行态表。
2. **hash 去重**：原子知识全挂 snapshot，hash 相同的两个 KB 文档共享 snapshot，零新增原子知识。

---

## 1. 数据层

### 1.1 新表（`databases/kb/`）

> 风格对齐 asset_core：`TEXT` 主键 / `TEXT` 时间戳 / `JSONB`。schema 目录与 `ontology/` 同级，FK 跨库指向 asset_core（已有先例）。

**`databases/kb/schemas/001_kb_users.sql`**
```sql
CREATE TABLE IF NOT EXISTS kb_users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,          -- 登录名 / 控制面头部注入名
    display_name  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at    TEXT NOT NULL
);
```

**`databases/kb/schemas/002_knowledge_bases.sql`**
```sql
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id            TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,                 -- 必须是 domain_registry.yaml 合法域（应用层校验）
    name          TEXT NOT NULL,
    description   TEXT,
    owner_id      TEXT NOT NULL REFERENCES kb_users(id),
    visibility    TEXT NOT NULL DEFAULT 'private'
                  CHECK (visibility IN ('private','shared','public')),
    status        TEXT NOT NULL DEFAULT 'active',  -- 仅 active（归档已砍，需求 D11）
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (domain, name)                        -- 域内 KB 名唯一
);
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_domain ON knowledge_bases(domain);
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_owner ON knowledge_bases(owner_id);
```

**`databases/kb/schemas/003_kb_members.sql`**（仅 `visibility='shared'` 时用）
```sql
CREATE TABLE IF NOT EXISTS kb_members (
    kb_id     TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL REFERENCES kb_users(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer','editor')),
    added_at  TEXT NOT NULL,
    PRIMARY KEY (kb_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_members_user ON kb_members(user_id);
```

### 1.2 `asset_documents` 改造（`databases/asset_core/schemas/004_kb_isolation.sql`）

```sql
ALTER TABLE asset_documents ADD COLUMN IF NOT EXISTS kb_id          TEXT REFERENCES knowledge_bases(id) ON DELETE SET NULL;
ALTER TABLE asset_documents ADD COLUMN IF NOT EXISTS storage_path   TEXT;   -- 文件落盘绝对/相对路径
ALTER TABLE asset_documents ADD COLUMN IF NOT EXISTS directory_path TEXT;   -- 目录层级字符串，如 "5G规范/AMF"
ALTER TABLE asset_documents ADD COLUMN IF NOT EXISTS owner_id       TEXT REFERENCES kb_users(id) ON DELETE SET NULL;

-- UNIQUE 调整：原 (domain, document_key) → (kb_id, document_key)
-- 存量 kb_id NULL，Postgres 多 NULL 不冲突，向后兼容
ALTER TABLE asset_documents DROP CONSTRAINT IF EXISTS asset_documents_domain_document_key_key;
ALTER TABLE asset_documents ADD CONSTRAINT uq_asset_documents_kb_key UNIQUE (kb_id, document_key);
CREATE INDEX IF NOT EXISTS idx_asset_documents_kb_id ON asset_documents(kb_id) WHERE kb_id IS NOT NULL;
```

> **不设 `upload_status` 列**（难点 A 的关键）：文档状态由 KB 读时派生（见 §4.2），避免 mining 写 `asset_documents`。

### 1.3 DDL 执行顺序

`pg_schema.py` 按序执行。新增顺序：
```
databases/kb/001_kb_users → 002_knowledge_bases → 003_kb_members
databases/asset_core/004_kb_isolation（ALTER 引用 knowledge_bases，必须在 kb 表之后）
```
ontology 仍最后（其 FK 指向 asset_core，不变）。

### 1.4 关键关系

```
kb_users ◄──owner── knowledge_bases ◄──kb_id── asset_documents
    ▲                   │ owner                          │
    │              kb_members                            │
    └──────────────────┘                                 ▼
                                          asset_document_snapshot_links → snapshots → segments/units
```

---

## 2. KB package（嵌入 `knowledge_mining/mining/kb/`）

### 2.1 目录结构

```
knowledge_mining/mining/kb/
  __init__.py
  routes/
    kbs.py            # /api/kb CRUD
    documents.py      # /api/kb/{kb_id}/documents 文件管理 + 上传/解压
    mining.py         # /api/kb/{kb_id}/mine 触发挖掘
  services/
    kb_service.py     # KB CRUD + 成员管理 + 可见性校验
    document_service.py  # 文件 CRUD + zip 解压 + 目录层级 + storage_path
    mining_trigger.py # 触发 mining run（携 document_ids + kb_id）
  auth.py             # Phase 1：从控制面头部注入 X-KB-User → kb_users
  storage.py          # 落盘路径策略
```

路由注册到 `mining/api/app.py`（与现有 uploads/runs/knowledge 平级）。

### 2.2 路由清单

| Method | Path | 作用 |
|---|---|---|
| POST | `/api/kb` | 创建 KB（domain + name + visibility） |
| GET | `/api/kb` | 列我可见的 KB（owner + member + public） |
| GET/PATCH/DELETE | `/api/kb/{kb_id}` | KB 详情 / 改元信息 / 软删 |
| POST/GET | `/api/kb/{kb_id}/members` | 加成员 / 列成员（shared） |
| POST | `/api/kb/{kb_id}/documents` | 上传文件（支持 zip 自动解压，保留目录） |
| GET | `/api/kb/{kb_id}/documents` | 列文件（云端文件管理观感：名/大小/类型/上传时间/目录） |
| GET/PATCH/DELETE | `/api/kb/{kb_id}/documents/{doc_id}` | 文件详情 / 改元信息 / 软撤回 |
| GET | `/api/kb/{kb_id}/documents/{doc_id}/download` | 下载原件 |
| POST | `/api/kb/{kb_id}/mine` | 触发挖掘（可选 doc_ids 子集） |

### 2.3 上传 = 建文档身份

`document_service.upload(kb_id, file)`：
1. 落盘到 `{upload_root}/{kb_id}/{directory_path}/{filename}`（zip 先解压，按相对路径填 `directory_path`）。
2. INSERT `asset_documents`：`id` / `domain`（从 KB 取）/ `kb_id` / `document_key`（按 KB 内 filename+path 生成，保证 KB 内唯一）/ `document_name` / `document_type`（按 mime/扩展名）/ `storage_path` / `directory_path` / `owner_id` / `metadata_json`。
3. **不计算 hash、不建 snapshot**（挖掘时才算）。
4. 返回 doc id；状态派生 = `uploaded`（无 mining_run_document、不在 release）。

> 复用现有 `infra/archive_extractor.py` 的 zip/rar 解压能力（uploads.py 已在用）。

---

## 3. 挖掘触发与 mining db_write 适配（难点 A）

### 3.1 触发挖掘

`mining_trigger.mine(kb_id, doc_ids=None)`：
1. 校验：caller 是 KB owner/editor。
2. 选文档：`doc_ids` 或 KB 下所有 `uploaded` 文档。
3. 建 `mining_run`（携 `kb_id` 入 `metadata_json`）；为每个文档建 `mining_run_documents` 行，`document_id` ← 已存在的 `asset_documents.id`，`action='NEW'`（首挖）。
4. 起后台线程跑 `run()`（复用现有机制，runs.py 的 mutex/线程模型不变）。

### 3.2 mining db_write 改造

**当前**（`knowledge_mining/mining/snapshot/__init__.py:17` `select_or_create_snapshot`）：
```
get_document_by_key → upsert_document（建/改 asset_documents）
→ get_snapshot_by_hash → upsert_snapshot → insert_snapshot_link → 写 segments/units
```

**改造后**（文档已由 KB 预建，mining 不再建身份）：
```
find document by id（mining_run_documents.document_id，必存在）
→ 从 storage_path 读文件 → parse → 计算 normalized_content_hash
→ get_snapshot_by_hash(domain, hash)：
     命中 → 复用 snapshot（另一 KB 的同 hash 文档已挖，零新增原子知识）
     未命中 → 建 snapshot
→ insert_snapshot_link（本文档 ↔ snapshot）
→ 写 segments / units / embeddings（挂 snapshot）
```

**删掉 `upsert_document` 调用**——mining 对 `asset_documents` **零写**，保住「每个表单一写方」。

> `decide_document_lifecycle_action`（`jobs/run.py:26`）的 NEW/UPDATE/SKIP/RESTORE 逻辑保留：KB 上传=NEW；重挖内容变了=UPDATE；hash 不变=SKIP/RESTORE（复用 snapshot）。

### 3.3 兼容旧 `/api/runs`（目录式）

旧的 `POST /api/runs`（input_path 扫盘）保留，但内部改为：扫盘 → 为每个文件建 `asset_documents`（若无）→ 再走 KB 触发流程。**或**直接废弃，统一走 `/api/kb/{id}/mine`。**建议起步保留但标记 deprecated**，避免破坏现有调用方；后续删。

### 3.4 文档状态派生（KB 读时计算，不存列）

`document_service.derive_status(doc_id)` 通过 JOIN 推导：

| 派生状态 | 判定（取最新） |
|---|---|
| `uploaded` | 无 mining_run_document，且不在 active release |
| `mining` | 最新 mining_run_document.status ∈ {pending, processing} |
| `failed` | 最新 mining_run_document.status = failed |
| `published` | 在 active release 的 build_document_snapshots 中 selection_status=active |
| `withdrawn` | 最新 build 中 selection_status=removed（或历史进过 release、当前不在） |

单一真相源 = 运行态表 + release，无状态同步问题。

---

## 4. 范式承载 KB 集合（难点 B）

### 4.1 scope_resolve 算子加 `kb_ids` 参数

`agent_serving_java/.../operator/operators/output/ScopeResolveOperator.java`：
- `paramSchemaJson` 加 `kb_ids: array<string>`（可选）。
- 执行时：解析 active release 的 `(document_id, snapshot_id)` 集合后，若 `kb_ids` 非空，加过滤 `asset_documents.kb_id IN (kb_ids)`，缩小 snapshotId 集。
- 结果仍塞 `ctx.attributes`，下游 retrievers 按 snapshotIds 过滤，**SQL 不动**。

### 4.2 范式 JSON 示例

```json
{
  "nodes": [
    {"id": "input",  "type": "request_input"},
    {"id": "qu",     "type": "query_understanding", "inputs": {"query": "input.query"}},
    {"id": "scope",  "type": "scope_resolve",
                     "params": {"kb_ids": ["kb-aaa", "kb-bbb"]}},
    {"id": "fts",    "type": "fts", "inputs": {"query": "input.query", "scope": "scope.scope"}},
    {"id": "dense",  "type": "dense_vector", "inputs": {"queryEmbedding": "qu.queryEmbedding", "scope": "scope.scope"}},
    {"id": "fuse",   "type": "weighted_rrf", "inputs": {"candidates": ["fts.candidates", "dense.candidates"]}},
    {"id": "rerank", "type": "model_rerank", "inputs": {"candidates": "fuse.candidates", "query": "input.query"}},
    {"id": "out",    "type": "assemble", "inputs": {"candidates": "rerank.candidates", "understanding": "qu.understanding", "scope": "scope.scope"}}
  ],
  "edges": [ ... ]
}
```

`operator_paradigm` 表已存范式 JSON，`kb_ids` 就在 `scope_resolve.params` 里——**不需新表**。

### 4.3 设计态选 KB

范式编辑器（前端）调 `GET /api/kb`（我可见的 KB）→ 选若干 → 写进 `scope_resolve.params.kb_ids` → 编译/发布范式。设计者只能选自己可见的 KB，故查询时无权限失败场景（需求 9.1）。

---

## 5. serving 改造（最小侵入）

| 改动点 | 文件 | 内容 |
|---|---|---|
| scope 过滤 | `ScopeResolveOperator.java` + `AssetRepository.resolveActiveScope`（:55） | 加 `kb_id IN (kb_ids)` 过滤 |
| 来源标注 | `domain/SourceRef.java` + hydrate | 加 `kb_id` / `kb_name`，靠 `unit→snapshot→link→document→kb_id` 回溯 |
| retrievers SQL | 各 mapper | **不动**（只缩小 snapshotId 集） |

`SearchRequest` **不带 kb_ids**（范式化；KB 集合在范式里）。

---

## 6. 权限与 auth

### 6.1 可见性校验（`kb_service`）
- `private`：仅 owner。
- `shared`：owner + kb_members。
- `public`：任何认证用户。
- 「我可见的 KB」= owner 的 + member 的 + public 的。

### 6.2 auth Phase 1（控制面头部注入）
- `kb/auth.py`：从请求头 `X-KB-User: <username>` 取用户名 → 查/建 `kb_users` → 注入当前用户。
- 与 `main_control_service/config/system/ip_whitelist.yaml` 配合（内网）。
- Phase 2 接真实登录时，只换身份来源，表与权限逻辑零改。

---

## 7. 迁移方案

1. **建 kb 表 + ALTER asset_documents**：新库直接跑 DDL；存量库跑 `databases/kb/*` + `asset_core/004_kb_isolation.sql`（幂等）。
2. **存量文档**：`kb_id = NULL`（未归类）。不传 kb_ids 的范式仍能搜到（向后兼容，验收 §9.4）。
3. **可选 backfill 脚本**：每个 domain 建一个「默认 KB」（owner=系统用户），`UPDATE asset_documents SET kb_id=<default> WHERE kb_id IS NULL`。
4. **用户**：Phase 1 按需创建 `kb_users`（首次见到 `X-KB-User` 时 upsert）。

---

## 8. 不变量、风险与取舍

| 项 | 说明 |
|---|---|
| ✅ 每表单一写方 | KB 写 asset_documents；mining 写其他 asset_*；serving 只读 |
| ✅ hash 去重零新增 | 同 hash 跨 KB 文档共享 snapshot，mining 走 SKIP/RESTORE |
| ⚠️ 状态派生的读代价 | 文档状态 JOIN 运行态+release 计算；列表场景可缓存 |
| ⚠️ 旧 /api/runs 兼容 | 起步保留 deprecated，内部走 KB 登记；后续删 |
| ⚠️ 本体域级共享 | 实体图域级，KB 过滤只在 unit/snapshot 层；同域 KB_A 的实体 KB_B 可见（需求 §3.4-3） |
| ⚠️ 范式 kb_ids 静态 | KB 集合在范式设计态固定；KB 删除/改可见后老范式可能引用失效 KB，需校验或运行时容忍空 |

---

## 9. 实现阶段划分（过渡到实现计划）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1 数据层 | kb 三表 + asset_documents ALTER + DDL 顺序 + 迁移脚本 | — |
| P2 KB package 骨架 | 路由 + 服务 + auth 头部注入 + KB CRUD | P1 |
| P3 文件管理 | 上传/zip 解压/目录/CRUD/下载/状态派生 | P2 |
| P4 mining 适配 | db_write 砍 upsert_document + 触发流程 + 旧 /api/runs 兼容 | P1,P3 |
| P5 serving 范式化 | scope_resolve kb_ids + SourceRef 来源标注 | P1 |
| P6 权限 + 迁移 | 可见性校验全链路 + 存量 backfill | P2-P5 |

每阶段独立可测、可合并（契合议题一的 feature-branch + PR 流）。

---

## 10. 待定 / 后续

- 压缩包解压后的「多文档」边界（一个 zip 产出 N 个文档，目录层级如何映射 directory_path）——P3 细化。
- KB 删除的级联语义（软删 KB → 其下文档何去何从）——P2 细化，倾向「KB 软删 = 拒绝写入，文档保留可读」。
- 范式引用了已删 KB 的运行时容忍策略——P5 细化。
- auth Phase 2（真实登录）——独立后续项目。
