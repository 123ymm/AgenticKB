# 现状：挖掘模块（knowledge_mining）

> 逐行核对源码产出。文档（`README.md`、`docs/stage-*.md`）描述的是已删除的 SQLite 时代架构，全程未参考。论断标 `(file:line)`。
>
**一句话现状**：挖掘是一条 16~18 阶段的 streaming pipeline，从文件落盘到 publish release 全链打通；解析/切片/篇章关系/检索单元/向量化/落库工作稳健。**本体线整体被 `has_ontology` 守卫**，多数域未引种本体 → 跳过；引种了的域里，实体抽取靠一个兼容兜底「可能」在跑，但**本体归纳（`ontology_induction`）因模板未注册而全程静默 no-op**。文档管理只有「上传 + 触发 run + 软撤回」，没有 KB 概念、没有文档元信息编辑——这正是新「知识库管理」模块的设计空间。

---

## 1. 完整管线阶段与顺序

入口：`knowledge_mining/mining/jobs/run.py:122` 的 `run()`。

### 1a. 全局阶段（run 级，串行）

| # | 阶段 | 入口 | 输入 → 输出 | 写表 |
|---|---|---|---|---|
| G0 | `ingest` | `run.py:190` → `ingest_directory`（`ingestion/__init__.py:61`）| `input_path` 目录 → `list[RawFileData]` | 只读文件系统 |
| G1 | 建 source_batch | `run.py:813` → `asset_db.upsert_source_batch` | batch_id(uuid4 hex) | `asset_source_batches`、`mining_runs.source_batch_id` |
| G2 | 每文档分类 | `run.py:882` → `decide_document_lifecycle_action`（`run.py:26`）| doc+域+channel+hash → NEW/SKIP/RESTORE/UPDATE | `mining_run_documents` |
| G3 | streaming 管线 | `run.py:1031` → `StreamingPipeline.process_all` | ctx 列表 → 完成的 ctxs | 见 1b |
| G4 | `graph_write`（全局落图，仅 `has_ontology`）| `run.py:1060` → `_run_graph_write`（`run.py:716`）| ctxs → 落 canonical 实体/mention/出处/候选 | `ontology_entities`、`asset_segment_entity_mentions`、`ontology_evidence_nodes`、`ontology_candidates`（**不建边**）|
| G5 | 人审 Gate 编排（`not phase1_only and has_ontology`）| `_pause("entity_review")` / `_run_induction` / `_pause("ontology_review")` / `_finalize_graph` | — | 见 1c |
| G6 | assemble+validate+publish | `run.py:1169/1186` → `publishing.py` | snapshot_decisions | `asset_builds`、`asset_build_document_snapshots`、`asset_publish_releases` |

### 1b. 文档级 streaming 阶段（`run.py:1018-1029`）

| # | 阶段 | workers | 入口 | LLM 模板 | 输入→输出 |
|---|---|---|---|---|---|
| D1 | `parse` | 1 | `parse_stage`（`pipeline.py:387`）| 无 | RawFile → SectionNode 树 |
| D2 | `segment` | 1 | `segment_stage`（`pipeline.py:402`）| 无 | tree → raw segments |
| D3 | `enrich` | max | `enrich_stage`（`pipeline.py:422`）| `mining-segment-understanding` | segments → 语义角色 + content_assessment |
| D4 | `entity_extract` | max（仅 `has_ontology`）| `entity_extract_stage`（`pipeline.py:431`）| `mining-entity-extraction` ⚠️ | segments → entity_refs（A:typed / B:`__untyped__`）|
| D5 | `resolve` | max（仅 `has_ontology`）| `resolve_stage`（`pipeline.py:440`）| 无（别名词典）| mention → canonical_name + resolve_status |
| D6 | `entity_relations` | **不入 streaming** ⚠️ | 仅由 `_finalize_graph` 间接调用 | 无 | segments → candidate_relations |
| D7 | `discourse` | min(max,2) | `discourse_stage`（`pipeline.py:458`）| `mining-discourse-relation` | segments → RST SegmentRelationData[] |
| D8 | `retrieval_units` | max | `retrieval_units_stage`（`pipeline.py:469`）| `mining-question-gen` + `mining-contextual-retrieval` | segments → 4 类检索单元 |
| D9 | `embedding` | max | `embedding_stage`（`pipeline.py:486`）| 无（llm_service 自管模型）| units → {unit_key, vector} |
| D10 | `db_write` | 1（强制串行）| `db_write_stage`（`pipeline.py:548`）| 无 | 所有产物 → 落库 |

> **注意 D6**：`pipeline.py:449` 有 `entity_relations_stage` 函数定义，但 `run.py:1018` 的 stages 列表**没把它挂进 streaming**——它只在 `_finalize_graph`（`run.py:466`）里通过 `reaggregate_edges` 被间接调用。这是设计意图（逐段产候选、全局做 NPMI 统计），但容易误读为漏挂。

### 1c. 全局本体线（仅当 `has_ontology=True`）

| 阶段 | 触发位置 | LLM | 写表 |
|---|---|---|---|
| `graph_write`（全局A）| `run.py:1060` | 无 | `ontology_entities`/`ontology_evidence_nodes`/`asset_segment_entity_mentions`/`ontology_candidates`(relation) — **不建边** |
| Gate2 `entity_review` | `run.py:1091` | — | run → `awaiting_review` |
| `ontology_induction`（N3）| `run.py:1095` | `mining-ontology-induction` ⚠️ pack 未声明 | `ontology_candidates`(node_type) |
| Gate1 `ontology_review` | `run.py:1096` | — | run → `awaiting_review` |
| `graph_write_final`（N5 回贴+建边）| `run.py:1100` | 无 | `ontology_entities`(rebind) / `ontology_entity_relations` / evidence / 计数重算 |

**Gate 顺序是反转的**（`test_review_gate.py:23-42` 锁住）：实体确认（Gate2）优先于本体确认（Gate1）——先把 `__untyped__` 实体确认干净，再交给 LLM 归纳类型，避免归纳输入混入噪声。

`resume()`（`run.py:307`）是 `awaiting_review` 后的续跑入口，从 `mining_run_documents` 重建 snapshot_decisions（`run.py:540`）。

阶段总数核对：run 级 6 + streaming 10 + 本体尾段 2 ≈ **16~18 个独立阶段**（CLAUDE.md「14+」准确）。

---

## 2. 各阶段真实职责

| 阶段 | 做什么 | LLM | 真在跑？ |
|---|---|---|---|
| **parse**（`stages/parse.py`）| 文件→SectionNode 树。Markdown 用 markdown-it-py；txt 按段落+token 切；PDF 用 pdfminer.six（`infra/pdf_parser.py`）；DOCX 用 python-docx（`infra/docx_parser.py`）。PDF/DOCX 软失败返回 None+last_error | 无 | ✅ |
| **segment**（`stages/segment.py`）| 树→raw segments。5 步：walk→merge small(<100t)→absorb cross-section orphans(<128t)→merge lead-in→split large(>512t)→inject structural breadcrumb。读 RetrievalPolicy | 无 | ✅ |
| **enrich**（`stages/enrich/__init__.py`）| 段落理解：分类 `semantic_role`、评估 `content_assessment.{is_substantive,is_navigation}`。tiny 段(<30t)跳过 | `mining-segment-understanding` | ✅（3 pack 声明，generic 除外）|
| **entity_extract**（`stages/entity_extract/__init__.py`）| 双通道实体抽取：A 通道落 typed ref；B 通道/低置信/类型外 落 `__untyped__` ref。读 active_node_types 作约束 | `mining-entity-extraction` | 🟡 **见 §4 重要订正** |
| **resolve**（`stages/resolve/__init__.py`）| 实体归一：Tier1 别名词典精确匹配→`auto`；其余→`pending`（触发 Gate2）。Tier2 向量默认关，Tier3 LLM 不做 | 无 | 🟡 依赖 entity_extract 先产出 |
| **entity_relations**（`stages/entity_relations/__init__.py`）| 同段实体两两配对，按 `active_relation_types.allowed_pairs` 过类型闸，产候选边。5 道质量闸的前 3 道在此 | 无 | 🟡 间接跑，要求 mention 表有已确认项 |
| **relations**（`stages/relations/__init__.py`）| **篇章 RST 关系**（独立线，不进 ontology_*），落 `asset_raw_segment_relations`。滑动窗口默认 15 段，LLM 抽 15 种 RST 关系 | `mining-discourse-relation` | ✅ |
| **retrieval_units**（`stages/retrieval_units/__init__.py`）| 4 类检索单元：raw_text(1:1)、entity_card(强类型)、table_row(按行)、generated_question(LLM)。跳过导航段 | `mining-question-gen` ✅ + `mining-contextual-retrieval` ✅ | ✅（cloud_core_network 的 contextualizer policy 关了，不实例化）|
| **embedding**（`pipeline.py:486` + `infra/embedding.py`）| 调 llm_service `/api/v1/embeddings`，模型自管 | 无 | ✅ |
| **db_write**（`pipeline.py:548`）| 一个事务写 snapshot+link+segments+relations+units；事务外 best-effort 写 embeddings；空 segments 不建 snapshot | 无 | ✅ |
| **graph_write**（`stages/graph_write/__init__.py`）| 跨文档聚合→canonical 实体+mention+出处+off-schema 候选。NPMI 阈值 0.3。**不建边** | 无 | 🟡 仅 `has_ontology=True` |
| **ontology_induction**（`stages/ontology_induction/__init__.py`）| 从人确认的 `__untyped__` 实体归纳 node_type 候选；DF 阈值 2，语料小时自适应降阈 | `mining-ontology-induction` | 🔴 **静默 no-op**（见 §4）|
| **publishing**（`stages/publishing.py`）| classify + assemble_build（增量 merge）+ validate + publish_release（domain lock）| 无 | ✅ |
| **withdrawal**（`stages/withdrawal.py`）| 软撤回（克隆 build + removed + publish）| 无 | ✅ |
| **eval**（`stages/eval.py`）| `run_eval`(Recall@K) + `run_data_quality_eval`(6 项结构检查)| 无 | 🔴 **死代码**（见 §5）|

---

## 3. 文档如何进入系统（为「知识库管理」铺路）

### 3.1 上传 → 文件落盘（与库解耦）

`POST /api/uploads`（`uploads.py:65`）：校验 domain → 生成 `batch_id = uuid4().hex[:12]` → 落盘 `{upload_root}/{domain}/{batch_id}/` → 自动解压 zip/rar。返回 `{upload_batch_id, domain, file_count, files, storage_path, extracted_archives}`。

**关键**：上传完全不知道数据库。`upload_batch_id` 只是磁盘目录名，**不写任何表**。`asset_source_batches` 是 mining run 启动时才创建的（`batch_code = f"batch-{run_id[:8]}"`，跟 upload_batch_id 没关系）。

### 3.2 文档归属：没有 KB 概念，三层隐式表达

1. **domain**：所有 asset_* 表都有 domain 列，物理隔离边界。
2. **source_batch**：一次 run 一个 batch，文档通过 `asset_document_snapshot_links.source_batch_id` 关联。
3. **channel**：发布通道（默认 `prod`），release/build 都按 domain+channel 索引。

> 当前 1 个 domain = 1 个隐式 KB。新模块要么 (a) `asset_documents` 加 `kb_id` 让一域多 KB，要么 (b) 新建 `knowledge_bases` 表把 domain 升格。详见数据库现状文档 §3.1。

### 3.3 文档生命周期 NEW/SKIP/RESTORE/UPDATE

决策：`decide_document_lifecycle_action`（`run.py:26-51`），状态来源 `AssetCoreDB.get_document_lifecycle_state`（`db.py:350`）——按 domain+channel+document_key+normalized_content_hash 查「当前 active 选了哪个 snapshot + 历史有没有这个 hash」。

- **NEW**：document_key 在 active 选区没有 → 建 document + 新 snapshot
- **SKIP**：active 选中且 hash 一致 → 复用，不重解析
- **RESTORE**：active 没选但历史选过且 hash 一致 → 复用历史 snapshot，新插一条 link
- **UPDATE**：active 选了但 hash 变了 → 复用 document_id，建新 snapshot

### 3.4 前端目前能做的 / 不能做的

能：上传、列磁盘 batch、触发 run、看进度、取消/发布/续跑、查 active 资产、下载原件、软撤回（单文档/整 batch）。

不能：增删文档元信息、给文档打标签、按上传批次管理资产、移动文档到 KB 分组、强制重挖单文档。

---

## 4. LLM 调用与模板（含对 CLAUDE.md 的重要订正）

### 4.1 submit_task 失败语义（关键）

`LlmClient.submit_task`（`llm_client.py:51-92`）失败模式：HTTP 异常 → `logger.warning` + `self.close()` + **返回 None**。调用方拿到 None 不进 `seg_tasks`，`poll_all` 没结果——**阶段没有任何报错，只是该段不产 LLM 输出**。这是整条本体线静默 no-op 的机制根源。

### 4.2 4 个 scenario pack 的 llm_templates 实际声明

| pack | question-gen | segment-understanding | discourse-relation | contextual-retrieval | entity-extraction | ontology-induction |
|---|---|---|---|---|---|---|
| `cloud_core_network` | ✅ v4 | ✅ v4 | ✅ v2 | ✅ v2 | ❌ | ❌ |
| `generic` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `civil_engineering` | ✅ v4 | ✅ v4 | ✅ v2 | ✅ v2 | ❌ | ❌ |
| `odn` | ✅ v4 | ✅ v4 | ✅ v2 | ✅ v2 | ❌ | ❌ |

### 4.3 ⚠️ 对 CLAUDE.md 的订正

CLAUDE.md 说 `mining-entity-extraction` 与 `mining-ontology-induction` 都因 pack 未声明而全程静默 no-op。**核实结果：只对了一半。**

- **`mining-entity-extraction`（订正）**：4 个 pack 的 domain.yaml 确实没显式声明它，**但** `infra/llm_templates.py:67-132` 的 `build_templates_from_profile` 会**自动从 `mining-segment-understanding` 模板残留的 `entities` schema 字段合成** `mining-entity-extraction`（line 80/103-106/124-130，注释「compat-1 兼容旧 Domain Pack 的独立实体抽取」）。3 个非 generic pack 的 segment-understanding schema 里确实有 `entities` 字段（如 `cloud_core_network/domain.yaml:208`），所以这个兜底**会触发**。该函数被 `run.py:625` 调用并 `client.register_template(tpl)` 注册到 llm_service。测试 `test_v14_domain_pack.py:185-194` 与 `test_pipeline_operators.py:491-498` 锁住了这条合成。**结论**：cloud_core_network/civil_engineering/odn 下 entity_extract **正常工作**（只要合成的 compat-1 模板被 llm_service 接受），不是 no-op。CLAUDE.md 这条已过时。
- **`mining-ontology-induction`（确认 CLAUDE.md 正确）**：grep 全仓库，既无 pack 声明，也无代码 fallback，也无注册。`OntologyInductor.induce()`（`ontology_induction/__init__.py:174-228`）的 `submit_task` 取不到模板返回 None → induce 立即返回 0 候选（line 207-208）。**后果**：node_type 永远不会被自动提议 → Gate1 永远没有 `proposed` 候选 → 永远不会 `_pause("ontology_review")`。但 off-schema 关系候选（`relation_candidates`）仍正常经 graph_write 进 `ontology_candidates`，所以 Gate1 的关系类型候选可能有内容，只是 node_type 永远空。
- **`generic` pack 无 llm_templates（确认 CLAUDE.md 正确）**：generic 域所有 LLM 阶段（enrich/discourse/question_gen/contextual）全降级，管线还能跑通但产出只有 raw_text 检索单元。

### 4.4 模板注册流程（`_init_llm` `run.py:597-681`）

1. `LlmClient.health_check()` 不通 → 返回 None，LLM 服务下线（`run.py:618`）
2. `build_templates_from_profile(profile, domain_id=...)` 构造模板列表
3. 每个模板 `client.register_template(tpl)`（`run.py:627`），调 llm_service `POST /api/v1/templates`

> 模板存在 llm_service 的 `agent_llm_prompt_templates` 表里，pack 只负责启动时注册——**若曾被人工 POST 进去则仍可用，查库才能确认环境实际状态**。代码层只能确认 pack 不会注册。

---

## 5. 死代码与同构分裂

### 5.1 死代码

| 项 | 位置 | 证据 |
|---|---|---|
| `EvalStage` / `run_eval` / `run_data_quality_eval` | `stages/eval.py` 全文 | grep 零外部调用；代码引用 SQLite API（`AssetCoreDB(path)` + `db.open()`），与当前 PG 版不兼容，即便调用也立即崩 |
| `PublishingStage` 类 | `publishing.py:20-26` | 只有定义，execute 直接 return，无人 import（实际用的是函数 `assemble_build` 等）|
| `ParserStage` 类 | `parse.py:23-40` | streaming 用 `create_parser` 函数，不走类包装 |
| `conninfo_from_env` | `pg_config.py:59` | 有定义无调用 |
| `conninfo_from_url` 的 `jdbc:` 分支 | `pg_config.py:25-27` | Python 侧收不到 jdbc URL（那是 Java 侧的），防御性 |

### 5.2 同构分裂

| 项 | 位置 | 说明 |
|---|---|---|
| Python 依赖三份 | `pyproject.toml` / `docker/Dockerfile` / `knowledge_mining/requirements.txt` | 已漂移：pyproject 缺 `jieba`/`python-multipart`/`psycopg-pool`/`python-docx`；requirements 缺 `jsonschema`/`jinja2`/`aiosqlite`/`fastmcp`。`pip install -e .` 跑 uploads 路由会 ImportError |
| `.env` 解析三份 | `reset_db.py:24-49` / `export_db.py` / `import_db.py` 各手抄一遍 | 简单 KV 解析，无共享 |
| 表顺序两份 | `db_tables.py:EXPORT_TABLES`（父表先）/ `reset_db.py:ALL_TABLES`（子表先）| 方向相反，db_tables.py docstring 自称被 reset_db 用，实际没用 |

---

## 6. 已知问题与坑

| 项 | 位置 | 后果 |
|---|---|---|
| `ontology_induction` 静默 no-op | `ontology_induction/__init__.py:197` | 本体类型永不自动归纳，Gate1 无 node_type 候选 |
| `submit_task` 取不到模板只 warning | `llm_client.py:90` | seg_tasks 空，poll_all 返回空，阶段静默无产出 |
| `entity_relations` 不在 streaming | `run.py:1018` | 容易误读为漏挂；实际是设计意图 |
| `graph_write` 受 `has_ontology` 守卫 | `run.py:835` | 4 pack 默认未引种本体 → 多数情况本体线被跳过 |
| contextualizer 在 cloud_core_network 关闭 | `run.py:671` | retrieval_policy `contextual_retrieval: "off"` → 不实例化，raw_text 只用免费 structural breadcrumb |
| `eval.py` 死代码引用 SQLite API | `eval.py:76-77,302-303` | 即便接入主链也会崩 |
| 空 build 允许 | `withdrawal.py:136` | 撤回最后一份文档留空 active release（设计性，前端能区分「0 文档 release」与「无 release」）|

---

## 三色清单

### 🟢 工作正常
- parse / segment（纯算法，无外部依赖）
- enrich（3 pack 声明 segment-understanding）
- discourse relations（3 pack 声明 discourse-relation）
- retrieval_units / question gen（3 pack 声明 question-gen）
- embedding（调 llm_service，无模板依赖）
- db_write / assemble_build / publish_release（无 LLM 依赖）
- withdrawal 软撤回（domain lock + 增量 build）
- document lifecycle NEW/SKIP/RESTORE/UPDATE（hash 驱动，幂等）
- run progress / cancel / resume / trace（运行态追踪齐全）
- **entity_extract**（3 个非 generic pack 下有 compat 兜底合成模板，正常工作）

### 🟡 部分工作 / 降级
- resolve：能跑但依赖 entity_extract 先产出；entity_extract no-op 时跟着空跑
- entity_relations：仅 `_finalize_graph` 间接调用，要求 mention 表有已确认项
- graph_write：仅 `has_ontology=True` 时跑；多数域跳过
- contextualizer：cloud_core_network policy 关了，不实例化
- per-domain 分库：代码已通，但 registry 故意把 4 域指同一物理库 coremasterkb

### 🔴 静默失效 / 死代码
- `ontology_induction` 模板 `mining-ontology-induction`：4 pack 全未声明，**真无 fallback**，本体类型归纳全程 no-op
- `generic` pack 全量降级：无 `llm_templates` 段，所有 LLM 阶段失效
- `stages/eval.py` 全文死代码（引用 SQLite API）
- `PublishingStage` / `ParserStage` 类、`conninfo_from_env` 死代码
- 依赖三份分裂、`.env` 解析三份分裂、表顺序两份分裂
