# Wiki 制品批次3：编译管线 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** wiki 制品获得主通道——绑定知识库目录 → 后台编译（聚类→目录 LLM→人工确认→簇级写作→链接/矛盾/缺口→落 rev0）→ 脏标记观测 + 手动重编译 + origin 保护。

**Architecture:** 绑定管理走 `kb_artifact_sources`（sources CRUD + scope/fingerprint JSONB 契约定稿）；编译任务 = `kb_artifact_batches` 一行（channel=wiki_compile，**状态机存 details JSONB**——表无 status 列，DDL 零改动）+ mining 后台 asyncio 任务（照批次2 embedding 后置的强引用集模式）；取材 SQL 照抄库侧 `_CURRENT_SNAPSHOT_CTE` 口径（只读 L1 产出，不走挖掘管线）；LLM 全走 `LLMServiceAsyncGenerationClient`（任务通道+幂等键，重编译同语料命中缓存=重跑稳定性）；聚类纯代码独立模块；写作产物经 WikiDB 落 rev0（origin='machine'，绕过 WikiService 人工入口的 origin 恒 manual 规则）。

**Tech Stack:** FastAPI + psycopg + PostgreSQL(pgvector) + llm_service 任务通道 + Vue3/Element Plus。**零 DDL、零新依赖。**

**设计依据:** 63号 §四全节（4.1 复用资产/4.2 六步/4.3 页面格式/4.4 引用规范/4.5 质量账/4.6 工程底座）+ §3.2 origin 保护 + 批次2完成记录接手清单（sources 零代码/create_batch 未放行/Lifecycle 已挂投影勿另起炉灶）。

---

## 本批次定稿的实现决策

1. **编译状态机入 batches.details**（表无 status 列，不迁移）：`details = {"state","progress","toc","clusters_digest","stats","conflicts","error","source_fingerprint","prompt_version"}`；state ∈ `preparing → awaiting_toc → writing → completed | failed | cancelled`。**details 写入口径（并发铁律）**：一律 `UPDATE kb_artifact_batches SET details = details || %(patch)s::jsonb WHERE batch_id=%s` **增量合并**，禁全量覆盖；后台任务是 writing 阶段 progress/stats 的唯一写者，cancel 只写 state 键；完结/failed 的 state 写入带守卫 `AND details->>'state' IN ('preparing','writing')`（被 cancel 抢先则不覆写）。前端轮询 status 端点读最新 batch。row_count 列在 completed 时填页数。**start 串行化**：查在飞+建批放进单事务 `SELECT pg_advisory_xact_lock(hashtext(%(artifact_id)s))`（零 DDL、多 worker 安全）；模块级 `_COMPILE_TASKS` 强引用集（照 _EMBED_TASKS 模式）。**cancel 对 awaiting_toc 也生效**（该态无后台任务，直接改 state；在飞 writing 在批边界检查停止位后落 cancelled）。
2. **取材量级三重护栏（防 OOM）**：①start 时先算 Σ unit_count（指纹查询已有该数据），**embedded 单元总量 > 30000 → 422 too_many_units**（错误码入表；提示收窄目录范围）——已知内网存在 40 万向量库，无护栏绑全库必 OOM；②向量解码用 `array('f')`（stdlib，依赖冻结内）≈4KB/条而非 list[float] 的 ~32KB/条（CPython 1024×24B float 对象+指针），聚类点积同模块循环；③units 读取按 snapshot 分批（fetchmany 2000）流式聚合。确定性：units 按 (snapshot_id, ordinal, representation_id) 排序、聚类种子取首遇——同语料重跑结果稳定（验收②）。
3. **目录生成单次 LLM 调用，输入有界**：每簇先算启发式摘要（前 3 个单元首 200 字 + 高频词）；**TOC 输入取按成员数 top-150 簇，其余簇折叠为一行统计（"另有 X 个小簇"）**；**TOC prompt 硬约束产出 ≤200 页**（页 cap 前移到目录生成）；confirm_toc 展开页数 >200 → 422 too_many_pages（用户删减后再确认，杜绝"确认了却静默截断"）；写作阶段保留 200 硬 cap 作防御性截断。写作每页一次 LLM 调用（25 页/批批间约束注入）。
4. **幂等键即重跑稳定器**：所有生成调用幂等键 = `_generation_key` **四参口径 `(kind, prompt_version, expected_output_type, payload)`**（TOC/质量调用 type=json_object、页写作 type=text——键含 type 防串缓存）——同语料重编译命中 llm_service 任务缓存，输出逐字一致。
5. **origin 冲突账**：重编译时 rev0 既有 manual 页不碰；机器页键与 manual 页撞 → 跳过该机器页 + `details.conflicts` 记录 `{page_key, manual_title, machine_title}`（人裁决=手工删/改其一）。
6. **JSONB 契约定稿（批次1遗留缺口③）**：`sources.scope = {"directories": ["a/b", ...]}`（空数组=全库）；`sources.fingerprint = {"snapshots":[{"snapshot_id","unit_count"}], "computed_at"}`；`batches.details`（wiki_compile）= 决策1形状。**页 provenance 多源形状**：`[{kb_id, snapshot_id, unit_ids}]` **按 (kb_id,snapshot_id) 分组的多条目**（簇可横跨多 KB；representation_id 全局 PK 归属无歧义）。
7. **引用校验双时点（对设计 §4.4 的落地）**：编译页后置校验 machine 页（provenance 非空/悬空/密度+draft 宽免）之外，**WikiLifecycle.publish 发布前对 rev0 全部页再跑同款确定性校验**（校验函数由 wiki_compile_service 导出、publish 复用）：machine 页 provenance 空 → 阻断；悬空引用 → 阻断；manual 页 provenance 空 → flags 标记+发布响应携带 warning（**记录偏差**：设计"硬性"对人工页降为警告——批次2 编辑器无 provenance 录入 UI，硬阻断会封死人工页发布路径；后续补录入 UI 后可收紧）。**MVP 不做"凝聚度低重聚"**（§4.2 ② 增量指派+拆+合已覆盖主逻辑，记录偏差）。

## 铁律（同批次2，全文适用）

依赖冻结（pyproject/pom/package.json 不动）；git add 只写显式路径；永不提交 llm_service.yaml；**表格制品路径与批次2已定契约零破坏**（REST 信封/hex 路径/错误码表/幂等键格式）；注释中文；git 全路径 `"/c/Program Files/Git/cmd/git.exe"`。测试命令同批次2（PY docker a74b940 + kb 套件 `--continue-on-collection-errors` 差分零新增失败；前端 npm test + npm run build）。真库用例写而不跑（py≥3.12 才能收集，见批次2完成记录④）。

---

## Task 1: 来源绑定管理 + create_batch 放行

**Files:**
- Create: `knowledge_mining/mining/kb/db_compile_inputs.py`（本任务先只放 sources/指纹查询；Task 2 加取材）
- Modify: `knowledge_mining/mining/kb/services/wiki_service.py`（WikiService 加 sources 方法）或新建 `services/wiki_sources_service.py`——**裁定：并入 WikiService**（同域薄方法，避免第四个服务文件）
- Modify: `knowledge_mining/mining/kb/routes/wiki.py`（三端点）、`knowledge_mining/mining/kb/db_artifacts.py`（create_batch 放行 wiki_compile——grep `def create_batch` 定位，channel 白名单元组加 `"wiki_compile"`，file_name/sheet_name 传 None）
- Test: `knowledge_mining/tests/kb/test_wiki_sources.py`（无 PG：service+routes）；`test_wiki_db.py` 追加 PG 用例（sources 往返+指纹）

**内容：**
- `CompileInputsDB`（pool）：`list_sources(artifact_id)`；`upsert_source(artifact_id, kb_id, scope, created_by)`（ON CONFLICT (artifact_id,kb_id) DO UPDATE scope）；`delete_source(artifact_id, kb_id)`；`update_fingerprint(artifact_id, kb_id, fingerprint)`；`current_fingerprint(kb_id)`——按 `_CURRENT_SNAPSHOT_CTE`（kb/db.py:1537-1552 同构，单 kb 标量版）取 active 快照集 + 每快照 `COUNT(asset_retrieval_units_v2)`（该查询同时是 start 的量级护栏数据源）；`update_batch_details(batch_id, patch)`（决策1 增量合并 SQL）；`latest_compile_batch(artifact_id)`（最新 wiki_compile 行含 details）。**kb 可读判定**：从 kb/db.py L1359-1430 的 list/read 查询里**抽内联可见性谓词为单函数**（谓词照抄该 SQL 原样——public OR 成员 OR site admin 分支一个不少，防第三次判定不对称坑）；不可读一律 404 反探测。PG 用例（写而不跑）钉死 admin/public/成员/无关者四象限
- WikiService 方法：`list_sources(user, artifact_id)`（ROLE_RUNTIME_READ；每行带 `dirty` = fingerprint 与 current_fingerprint 不一致）；`upsert_source(user, artifact_id, kb_id, directories)`（ROLE_DESIGN + expected_updated_at 乐观锁（touch kb_artifacts）+ kb 可读校验 404/403）；`delete_source(...)`（同）
- 路由：`GET /{artifact_id}/wiki/sources`、`PUT /{artifact_id}/wiki/sources`（body {kb_id, directories: list[str], expected_updated_at}）、`DELETE /{artifact_id}/wiki/sources/{kb_id}`（锁令牌随 body）。错误码入 wiki 码表（source_not_found→404、invalid_scope→422 等）
- `create_batch` 放行：`("upload","kb_import")` → 加 `"wiki_compile"`（唯一既有文件改动豁免点；sheet_name/file_name 对 wiki_compile 传 None）

## Task 2: 取材与聚类（纯代码核心）

**Files:**
- Create: `knowledge_mining/mining/kb/wiki_clustering.py`（纯函数模块）
- Modify: `knowledge_mining/mining/kb/db_compile_inputs.py`（加 `collect_units(kb_id, directories)`）
- Test: `knowledge_mining/tests/kb/test_wiki_clustering.py`（纯函数，无 PG，本地红绿）；`test_wiki_db.py` 追加 PG 用例（collect_units SQL 形态）

**collect_units SQL（摸底已给可照抄形态，含要点）**：`_CURRENT_SNAPSHOT_CTE` 单 kb 版 → `asset_retrieval_units_v2 u JOIN cur` → LATERAL 取 directory_path（asset_document_snapshot_links⋈asset_documents）→ `LEFT JOIN asset_retrieval_embeddings_v2 e ON representation_id+snapshot_id+dimension=1024`（LEFT 保住无向量单元）→ 目录过滤（`=ANY(dirs) OR strpos(directory_path, dp||'/')=1`；空数组=全库）→ ORDER BY snapshot_id, ordinal, representation_id。返回 `[{representation_id, snapshot_id, content_text, structural_context, section_ref, ordinal, dense_eligible, directory_path, embedding(array('f')|None)}]`；`embedding_vector_vec::text` 解码为 **`array('f', map(float, s.strip('[]').split(',')))`**（4KB/条，决策2）。量级护栏：单快照分批（fetchmany 2000）流式聚合。注意：section_ref 列由 015 迁移 ALTER 补（013 建表无），核对以现库为准。

**聚类器（63号 §4.2 ②，纯函数 `cluster_units(items, *, sim_threshold=0.62, merge_threshold=0.85, max_cluster=100, min_promote=3) -> list[Cluster]`）**：
- 输入带 embedding 的 items（无向量的单元不参与聚类、旁路到"未聚类池"）
- 增量指派：逐项与既有簇种子算余弦（归一化后点积，种子=首成员向量；**判定对全簇均值**）≥sim_threshold 入簇；否则新种子
- 3+ 成员转正（<3 的种子簇并入未聚类池，其成员按目录/section_ref 相邻性兜底归入"杂项簇"——每成员仍要有归属以便取材完整）
- >max_cluster 拆：簇内以最远离的两成员为双种子重跑增量指派（递归）
- 簇间中心相似 >merge_threshold 合并（迭代至稳定）
- 确定性：输入序固定→输出序固定；Cluster = {cluster_id: 稳定哈希（成员 representation_ids 排序后 sha256[:12]）, member_ids, centroid, heuristic_label（top 高频词 3 个 + 首单元前 60 字）, digest_members（取材用前 12 个 representation_id）}
- 旁路：`build_cluster_context(cluster, units_lookup, cap=12, per_unit_chars=800)` → 簇上下文文本（供 LLM）

**测试**：合成向量（低维即可测逻辑，如 4 维）——入簇/新种子/转正/拆/合/确定性（同输入两次运行结果相等）/未聚类兜底/成员哈希稳定。

## Task 3: 编译管线主体（CompileService）

**Files:**
- Create: `knowledge_mining/mining/kb/services/wiki_compile_service.py`（状态机+LLM 编排+写作+质量+落库；prompt 常量与 PROMPT_VERSION 随文件）
- Modify: `knowledge_mining/mining/kb/deps.py`（装配）、`routes/wiki.py`（compile 四端点）
- Test: `knowledge_mining/tests/kb/test_wiki_compile.py`（无 PG：Fake LLM/Fake DB 全链）

**CompileService（构造 (wdb, inputs_db, generation_client, wiki_service)**——generation_client 注入 `GenerationClient` 协议形态（LLMServiceAsyncGenerationClient 生产实现；**阻塞 execute 下 asyncio.to_thread**，同批次2 embedding 口径）**）：**

- `start(user, artifact_id, *, expected_updated_at)`：ROLE_DESIGN+新鲜度；校验 ≥1 active source（无绑定 422 no_sources）；**advisory lock 事务内查在飞+建批**（决策1；在飞=最新 batch state ∈ preparing/awaiting_toc/writing → 409 compile_in_progress）；**量级护栏**：Σ各源 current_fingerprint unit_count（embedded 口径）>30000 → 422 too_many_units；`create_batch(channel="wiki_compile", created_by)` → 后台任务（`_COMPILE_TASKS` 强引用集）跑 `_run_prepare(batch_id)`
- `_run_prepare`（async；LLM 调用下 to_thread）：①collect_units 全部绑定源（按 source 逐个取再合并，scope.directories 各自过滤；合并时记每 unit 的 kb_id 归属）②`update_fingerprint` 各源 ③cluster_units ④目录 LLM 一次（输入=**按成员数 top-150 簇**的 heuristic_label+代表句+目录路径分布+折叠统计行；输出 JSON：`{"toc":[{"section_title","page_hints":[{"title","cluster_ids":[...],"page_type":"topic"}...]}...],"glossary":[{"term","definition_hint"}],"open_questions":[{"question","why"}]}`，expected_output_type=json_object，prompt 硬约束**产出 ≤200 页**）⑤details 增量合并 state=awaiting_toc + toc/clusters_digest/stats{unit_count,cluster_count,embedded_ratio} ⑥失败 → 守卫写 state=failed+error
- `confirm_toc(user, artifact_id, toc, *, expected_updated_at)`：ROLE_DESIGN；当前 batch 必须 awaiting_toc（否则 409 invalid_compile_state）；**展开页数 >200 → 422 too_many_pages**；toc 可被前端编辑（增删节/改标题/并拆页提示）；details 增量合并 toc=确认版 + state=writing → 后台 `_run_writing(batch_id)`
- `_run_writing`：对确认 toc 展开的每页（200 硬 cap 防御性截断，stats 记 dropped_pages）：按 25/批；**每页 prompt**（四节 schema 模板：blockquote 摘要/核心事实（每句带 `[[src:representation_id]]`）/相关页面（`[[page_key]]` 用目录内其他页键）/来源节）+ 全局目录上下文 + 该页 cluster 的 unit 原文（build_cluster_context，cap 12×800 字）+ **批间约束注入**（前批产生的断链清单+「无据不写」规则文本拼进 system）；页键=normalize_page_key(title)；幂等键=`_generation_key("wiki-page", WIKI_PAGE_PROMPT_VERSION, "text", payload)`；写 `wdb.upsert_page(origin='machine', provenance=按 (kb_id,snapshot_id) 分组的实际引用条目（决策6），expected_updated_at=None)`（None 跳过比对但**仍 touch**——批次2 已核实该行为：200 页编译会推进乐观锁令牌 200 次，**编译终态后前端必须刷新制品详情**（Task 4 M4））；**origin 保护**：落页前读 rev0 现有页，manual 同键 → 跳过+conflicts 记录；machine 同键 → 覆盖（重编译语义）
- 页后置：全量链接重算（复用 WikiService._recompute_all_links——compile_service 持有 wiki_service 引用）；**引用校验（63号 §4.4，确定性代码非 LLM）**：每 machine 页 provenance 非空（空 → flags.provenance_missing）；行内 `[[src:id]]` 抽出的 id ∈ 取材 unit_id 全集（不在 → flags.dangling_refs 计数）；密度：连续 ≥3 个事实句（`- ` 列表行）无 src 标记 → flags.density_warning（**draft 宽免**：origin='manual' 页跳过）
- **矛盾/缺口页**：写作完成后一次 LLM 质量调用（输入=全部页 title+blockquote 摘要列表，输出 `{"contradictions":[{"page_a","page_b","claim_a","claim_b","severity"}],"gaps":[{"question","context"}]}`）→ 生成 contradiction/open_question 页（frontmatter 严格按 63号 §4.3 专页 schema；open_question 缺口也吸收 prepare 阶段的 open_questions）；glossary 页：目录 LLM 的 glossary 术语表逐条生成（cap 30 页，超出截断记 stats）
- 完结：details.state=completed + stats{pages_written, conflicts, broken_links, dangling_refs, dropped_pages, glossary_pages, contradiction_pages, open_question_pages}；batch row_count=页数；**不自动发布**（人工修订后走既有 publish——设计 §4.2 ⑥"进人工修订"）
- `status(user, artifact_id)`：ROLE_RUNTIME_READ？——**裁定 design**（编译过程含未发布内容，runtime 不可见）：ROLE_DESIGN；返回最新 wiki_compile batch 的 {state, progress(当前批/总批), stats, conflicts, toc(awaiting_toc 时), error}
- `cancel(user, artifact_id)`：在飞状态 → state=cancelled（后台任务下次批边界检查停止位）+ 强引用集合摘除
- 失败语义：LLM 单页失败重试由 llm_service 任务通道承担（max_attempts=3）；整页最终失败 → 跳过该页 + stats.failed_pages 计数（不阻断整本）；prepare/writing 阶段级异常 → state=failed+error，可重新 start（幂等键让已完成页命中缓存）

**路由**：`POST /{artifact_id}/wiki/compile`、`GET /{artifact_id}/wiki/compile/status`、`POST /{artifact_id}/wiki/compile/toc`、`POST /{artifact_id}/wiki/compile/cancel`。新错误码入表（no_sources→422、compile_in_progress→409、invalid_compile_state→409）。

**测试（Fake LLM=可编程 GenerationClient 桩记录 prompt/返回预制 JSON；Fake DB 沿用批次2 Fake 家族）**：状态机全迁移（preparing→awaiting_toc→writing→completed / failed / cancelled）；TOC 确认编辑生效；25/批分批+约束注入断言（第二批 prompt 含第一批断链文本）；origin 保护（manual 撞键跳过+conflicts）；引用校验三件（provenance 空/悬空/密度）+ draft 宽免；矛盾/缺口/术语页生成与 frontmatter 严格性；页 cap 200 截断；重编译 machine 覆盖 manual 保留；状态端点 design-only。

## Task 4: 前端编译面板

**Files:**
- Modify: `kb-ui/src/api/artifacts.ts`（七方法：listWikiSources/upsertWikiSource/deleteWikiSource/startCompile/compileStatus/confirmToc/cancelCompile）+ types
- Create: `kb-ui/src/components/artifacts/WikiCompilePanel.vue`
- Modify: `kb-ui/src/views/artifacts/ArtifactDetailView.vue`（design tab wiki 分支顶部挂 CompilePanel，EditorPanel 其下）
- Test: spec 两处

**WikiCompilePanel.vue（≤400 行）**：
- props {artifactId, canWrite, expectedUpdatedAt}；区块一=来源绑定：列表（kb 名+目录 chips+dirty 徽标"来源已变化"）、添加（**先域下拉再选库**——kb-ui 无 listKnowledgeBases；实际 API 是 `kb-ui/src/api/kb.ts` 的 `useKbApi().listKbs(domain)`（**必须传 domain**），域选择+KB 选择的现成模式照 `src/views/paradigm/ParadigmEditorView.vue` / `McpAccessView.vue` 的调用方式；目录输入 MVP 从简：el-input-tag 手输目录路径数组）、删除；区块二=编译：开始按钮（canWrite）+进度+**目录确认步**（awaiting_toc 时渲染 toc 树：节标题可编辑 input、页提示列表勾选包含/排除、确认/取消按钮）+完成报告（stats 表格+conflicts 列表+broken/dangling 计数）；区块三=脏标记横幅（任一源 dirty → el-alert"来源已变化，建议重新编译"+按钮直达开始编译）
- **轮询生命周期（硬要求）**：onMounted 起 setInterval(5s) 轮询 compileStatus；**onBeforeUnmount clearInterval**；读到终态（completed/failed/cancelled）即停；props.artifactId 变化时重置重启；spec 加"卸载后无在飞请求"断言（fake timers）
- **编译终态刷新令牌（M4 硬要求）**：读到终态后 emit('changed')——DetailView reloadAll 拉新 updated_at，否则编辑器（持有旧乐观锁令牌）编译后首次保存必 409
- 取消按钮（在飞时，awaiting_toc 也显示）；409/422 错误处理沿用批次2 模式

**DetailView**：wiki design 分支 = `<WikiCompilePanel ... @changed="reloadAll" />` + `<WikiEditorPanel ... />` 纵向排列（始终展开，不加折叠）。spec 更新（wiki design 断言两面板均渲染）。

## Task 5: 批次门禁 + 完成记录

- Python 全量差分（merge-base 24c43d4 test-ID 级零新增失败）；前端 npm test+build；MCP/Java 不动（本批次零 Java/MCP 改动，但跑 `$MVN -f agent_serving_java/pom.xml compile` 确认无意外牵连——应零）
- 本文件追加"批次3 完成记录"（提交链/偏差/门禁数字/批次4 接手清单：检索侧依赖的 units/embed_status 语义、脏标记与手动重编译口径、conflicts 账前端展示位、**质量账三本合一面板**（links.broken+flags+专页——批次3 只有编译报告一处展示，接手清单挂账）、编辑器 provenance 录入 UI（收紧决策7 的人工页发布校验的前置））

---

## 验收口径（批次3 完成定义）

1. 绑定→编译→目录确认→写作→completed 全链在 Fake 层走通；stats/conflicts 正确。
2. origin 保护：人工改过的页重编译后内容不变（Fake 层断言）；machine 页覆盖。
3. 引用规范：machine 页 provenance 非空、src 悬空入 flags、密度告警、draft 宽免。
4. 重跑稳定性基础：同输入聚类结果逐字节相等；LLM 幂等键稳定（prompt_version 冻结）。
5. 门禁：Python 差分零新增失败；前端全绿+build 零错；表格路径零回归。

---

## 批次3 完成记录（2026-10-22 定稿）

**提交链**（基于批次2 完成点 2139768）：d451abc 计划 → 56cd904 T1 sources 绑定 → ce5151d T1 修复（指纹 scope 感知/dirty 遮蔽/输入硬化）→ a5ee7f0 T2 取材+聚类 → 39a1b6c T2 修复（质心缓存+种子准入 3.8x/内存 array 化/共享快照去重）→ 75c8597 T3 编译主体 → adcced0 T3 修复（僵尸跨轮/停止位按 batch_id/cache 归属）→ 7c4536c T3 收口（start 建批原子化/缓存删除清理/悬空口径收窄簇成员）→ 17f0672 计划对齐 → 14ac7e2 T4 前端编译面板 → bf5c904 T4 修复（awaiting_toc 停轮询保编辑/切换重启）→ 4d171f7 T4 收口（过期响应防护/错误路径测试/僵尸定时器）。

**门禁**：Python 全量 37f/**2159p**/22s/261e（merge-base 基线 37f/2087p/21s/261e——失败/错误数完全一致，+72 passed=批次3 新增）；MCP 165 passed；Java `mvn compile` 零牵连；前端 **617 用例全绿**（93 文件）+ `npm run build` 零类型错。审查链：每任务规格+质量两审共 8 轮+5 轮修复（计划审查 1C/4H/5M/7L 全修；T2 性能实测 3.8x；T3 并发对抗核验双任务交错测试）。

**关键实现定稿（批次4/5 依赖）**：
- **指纹 scope 感知**：`current_fingerprint(kb_id, directories)`——dirty=绑定范围内编译语料变化；`_run_prepare` 落库指纹**必须同参**调用（两侧同形铁律）。
- **量级护栏**：Σscope 单元 >30000（MAX_UNITS）→ 422 too_many_units；向量 array('f') 4KB/条；聚类 O(N·max_seeds=500·D)（实测 600 单元 4.4s，30k 外推 ~6 分钟）。
- **编译状态机**：kb_artifact_batches.details（jsonb 增量合并+guard_states 守卫写）；start=advisory lock 事务内建批原子带 `{"state":"preparing"}`；停止位按**本批 batch_id** 的 state≠writing 即停；_PREPARE_CACHE 进程内存（键=artifact_id 值带 batch_id 归属账；重启丢=awaiting_toc 作废提示重编；制品硬删时 drop_prepare_cache 延迟 import 尽力清理）。
- **悬空引用三接缝分层**：编译期=页簇成员并集（严）/发布期=rev0 provenance 并集（宽，err-safe）；发布校验（validate_pages_for_publish）：machine topic 页 provenance 空或悬空→阻断 409 publish_validation_failed；manual 页空 provenance→warning（**偏差记录**：设计"硬性"对人工页降为警告——编辑器无 provenance 录入 UI）；专页（glossary/open_question/contradiction）一律宽免。
- **create_batch**：file_name/sheet_name 传空串（NOT NULL DEFAULT ''）。
- **前端轮询**：usePolling 复用；POLL_STATES={preparing,writing}（awaiting_toc/终态停）；epoch+artifactId 双防护过期响应；终态 emit changed 刷新乐观锁令牌。

**对设计的偏差记录**：①不做"凝聚度低重聚"（§4.2 ②）；②人工页发布 provenance 降警告（§4.4）；③TOC 输入 top-150 簇折叠（§4.2 ③输入有界化）；④质量账三本合一面板只有编译报告一处展示（§4.5——挂账批次4/5）。

### 批次4 接手注意事项

1. **检索侧依赖**：kb_wiki_page_units 按 (artifact_id, revision=current_revision, embed_status='done') 过滤——批次2 发布投影+embedding 异步已产数据；HNSW 走 idx_wiki_units_hnsw（009 经验：ANN 两层 SQL+force_custom_plan 会话参数已全局生效于域池）。
2. **conflicts 账前端展示位**：编译报告已有 conflicts 明细；编辑器内冲突提示未做（用户裁决=手工删/改其一）。
3. **编辑器 provenance 录入 UI**（收紧人工页发布校验的前置）未做——manual 页发布仍走 warning 通道。
4. **挂账小项**：发布校验 N+1（list_pages+逐页 get_page）；写作上下文 dataclass 化；_run_writing ~118 行；system 角色界定；轮询失败恢复交互（现=停轮询单次提示）；脏横幅直达重编译按钮（裁定省略——编译区就在下方）；errorCode 共享工具。
5. **真库验收提醒**：PG 门控用例集（test_wiki_db.py 含批次3 新增 collect_units/指纹/守卫/共享快照用例）须 py≥3.12 环境跑；编译管线全链真库预演属批次5。
