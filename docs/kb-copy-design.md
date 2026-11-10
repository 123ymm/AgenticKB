# 知识库拷贝（KB Copy）—— 技术设计

> **状态**：已实施（2026-10-23，随 1.1.16）——实施记录见 §7.1
> **版本**：v1.1（2026-10-23）——v1.0 评审后简化：去掉直建 Build（"即拷即用"降级为"快照血缘共享"，检索由新库自行挖掘获得，见 §0.3 变更记录）
> **上游**：`docs/kb-management-design.md` v0.2 · ADR-0001（D2/D5/D7/O1）
> **范围**：同域内整库拷贝。跨域拷贝、制品复制、直建 Build（即时可检索）见 §9。

---

## 0. 需求与语义定界

### 0.1 用户故事

| 场景 | 诉求 |
|---|---|
| **备份/建档** | 大改前留一份文件+知识血缘的完整副本 |
| **沙箱试验** | 副本上自行选范式重挖，坏了整库删除，源库无感 |
| **模板分发** | 管理员维护样板库，成员拷贝 public 库起步（read 即可拷） |

### 0.2 已拍板决策

| 决策点 | 结论 | 版本 |
|---|---|---|
| 拷贝语义 | **快照血缘共享，不建 Build**：拷文件+目录+快照 links；拷完文档均为 `uploaded`、**不可检索**；新库自行选范式触发挖掘，指纹命中自动复用既有快照（零 LLM 重嵌） | v1.1 修订（v1.0 曾为"即拷即用"） |
| 配置范围 | 仅复制 **visibility**（+ name 派生、description 元信息）；**不复制**挖掘范式 / 检索范式 / 成员 | v1.0 |
| 知识制品 | V1 **不复制** kb_artifacts | v1.0 |

### 0.3 v1.0 → v1.1 变更记录与语义后果

去掉 `assemble_build` 直建，是**大幅简化**，但语义后果必须理解清楚：

1. **links 是血缘，不是可见性也不是保护**：`snapshot_links` 不使文档进入检索（mined/serving 均按 Build 成员派生）；purge 对快照的"还有用"判定只认 serving 集，**不认 links**（`purge_service.py` reclaim 注释明言）。因此拷贝后、新库首挖前，共享快照**不受保护**——源库重挖且旧快照不再被任何库 serving → 废弃 7 天后可被回收（届时新库再挖走全量重算路径，功能仍正确，只是失去复用）。
2. **复用的真实机制是指纹缓存**：新库首挖时 `UNIQUE(domain, snapshot_fingerprint)` 命中（同域 + 同 raw_hash + 同 parser/workflow/compiler 指纹）→ 复用同一快照行，v2 资产（units/embeddings/结构树）按 snapshot_id 已存在 → 零 LLM 成本。**与 links 无关**——links 的价值是血缘完整性与未来直建 Build（§9）的铺垫。
3. **范式选择的耦合**：指纹含 `workflow_graph_hash`。新库自选范式若与源快照版本不同（范式已演进/选了别的）→ 指纹失配 → 全量重解析重嵌入（真实 LLM 成本）。操作建议：拷贝完成后 UI 引导选择与源库相同的挖掘范式。
4. 解析阶段对新 document_id 仍会重跑（shadow_parse 幂等探针按 document_id 键控，新 id 必 miss）——CPU 成本，无 LLM；快照提交时指纹命中即复用。

### 0.4 非目标

- **跨域拷贝**——指纹含 domain，跨域必然新快照；由导出/导入通道承接。
- **MCP 钥匙 / 检索账本**——钥匙是用户级资产；拷贝无检索行为。
- **异步任务化**——纯 DB 插入事务（无字节 IO、无 LLM、无 Build 组装），同步 201 + 单事务原子性。
- **kb_document_refs 跨库引用、onenet_imports 记录**——只拷源库自有文档。

---

## 1. 共享机制（schema 事实依据）

| 共享物 | 机制 | 依据 |
|---|---|---|
| **对象字节** | 新 `asset_documents` 行指向同一 `storage_object_id`，MinIO 零复制 | purge 按**孤儿才回收**（`_reclaim_objects_if_orphaned` 全局引用计数），多文档引用安全 |
| **快照（血缘）** | 新文档经 `asset_document_snapshot_links` 链到源快照 | 该表天生多对多（`001_asset_core.sql:48`）；ADR-0001 O1「指纹不含 document_id」的设计位 |
| **快照（复用）** | 新库首挖时指纹命中复用同一快照行 → v2 资产（units/embeddings/结构树/表格，全部按 `snapshot_id` 键控，013 迁移）自动共享，零重嵌 | `asset_document_snapshots` 的 `UNIQUE(domain, snapshot_fingerprint)` |

**不写 Build**：`asset_builds` / `asset_build_document_snapshots` 零触碰——新库无 Build → serving 视图为空 → 不可检索（预期行为）。

---

## 2. 拷贝口径

拷贝**源库全部 active 文档**；对每份文档：

```
文档 → 源库最新 validated Build 的 active snapshot（与「mined」派生同口径）
  ├─ 有映射 → 新文档 + snapshot_link（血缘）      → 拷后 status='uploaded'
  └─ 无映射 → 新文档仅身份行                      → 拷后 status='uploaded'
```

两类文档拷后状态一致（`uploaded`——无 Build 成员），区别只在血缘行有无。新库生命周期：

```
拷贝(201) → 全部 uploaded，不可检索
  → 用户在设置里选挖掘范式 → 触发 /mine
  → 指纹命中：复用快照，秒级晋升 vector_ready（CPU-only）
  → 指纹失配（范式演进/换范式）：全量重算（真实 LLM 成本，UI 需提示）
```

---

## 3. API 设计

```
POST /api/kb/{kb_id}/copy        201 Created（同步、单事务）
```

**请求体**（全部可选）：

```json
{
  "new_name":    "string ≤80",        // 缺省自动派生 "<源名>-副本-YYYYMMDD"；冲突 → 409
  "visibility":  "private | public",  // 缺省 = 源库值，可覆盖（受防放大规则约束）
  "description": "string | null"      // 缺省 = 源库 description，可覆盖/清空
}
```

**响应 201**：新 KB 完整行（同 `GET /api/kb/{kb_id}` 形状），附加：

```json
{ "copy_source": { "kb_id": "<源id>", "kb_name": "<源名>", "copied_at": "…" },
  "copy_stats":  { "documents": 120, "folders": 18, "linked_snapshots": 118 },
  "next_step_hint": "select_mining_paradigm" }   // 前端据此引导选范式
```

**错误码**：

| HTTP | 条件 |
|---|---|
| 404 | 源库不可见（不泄露存在性原则不变） |
| 403 | `domain_not_bound`：操作者未绑定源库所在域 |
| 403 | `visibility_not_allowed`：非 owner/site-admin 显式请求 public（防放大规则） |
| 409 | 目标名与操作者同域活跃库重名（唯一约束兜底；并发双击派生名相同 → 第二次 409） |
| 422 | 源库无 active 文档 / active 文档数 > 5000（同步事务上限，提示导出导入） |

> 实施勘误（2026-10-23）：v1.1 表中原有「409 源库 status='deleting'」一行不可达——
> 读权限门 `is_visible` 本就要求 `status='active'`，删除中/已软删的源库对所有人统一
> 404（不泄露存在性）；「校验后、加锁前」的竞态由事务内 `FOR SHARE + active 复核`
> 兜住（复核不过 → 404）。

**权限**：源库 `is_visible` + 操作者 `can_create_in_domain`；新库 `owner_id = 操作者`，`mining_workflow_id = NULL`，`default_paradigm_id = NULL`。

**可见性防放大规则**：member 拷 private 库、任何人拷 public 库，本质是"把读到的知识变成自己名下的完整副本"——合法；但若允许副本设 `public`，等于把他人私有知识域内公开。规则：**`visibility='public'` 仅当操作者是源库 owner 或 site-admin**；否则强制 `private`。

---

## 4. 事务与数据流

**前提**（非新约束，与现有上传路径同构）：`kb_*` 与 `asset_*` 同物理库（`KbDB` 经 `pg_pool` 写 `asset_documents` 已是现状）；copy 只写这两族表，不触域池、不取域发布锁。

```
BEGIN
 1. SELECT … FROM knowledge_bases WHERE id=源 FOR SHARE      -- 与 purge(FOR UPDATE) 互斥：
                                                               拷贝期间源库不被并发硬删
    复核 status='active'；读源库最新 validated Build 的 active 成员（§2 映射）
 2. INSERT knowledge_bases（新行：name/visibility/description、owner=操作者、
    status='active'，metadata_json 记 copy_source 溯源）
 3. 目录树拷贝：kb_folders 逐行复制（新 id、新 kb_id、parent_id 重映射；
    path 原样——(kb_id, path) 唯一约束天然无冲突）
 4. 文档拷贝（批量）：asset_documents 新行
    - 新 document_id；document_key 按 relative path 原规则重建（新库内无冲突）
    - storage_object_id / source_raw_hash / content_revision / file_size /
      document_type / directory_path 同源；folder_id 重映射；owner_id=操作者
 5. 快照血缘（批量）：§2 有映射的文档
    INSERT asset_document_snapshot_links(新 doc_id ↔ 源 snapshot_id)
    relative_path/source_uri/title 取自源 link；source_batch_id=NULL
COMMIT → 201
```

任一步失败整体回滚——无半成品、无补偿清理。**无 Build 组装、无域锁、无 LLM、无对象 IO**——v1.0 中最重的三处（assemble_build 契约、域发布锁持有、合成 run_id）全部消失。

---

## 5. 不变量影响清单

### 5.1 安全的

| 不变量 | 影响 |
|---|---|
| 每表单一写方 | copy 写 `kb_*` + `asset_documents/links`——与上传路径同一 KB 写方通道；**完全不触 builds/资产表** |
| 文档身份永久（D7） | 新文档=新永久身份，源文档不动 |
| Snapshot 唯一版本根（D5） | 不新建快照只加 link；link 的 CASCADE(document)/RESTRICT(snapshot) 语义正确 |
| 文档状态读时派生 | 无 Build → 全部 `uploaded`，派生路径不变 |
| serving / scope / EvidenceRef | 新库无 Build → serving 视图为空，零影响 |
| 回收体系 | 对象按全局引用计数不误删；快照保护按 serving 集（拷贝后未挖期间不保护——§0.3-1 已明示，行为一致不引入新谓词） |

### 5.2 实施检查点

| # | 检查点 | 动作 |
|---|---|---|
| V1 | **首挖复用路径**：新 document_id 走完整 workflow 时，快照提交处指纹命中确实复用（不新建快照、不重嵌）且 staging→final 幂等 | PG 集成测试：拷贝→选同范式→mine→断言快照数不变、embedding 调用数为 0、readiness=vector_ready |
| V2 | **范式失配路径**：换范式版本挖 → 全量重算正常走通（不是 bug 是特性，UI 提示） | 集成测试走通即可 |
| V3 | **quota 口径**：共享对象字节若按文档 sum 会双计 | 核对配额谓词 |
| V4 | **readiness/quality 派生**在"有 link 无 build"文档上的显示口径（应为 uploaded/低档，不报错） | 集成断言 |
| V5 | 源库重挖后旧快照被回收 → 新库后挖走全量重算——功能正确性验证 | 集成测试（§8-6） |

---

## 6. 前端

- **入口**：`KbListView` 行操作菜单 +「拷贝」→ `KbCopyDialog`：
  - 名称预填 `<源名>-副本-YYYYMMDD`；可见性 select 预填源值（非 owner 时锁定 private）；描述预填；
  - 静态提示：「拷贝包含全部文件与知识血缘；拷贝完成即可开始挖掘（选与源库相同的范式可复用已有解析与向量，几乎零成本）」；
  - 201 → 跳新库详情 → **顶部引导条**：「选择挖掘范式并开始挖掘，使知识可被检索」（`next_step_hint` 驱动，选完范式自动可挖）。
- `api/kb.ts` 加 `copyKb`；详情头「拷贝自」徽标（读 `copy_source`）。

---

## 7. 实施拆解（文件级）

| # | 文件 | 内容 | 量级 |
|---|---|---|---|
| 1 | `kb/services/copy_service.py`（新） | 编排：校验→锁→五步事务（§4）；typed 错误复用 kb_service 错误族 | ~250 行 |
| 2 | `kb/db.py` | +`get_kb_build_members(kb_id)`、批量 insert 助手（folders/docs/links）、`lock_kb_row_for_share` | ~130 行 |
| 3 | `kb/routes/kbs.py` | +`POST /{kb_id}/copy` + 错误映射 | ~60 行 |
| 4 | `tests/kb/test_copy_service.py` + PG 集成 | 名称/权限/防放大/空库/上限/血缘正确/首挖复用/失配路径/回收存活 | ~350 行 |
| 5 | `kb-ui` | `KbCopyDialog.vue` + 菜单项 + 引导条 + `api/kb.ts` | ~200 行 |
| 6 | `docs/` + `releases.json` | 本设计归档 + changelog | — |

### 7.1 实施记录（2026-10-23，随 1.1.16）

按上表落地，与设计的偏差与补充：

- **db.py 结构**：五步事务收拢为单方法 `KbDB.copy_kb(...)`（对齐 `transfer_kb_owner`
  的"锁+多步同事务"先例），service 层只做业务校验；serving 成员查询是
  `get_current_serving_snapshot` 的批量版（DISTINCT ON 同语义）。另加
  `count_active_documents_in_kb`（§3 闸门）。`get_kb` SELECT 增补 `metadata_json`
  下发（"拷贝自"徽标数据源；additive）。
- **新库 `mining_workflow_id = NULL`**（§3 拍板落地）：与"范式不复制"一致，
  `/mine` 对 NULL 范式返回 400 的既有语义就是选范式引导；KbDetail 类型注释
  本就写明该状态，前端挖掘面板无需改动。
- **副本文档 `created_at/content_updated_at = now`**（新行的时间起点），
  `modified_at`（文件 mtime 语义）同源保留；`storage_path`（legacy）与软删列不复制。
- **links 复制**：`relative_path/source_uri/title/source_storage_object_id/
  source_content_revision` 取自源 link 的 linked_at 最新一条（同 (doc,snapshot)
  多链时）；`source_batch_id=NULL`（批次归属源库上传事实）。
- **前端**：列表卡片「⋯」菜单改为**常驻**（拷贝 read 即可拷；改名/删除仍按写权限
  分级显示）；`KbCopyDialog` 预填派生名/源描述，可见性对非 owner 预锁 private；
  201 后跳新库详情，详情页常驻引导条（copy_source 存在且范式未选时）直达挖掘 tab。
- **测试**：`test_copy_service.py` 16 例（血缘/形状/防放大/权限/名称/闸门/生命周期
  隔离/路由映射 + 派生名纯函数）；PG 门控例按项目惯例 `KB_RUN_POSTGRES_ACCEPTANCE=1`
  真库跑。首挖复用（V1/V2）与回收存活（V5）属挖掘链端到端，留在验收清单 §8 随
  真库预演执行。
- **验证**：后端模块编译/导入 + 路由注册 ✅；kb 测试套件改动前后 FAILED/ERROR
  名单逐条一致（零回归）✅；前端 vue-tsc + vitest 556 例 + vite build ✅。

DDL：**零新增、零迁移**。复用面：`stages/publishing.py`、域锁、purge 谓词——**全部不再触碰**（v1.0 的第 4/5 项实施项消失）。

---

## 8. 验收清单

1. KB_A（3 文件已挖，vector_ready）→ 拷贝 → KB_B：文件列表/目录树一致，全部文档 `uploaded`，**检索面板无结果**（预期），`copy_source`/`copy_stats` 正确；
2. KB_B 设置选**与源相同范式** → mine → 秒级完成，`readiness=vector_ready`，快照总数不变（复用），llm_service 无 embedding 调用，检索命中与源一致；
3. KB_B 选**演进后的范式版本** → mine → 全量重算（新快照），完成后可检索——伴随 UI 成本提示；
4. viewer 拷 public 库成功且为新库 owner；member 拷他人 private 库 404；未绑域 403；member 拷贝显式传 public → 403；
5. 同名 409 友好文案；空库 422；并发双击仅成功一次；
6. 拷贝后 KB_A 重挖+等回收窗口 → KB_B 后挖走全量重算路径，功能正确不崩溃；
7. 删除 KB_B（硬删）→ KB_A 完好，共享对象不回收。

---

## 9. 开放问题 / V2 方向

- **直建 Build（"即拷即用"）**：v1.0 设计的完整方案见 git 历史——links 已就位，追加 `assemble_build(snapshot_decisions, kb_id, run_id='kbcopy-<uuid>')` + 域发布锁即可升级；届时需恢复快照保护审查（serving 集天然覆盖，已验证）与 run_id 合成值反查检查。
- **拷贝时顺带复制 mining_workflow_id**：提高首挖指纹命中率（§0.3-3 耦合）；当前拍板不复制，若实操中用户常选错范式可重新评估。
- **首挖解析跳过优化**：挖掘前按指纹预查快照，命中则跳过 parse 重跑（纯 CPU 优化，非必需）。
- **跨域拷贝 / 制品复制 / 异步任务化**：同 v1.0 立场。
