# 现状：本体系统（ontology）—— 端到端

> 本体跨挖掘(Python)和检索(Java)两侧，是议题三「本体未来形态 = LLM-wiki」的核心。逐行核对源码，论断标 `(file:line)`。
>
**一句话现状**：本体是**设计完整、流程已通、但被两个具体故障点压成「半运转」**的系统——挖掘侧实体/关系抽取链路打通、本体版本治理 + 两道人审 Gate 完整、草稿编辑器 + 候选评审 API 就位；但**类型归纳阶段（ontology_induction）因 LLM 模板未注册而静默 no-op**，且**实体图谱检索在内置 `/api/v1/search` 的特定路由配置下恒返回空**（范式路径正常）。即「挖掘能产实体但不能自动扩类型，检索有完整算子但内置主入口部分失效」。

> 订正：CLAUDE.md 说 `mining-entity-extraction` 也 no-op，**不准确**——它有 compat 兜底（`llm_templates.py:124-130`）在 3 个非 generic pack 下正常工作。只有 `mining-ontology-induction` 真无 fallback。

---

## A. 本体数据库 schema

权威 DDL：`databases/ontology/schemas/001_ontology_concept_postgresql.sql`（1-192 行）。6 张表 + 1 个 ALTER，按注释分 5 组。

### A.1 表清单（TBox 规则层 / ABox 事实层 / provenance / 候选）

| 表 | 角色 | 关键列 | 备注 |
|---|---|---|---|
| `ontology_versions` | TBox 版本快照 | `status ∈ {draft,active,superseded}`、`source`、`UNIQUE(domain_id,version_no)` | **抽取只认 active**；被下面 4 表 FK |
| `ontology_node_types` | TBox 点类型 | `name`、`layer='concept'`、`is_strong`、`definition`、`examples_json` | 如 `network_element` |
| `ontology_relation_types` | TBox 边类型 | `name`、`is_directed`、`inverse_name`、**`allowed_pairs_json`** | 约束合法端点类型对 |
| `ontology_entities` | ABox canonical 实体 | `canonical_name`、`node_type`、`aliases_json`、`mention_count`、`document_count` | `UNIQUE(domain_id,node_type,canonical_name)`，一个真实对象一条 |
| `ontology_entity_relations` | ABox 事实边 | `relation_type`、`confidence`(默认 0.7)、`source_refs_json`、`has_conflict` | **出处强制非空**：`CHECK jsonb_array_length(source_refs_json)>0`、`CHECK head<>tail` |
| `ontology_alias_dictionary` | 消歧别名词典 | `alias_normalized`、`canonical_name` | `UNIQUE(domain_id,alias_normalized)` |
| `ontology_evidence_nodes` | provenance 可追溯 | `target_kind ∈ {entity,relation,mention}`、`target_id`、`segment_id`、`quote` | FK→`asset_document_snapshots` + `asset_raw_segments`（CASCADE）|
| `asset_segment_entity_mentions` | 文章级 mention | `mention_text`、`canonical_name`、`resolved_entity_id`、`resolve_status ∈ {auto,human,pending,rejected}` | FK→asset_* + `ontology_entities`(SET NULL) |
| `ontology_candidates` | 进化轨候选 | `kind ∈ {node_type,relation_type}`、`status ∈ {proposed,queued,accepted,rejected}`、`score`、`payload_json`、`evidence_json` | 独立无 FK，逃生口 |
| `ALTER mining_runs` | 断点续跑脚手架 | 加 `subloop_stage`、`ontology_version_id` | 两道 Gate 之间停住用 |

**核心不变量**（DDL 28-30 行部分唯一索引）：
```sql
CREATE UNIQUE INDEX uq_ontology_versions_domain_active
    ON ontology_versions(domain_id) WHERE status = 'active';
```
"一域至多一个 active"由 DB 保证——`activate_version()`（`ontology_store.py:106-119`）先 supersede 旧 active 再 activate 新版。

**跨库 FK**：`ontology_evidence_nodes` 和 `asset_segment_entity_mentions` FK 指向 asset_core 的 `asset_document_snapshots` / `asset_raw_segments`——这是 ontology DDL 必须最后建、物理合库不可逆的原因。

### A.2 关系模型小结

- **节点**：`ontology_entities`（事实）+ `ontology_node_types`（规则，含 `allowed_pairs_json` 约束）
- **边**：`ontology_entity_relations`（事实，出处强约束）+ `ontology_relation_types`（规则）
- **出处**：`ontology_evidence_nodes`，三态 target_kind 把实体/边/mention 都挂回原文段落
- **版本**：规则层挂 `ontology_versions`，active 唯一；事实层挂 `ontology_version_id` 但 SET NULL（保留事实）
- **候选**：`ontology_candidates` 独立无 FK，是「逃生口」——off-schema 实体/关系先进这里，人审通过升版时并入新 active

---

## B. 挖掘侧：本体如何被构建

### B.1 完整构建流程（已通的部分）

入口：`jobs/run.py:1012-1099`（`_run_pipeline`）。本体线**整体被 `has_ontology` 守卫**（line 1013/1059/1070），未引种本体的域**完全跳过**。

**逐文档流水线**（`run.py:1018-1029`）：parse → segment → enrich → **entity_extract**（仅 has_ontology）→ **resolve**（仅 has_ontology）→ discourse → retrieval_units → embedding → db_write

**全局阶段**（pipeline 跑完后）：

| 阶段 | 函数 | 产出 |
|---|---|---|
| **全局A** graph_write | `_run_graph_write`（`run.py:716-758`）| 落 canonical 实体 + mention + evidence + off-schema 关系候选，**不建事实边** |
| Gate2 人审 | `_pause("entity_review")`（`run.py:1091`）| 暂停，等人确认 pending mention（merge/new/reject）|
| **N3 归纳** ontology_induction | `_run_induction`（`run.py:427-463`）| 从人确认的 `__untyped__` 实体归纳 node_type 候选 |
| Gate1 人审 | `_pause("ontology_review")`（`run.py:1096`）| 暂停，等人审候选类型 |
| **N5 终态建边** finalize_graph | `_finalize_graph`（`run.py:466-515`）| 回贴类型 → 重聚合 mention → 落事实边 + 重算计数 |

**Gate 顺序反转**（`test_review_gate.py:23-42` 锁住）：实体确认（Gate2）先于本体确认（Gate1）——先把 `__untyped__` 实体确认干净，再交给 LLM 归纳类型。

### B.2 每步的 LLM prompt 与产出

| 步骤 | LLM 模板 | 产出 |
|---|---|---|
| **entity_extract**（`stages/entity_extract/__init__.py`）| `mining-entity-extraction`（有 compat 兜底，正常跑）| 双通道：A 类型合法且置信≥0.5→typed ref；B/低置信/类型外→`__untyped__` ref（带 proposed_type、off_schema_reason）。写回 `seg.entity_refs_json` |
| **resolve**（`stages/resolve/__init__.py`）| 不走 LLM | 别名词典查表（Tier1）。命中→`auto`；未命中→`pending`（触发 Gate2）。Tier2 向量默认关，Tier3 LLM 不做 |
| **entity_relations**（`stages/entity_relations/__init__.py`）| 不走 LLM | 纯 pattern 匹配（active 本体的 `relation_types.allowed_pairs_json` 当类型闸）。同段实体有序两两配对，命中进 candidate_relations，没命中进 relation_candidates（逃生口）|
| **relations**（`stages/relations/__init__.py`）⚠️ **独立线** | `mining-discourse-relation` | **篇章 RST 关系**（不是本体关系！），落 `asset_raw_segment_relations`，**不进 ontology_* 任何表**。15 种 RST 关系定义在 `contracts/rst_relations.py`（10 标准 + 5 扩展）|
| **graph_write**（`stages/graph_write/__init__.py`）| 不走 LLM | 跨文档聚合，算 NPMI（默认阈值 0.3，DF≥2），落 canonical 实体+mention+evidence+off-schema 候选，**不建边**；终态时仅落过闸边，出处强约束（拿不到 evidence 不落边）|
| **ontology_induction**（`stages/ontology_induction/__init__.py`）| `mining-ontology-induction` ⚠️ **无 fallback** | **静默 no-op**（见 B.3）|

> **本体关系 vs RST 关系**：本体关系=实体↔实体（领域事实，`ontology_entity_relations`）；RST 关系=段落↔段落（修辞，`asset_raw_segment_relations`）。两套表不重叠。检索侧 `graph_expand` 走 RST，`entity_graph` 走本体。

### B.3 CLAUDE.md 论断核实（重点）

#### `mining-entity-extraction`（CLAUDE.md 说过时）

4 pack 的 domain.yaml 没显式声明它，**但** `infra/llm_templates.py:67-132` 的 `build_templates_from_profile` **自动从 `mining-segment-understanding` 残留的 `entities` schema 字段合成**它（line 80/103-106/124-130，注释「compat-1 兼容旧 Domain Pack」）。3 个非 generic pack 的 schema 里有 `entities` 字段，兜底**会触发**。测试 `test_v14_domain_pack.py:185-194` 与 `test_pipeline_operators.py:491-498` 锁住。**结论**：3 个非 generic pack 下 entity_extract 正常工作。

#### `mining-ontology-induction`（CLAUDE.md 正确）

grep 全仓库，**既无 pack 声明，也无代码 fallback，也无注册**。`OntologyInductor.induce()`（`ontology_induction/__init__.py:174-228`）的 `submit_task(template_key="mining-ontology-induction")` 取不到模板返回 None（`llm_client.py:51-92`，失败仅 warning），induce 立即返回 0 候选（line 207-208）。

**后果链**：node_type 永远不会被自动提议 → Gate1 永远没有 `proposed` 状态候选 → `_has_proposed_candidates` 永远 False → 永远不会 `_pause("ontology_review")`。但 entity_review Gate 仍在用（entity_extract 双通道 `__untyped__` mention 真会落）；off-schema 关系候选正常进 `ontology_candidates`，所以 Gate1 的关系类型候选可能有内容，只是 node_type 候选永远空。

#### `ontology_bootstrap.py` 不绕模板

走完全不同的路径——**人工上传 YAML 文件**冷启动引种本体 v1（`ontology_bootstrap.py:90-136`），不调 LLM，直接 `add_node_type`/`add_relation_type`/`upsert_alias` 灌进 active 版本。bootstrap 提供种子，induction 本应负责后续扩展但当前 no-op。

### B.4 人审门设计意图（3 个测试文件确认）

- **Gate2 entity_review**：pending mention 必须人审。`GraphStore.resolve_mention` 三路 merge/new/reject（`ontology_store.py:923-984`）。测试 `test_review_gate.py:253-298`。
- **Gate1 ontology_review**：候选必须人审。`OntologyStore.review_candidate` accept/reject（`ontology_store.py:391-422`），已裁决候选幂等保护（line 404-410）。`promote_accepted_candidates`（line 424-480）克隆旧版类型+追加 accepted 候选+激活新版。
- **草稿编辑器**：人可直接编辑本体，`replace_draft`（line 224-262）整份覆盖、`publish_draft`（line 264-273）激活。测试 `test_ontology_draft.py`。

**设计意图**：本体是慢变、强约束的领域规则，**机器只提议、人审拍板**。Gate2 在前（先把实体确认干净）→ N3 归纳 → Gate1（再确认类型）→ N5 回贴 + 终态建边。

### B.5 实体/概念/关系数据模型

- **实体**（concept/entity）：canonical 对象，`ontology_entities`，一个真实对象一条，`(domain,node_type,canonical_name)` 唯一。属性 `aliases_json`/`attributes_json`/`mention_count`/`document_count`。
- **节点类型**：`ontology_node_types`，cloud_core_network 域 8 类（network_element/interface/command/protocol/alarm/feature/parameter/concept，`domain.yaml:14-22`）。MVP 全 `layer='concept'`。
- **本体关系**：`ontology_entity_relations` + `ontology_relation_types`。**关系谓词类型由 pack 定义、人审锁定**，如 `connects_to`(head=network_element, tail=interface)。`allowed_pairs_json` 约束合法端点类型对。

---

## C. 检索侧：本体如何被消费

### C.1 三个本体相关算子

| 算子 | 走哪张表 | 干什么 |
|---|---|---|
| `entity_exact` | `asset_retrieval_units`（JSONB @> entity refs）| 实体名字面匹配，**不查本体图** |
| `entity_graph` | `ontology_entities` / `ontology_entity_relations` / `ontology_evidence_nodes` / `ontology_alias_dictionary` + `asset_retrieval_units` | 实体链接→多跳图遍历→回证据段→召回检索单元 |
| `graph_expand` | `asset_raw_segment_relations` + `asset_raw_segments` | **段落间** RST 关系图 BFS（与本体无关）|

### C.2 entity_graph 完整流程（`EntityGraphRetriever.java:54-96`）

1. **收集实体名**（collectNames）：从 `understanding.entities()` 取 name+normalizedName，入 LinkedHashSet；空则退化用 keywords 兜底。
2. **实体链接**（linkEntities，`OntologyGraphMapper.xml:25-40`）：SQL 同时走 `lower(canonical_name) IN (...)` 和别名子查询；DISTINCT 去重返回 seedIds。空则整体返回空。
3. **图遍历+回证+回表**（graphRecall，`OntologyGraphMapper.xml:50-101`）：**单条递归 CTE** 完成三件事：
   - `nb` CTE：从 seedIds 沿 `ontology_entity_relations` 双向多跳扩展，记 hop、`conf *= edge.confidence`（连乘）、path[] 防环、`r.confidence >= minRelConf` 剪枝、`hop < maxHop` 限深。
   - `nb_agg` CTE：每实体收敛 MIN(hop)/MAX(conf)。
   - 主查询：JOIN `ontology_evidence_nodes`(target_kind='entity') 回 segment_id → JOIN `asset_retrieval_units`（按 source_segment_id 对齐 + 按 snapshotIds 过滤）→ GROUP BY ru.id → ORDER BY hop ASC, conf DESC LIMIT topK。
4. **打分**：`score = BASE(0.85) × decay(0.6)^hop × conf × entityCardBoost(1.1)`。

注释（`OntologyGraphMapper.java:13-16`）明确："ontology 表与 asset_retrieval_units 共址于按域路由的同一个 asset 库"，整条召回一次 JOIN 完成，无跨库。

### C.3 entity_graph 当前能否工作？

> **结论：范式路径可用，`/api/v1/search` 旧路由的特定配置下恒返回空。**

**范式路径（可用）**：`EntityGraphOperator.execute`（`EntityGraphOperator.java:52-65`）从 `ExecContext.domain()` 取 domain，不读 ThreadLocal。调用链 Operator→Retriever→Mapper 全程同线程。ParadigmExecutor 显式 `DomainContext.set`（`ParadigmExecutor.java:139-140`）。用 `entity_graph_test.json` / `hybrid_with_entity_graph.json` 范式能正常工作。

**`/api/v1/search` 旧路由（部分恒空）**：`EntityGraphRouteRetriever.retrieve`（line 42-50）第一行读 `DomainContext.get()`，null/blank 返回空。SearchService 在虚拟线程派发变体/子查询检索（line 294/323）**没 `DomainContext.set`/`wrapRunnable`**，虚拟线程不继承 ThreadLocal。但 RetrievalOrchestrator 的 parallel 分支用 `wrapRunnable` 兜底（`RetrievalOrchestrator.java:178`），sequential 分支（≤1 route）裸跑——**触发条件：routePolicy 把 entity_graph 配进 ≤2 路方案**。默认 medium 路由表根本不含 entity_graph，默认 complex 4 路 parallel 分支由 wrapRunnable 兜底，不触发。

CLAUDE.md「entity_graph 路由在 /api/v1/search 中恒返回空」**方向正确，但范围需收窄**：仅在 routePolicy 把 entity_graph 配进 ≤2 路方案时恒空，默认路由下不触发。

---

## D. 现状综合判断

### D.1 成熟度分层

| 层 | 状态 |
|---|---|
| DDL / Schema | ✅ 完整、生产级（出处强约束、版本治理、部分唯一索引、FK 完备）|
| 挖掘侧实体抽取 + 双通道逃生口 | ✅ 跑通（compat 兜底，CLAUDE.md 这条过时）|
| 挖掘侧实体消歧 | ✅ 跑通（仅 Tier1 别名词典，人审兜底）|
| 挖掘侧关系候选（pattern 约束）| ✅ 跑通（仅在有 active 本体时）|
| Gate2 实体确认 + 草稿编辑器 + 升版 | ✅ 完整、有测试 |
| 本体图谱检索算子（范式路径）| ✅ 完整、文档准确 |
| Gate2/Gate1 REST API（12 端点）| ✅ 完整 |
| **N3 类型归纳（ontology_induction）** | ❌ **静默 no-op**（`mining-ontology-induction` 真无 fallback）|
| **`/api/v1/search` 旧路由的 entity_graph** | ❌ 特定路由配置下恒空（DomainContext 漏设）|

### D.2 一句话总结

**挖掘侧能自动产实体但不能自动扩类型、检索侧有完整算子但内置主入口部分失效**——schema、版本治理、出处强约束、两道人审 Gate、候选评审 API、检索算子与递归 CTE 都已生产级就位；但**类型自动归纳这条关键自动化路径被未注册的 `mining-ontology-induction` 模板切断**；同时内置检索的 entity_graph 在特定路由配置下因虚拟线程不继承 ThreadLocal 恒空。

---

## E. 走向「LLM-wiki 自动编写本体」的现有基础与缺口（议题三铺垫）

用户的判断：未来本体构建很可能是「大模型基于已有所有相关知识进行 wiki 编写 + 边链接构建」。外部参照：**Stanford STORM**（arXiv 2402.14207，LLM 研究→大纲→带引用的 Wikipedia 式长文，已开源）、**ontology-grounded KG construction**（本体作为 schema 约束 LLM 抽取）、entity linking to Wikidata 等方向。

### E.1 已有（可直接复用）

- ✅ **版本治理 + active 唯一约束**（`ontology_versions`）：天然支持「生成 draft → 人审 → publish」工作流
- ✅ **出处强约束**（`ontology_evidence_nodes` + DB CHECK）：每个实体/关系必带原文出处，wiki 风格的「引用」已就位
- ✅ **草稿编辑器 + 候选评审 API**（`routes/ontology.py` 的 `/ontology/draft` / `/ontology/candidates/{id}/review` / `/ontology/promote`）：就是 wiki 编辑的后端骨架
- ✅ **双通道逃生口设计**：LLM 自报「清单外重要概念」通过 `__untyped__` → 人审 → 归纳升类型，正是 wiki 自动编写需要的「边写边发现新条目」机制
- ✅ **本体种子 YAML 引种**（`ontology_bootstrap.py`）：人工冷启动入口已有
- ✅ **NPMI 关系强度过滤 + scoped_recompute**：自动提议的关系有质量闸
- ✅ **entity_graph 检索算子**：本体一旦写好立刻可被检索消费，闭环已通

### E.2 缺（走向 LLM-wiki 必须补的）

- ❌ **`mining-ontology-induction` 模板和 prompt**：核心阻塞点。当前归纳完全 no-op。建议结构：输入「已知类型 + 一批文档级实体清单 + 各实体的原文摘录」，输出「建议的 type_name/definition/examples/members」
- ❌ **跨文档全量知识聚合能力**：当前归纳输入是「已确认的 `__untyped__` 实体」，范围太窄。LLM-wiki 需要的是「给定全部文档与已有本体，写一段 wiki 条目 + 抽出它引用的所有链接」——这是一个**全新的全局阶段**，目前不存在
- ❌ **「边链接构建」的运行时机制**：用户设想的「边写边链」——LLM 写 wiki 条目时主动给已有 canonical 实体建链——目前完全没有对应算子。最近似的 `entity_relations` 是单段两两配对的 pattern 闸，做不了「基于整篇 wiki 内容的全局建链」
- ❌ **wiki 条目载体表**：现有 `ontology_entities.definition` 只是一句话，没有「长 wiki 文章 + 章节 + 引用列表」这种结构。要么扩 `ontology_entities`，要么新建 `ontology_wiki_articles` 表
- ❌ **生成-人审-发布的更软工作流**：当前两道 Gate 是阻塞式暂停，LLM-wiki 风格更接近「持续生成 → 评审队列 → 增量发布」，需要把 Gate 从硬阻塞改成事件流
- ⚠️ **`/api/v1/search` 的 DomainContext 虚拟线程 bug**：与 wiki 无关，但同样阻塞「生成的本体能在主入口被检索验证」，建议先修

### E.3 简短评估

架构层面（版本治理、出处强约束、双通道逃生口、人审 API、草稿编辑器）**非常贴合** wiki 风格的本体演进范式，是一个好的起点；但**核心自动化（归纳模板、跨文档聚合、边链接构建）都尚未落地**，离「LLM 基于全部知识写 wiki + 自动建链」还有约 60% 的工作量，主要在 4 块：**prompt 工程 + 新的全局阶段 + wiki 载体表 + 软评审工作流**。详见研讨大纲议题三。

---

## 关键文件索引

**Schema 与挖掘侧核心**：
- `databases/ontology/schemas/001_ontology_concept_postgresql.sql`
- `knowledge_mining/mining/stages/{entity_extract,ontology_induction,entity_relations,relations,resolve,graph_write}/__init__.py`
- `knowledge_mining/mining/infra/{ontology_store,ontology_bootstrap,llm_templates,llm_client}.py`
- `knowledge_mining/mining/contracts/rst_relations.py`
- `knowledge_mining/mining/api/routes/ontology.py`
- `knowledge_mining/mining/jobs/run.py`（line 625-627 模板注册 / 716-758 global-A / 1012-1099 编排 / 427-463 induction no-op）

**检索侧**：
- `agent_serving_java/src/main/java/.../operator/operators/retrieve/{EntityGraphOperator,GraphExpandOperator,EntityExactOperator}.java`
- `agent_serving_java/src/main/java/.../retrieval/{EntityGraphRetriever,EntityGraphRouteRetriever}.java`
- `agent_serving_java/src/main/resources/mapper/OntologyGraphMapper.xml`
- `agent_serving_java/src/main/resources/paradigm/examples/{entity_graph_test,hybrid_with_entity_graph}.json`
- `agent_serving_java/docs/ontology-retrieval-explained.md`（✅ 准确）

**Scenario pack（验证模板缺失）**：`main_control_service/config/scenario_packs/{cloud_core_network,generic,civil_engineering,odn}/domain.yaml`
