# Wiki 制品批次2：生命周期服务域 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** wiki 制品从"空壳"变成可手工建页/改页/发布/回滚/阅读的完整生命周期——发布事务接管（pages/links 快照+units 投影+向量异步）、REST wiki 端点、前端编辑器与阅读器、delete 级联补齐。

**Architecture:** DB 层新增 `db_wiki.py`（WikiDB，专有三表+来源表数据访问，发布/回滚/discard/ensure_draft 的 wiki 事务镜像 db_artifacts.py 同名方法）；`ArtifactService` 在 publish/rollback/discard/diff 四处插入 kind 分支点（注入 WikiLifecycle 协作者，表格路径一行不改）；新 `WikiService` 管页面 CRUD（origin 规则+专页 frontmatter 校验+链接重算）与发布后置（units 投影+embedding 异步）；路由 `routes/wiki.py` 挂 `/api/artifacts/{id}/wiki/*`；前端 WikiEditorPanel/WikiReaderPanel 拆掉占位守卫。

**Tech Stack:** FastAPI + asyncpg(dict_row) + PostgreSQL(pgvector) + Vue3 + Element Plus + marked/DOMPurify（已装，依赖冻结）。

**设计依据:** `docs/下一阶段/63-Wiki类知识制品MVP设计-与表格制品逐层对照-2026-10-21.md` §三（发布事务对照表）、§3.1/3.2（改名口径/origin 保护）、§5.3（切片口径）、§5.6（检索入口清单 REST 部分）、§六（前端对照）。DDL 已在批次1落地（`databases/kb/schemas/018_wiki_artifacts.sql` + 迁移010，本批次**零 DDL 改动**）。

**批次边界声明：** §5.6 的 `/wiki/search` 端点与阅读器搜索框、三通道检索（dense/keyword/fusion）**归批次4**（Java ArtifactWikiSearchService，制品域独立路径）；引用规范校验（provenance 非空/悬空引用/密度告警）与 JSONB 契约（batches.details/sources.scope/fingerprint 形状）**归批次3**——本批次 provenance 仅透传不校验。

---

## 本批次定稿的两个实现决策（对设计的细化，非变更）

1. **检索单元按版本保留，发布时不删旧版单元**（细化设计 §5.3"删旧版检索单元"的口径）：units 表主键含 revision，检索恒按 current_revision 过滤，不存在脏读；保留旧版单元使**回滚后检索立即可用**（无需重算向量）。单元清理只发生在制品硬删（级联）。发布投影幂等：先 DELETE 该 (artifact_id, revision) 既有单元再 INSERT。
2. **行内引用 `[[src:unit_id]]` 阅读器 MVP 渲染为角标**（tooltip 显示 unit_id，点击复制），**回跳库切片原文延后 P1**（需复用 A1 定位协议的 serving 侧接口，超出本批次）。

## 铁律（每个任务的实施者必须遵守）

- **依赖冻结**：不新增任何依赖包（pyproject.toml / pom.xml / package.json 一律不动）。
- **git add 只写显式路径**，禁止 `git add -A` / `git add .`；**永不提交** `config/system/llm_service.yaml` 与任何密钥。
- 表格制品既有路径**一行不改**（只允许插入 kind 分支点）；既有测试全绿是硬门禁。**唯一豁免**：`delete_artifact` 级联加四张 wiki 表的 DELETE——共享方法上的无条件插入，对 table 制品为空删零副作用（批次1遗留缺口①，必做）。
- **网关铁律**：kb-ui 经 `createProxyClient('mining')` 走 `/api/control-plane/api/v1/proxy/...`，`main_control_service/auth.py` 对 raw path 含 `%` 一律 400——**URL path 里禁止出现非 ASCII**（中文 page_key 必须 hex 编码传输，照抄表格 row_key 先例 `db_artifacts.py:54-66`）。
- 注释/文档用中文，风格随仓库（看邻近代码）。
- worktree：`D:\mywork\AgenticKB\.claude\worktrees\wiki-artifact-mvp`，git 用全路径 `"/c/Program Files/Git/cmd/git.exe"`（PATH 缺 git）。
- 测试命令速查（docker；`<WT>` = worktree 绝对路径）：

```bash
PY='docker run --rm -v "<WT>:/w" -w /w agentickb-release-validation:a74b940 sh -c "pip install -q pytest pytest-asyncio && python -m pytest"'
$PY knowledge_mining/tests/kb -q                          # 制品域（本批次主战场）
$PY knowledge_mining/tests/kb/test_wiki_service.py -q    # 单文件
cd kb-ui && npm test && npm run build                     # 前端单测 + 类型真验证
```

- 基线口径：kb 套件环境基线 37 failed / 261 errors（容器缺真 PG kb_db_test，**只在真库门控下跑的用例不计**）；MCP 165；前端 558。完成后与 merge-base 24c43d4 做 test-ID 级对比，**零新增失败**。

---

## Task 1: DB 层 db_wiki.py（WikiDB 专有表数据访问）

**Files:**
- Create: `knowledge_mining/mining/kb/db_wiki.py`
- Test: `knowledge_mining/tests/kb/test_wiki_db.py`（真 PG 门控）；`knowledge_mining/tests/kb/test_wiki_links.py`（纯函数，无 PG）

**锚点（照抄对象，全部在 `knowledge_mining/mining/kb/db_artifacts.py`）：** 文件头风格契约 L1-13、`delete_artifact` L199-234、`publish` L428-534（事务骨架）、`rollback_to` L561-600、`discard_draft` L660-686、`ensure_draft` L688-729、`diff_draft_vs_current` L1329-1399（含 `_empty_diff`）、`_assert_expected_updated_at` L1097-1113（FOR UPDATE+微秒全精度比对）。DDL：`databases/kb/schemas/018_wiki_artifacts.sql`（列名以此为准）。

### WikiDB 方法清单（全部实现）

**页面 CRUD（rev0=设计态工作区；revN=发布快照）：**
- `count_pages(artifact_id, revision=0) -> int`
- `list_pages(artifact_id, *, revision) -> list[dict]`：ORDER BY ordinal, page_key；返回 page_key/title/page_type/origin/ordinal/flags/updated_at/updated_by + **aliases**（自 frontmatter.aliases 派生的 list[str]，前端链接解析兜底用）（不含 body_md——列表轻量）
- `get_page(artifact_id, revision, page_key) -> dict | None`：全字段（含 body_md/frontmatter/provenance）
- `upsert_page(artifact_id, *, page_key, title, page_type, frontmatter, body_md, provenance, flags, ordinal, origin, updated_by, expected_updated_at)`：**单事务**：①kb_artifacts FOR UPDATE + expected_updated_at 全精度比对（失配 stale_artifact——对齐 `update_cell` L1122-1134 的锁+touch 同事务模式，页写必须推进乐观锁令牌，否则并发编辑与发布携带旧令牌均放行=静默丢更新）②`INSERT ... ON CONFLICT (artifact_id, revision, page_key) DO UPDATE SET ...`（所有内容字段+updated_at=now()+updated_by）③`UPDATE kb_artifacts SET updated_at=now()`。revision 固定 0（设计态）
- `delete_page(artifact_id, page_key, *, expected_updated_at) -> bool`：同款单事务（锁+比对+删页+touch）

**链接维护：**
- `replace_page_links(artifact_id, revision, from_page, edges)`：DELETE WHERE (artifact_id,revision,from_page) 再批量 INSERT（edges 由纯函数算好）
- `recompute_link_statuses(artifact_id, revision)`：对该 revision 全量 links 重算 valid/broken（目标页存在或命中 alias → valid）。页删后指向它的边自动落 broken（63号 §3.1）
- `list_links(artifact_id, *, revision, page_key=None, direction="out")`：direction="out" → WHERE from_page=page_key；"in" → WHERE to_page=page_key（反向链接，63号 §2.4）；无 page_key → 全量

**模块级纯函数（同文件顶部，供 service 复用）：**
- `normalize_page_key(title: str) -> str`：NFKC → strip → `\s+`→`-` → 剔除 `[^\w一-鿿-]`（\w 含下划线数字字母）→ 空 则抛 `WikiError("invalid_page_key")`（**normalize 结果可含中文**——中文是 page_key 存储形态，仅传输层编码，见下条）
- `encode_page_key(page_key) -> str` / `decode_page_key(hex) -> str`：UTF-8 hex 编解码。**URL path 里的 page_key 一律走 hex**（网关铁律：raw path 含 % 即 400，照抄 `_encode_key_parts` row_key 先例——路由收 hex 参数后解码再查库；非法 hex → `WikiError("invalid_page_key")`）
- `extract_wiki_links(body_md: str) -> list[tuple[str, str]]`：解析 `[[目标]]` 与 `[[目标|锚文本]]` → (to_key, anchor_text)；**跳过 `[[src:...]]`**（引用标记不是页面链接）；去重保序
- `compute_page_edges(pages: list[dict]) -> list[dict]`：输入 [{page_key, frontmatter, body_md}]，输出 edges=[{from_page,to_page,anchor_text,status}]；**目标解析归一**：to_key 命中某页 page_key 或其 frontmatter.aliases → `to_page` 写该页 canonical page_key（63号 §2.4：links.to_page=目标页 page_key；anchor_text 保留书写原文）→ status valid；未命中任何页/别名 → to_page 原样、status broken。归一是反向链接（WHERE to_page=?）与阅读器跳转（前端按 canonical key 找页）的正确性前提
- `chunk_page_for_units(title, body_md) -> list[tuple[unit_key, content_text]]`：≤2000 字 → [(page_key, title+"\n\n"+body)]；超长按 H2（`^## `）分节 → page_key#s1..#sN（文档序 1 起）；分节后单节仍 >2000 按段落硬切；每块内容前置 `# {title}` 与节标题行（63号 §5.3：块文本前置页标题上下文）

**diff：**
- `diff_draft_vs_current(artifact_id) -> dict`：rev0 pages vs current_revision pages 按 page_key 对齐；added=rev0 有 current 无；removed=反向；changed=title/body_md/frontmatter/page_type/origin 任一不同（origin 翻 manual 也算 changed——是真实待发布变更）。**返回键与 `db_artifacts.diff_draft_vs_current` L1387-1392 完全同名**（`added/removed/changed` 三键，各为 {count, samples} 形态——以实际代码为准逐字对齐，样本=page_key 列表，上限对齐 `_DIFF_SAMPLE_LIMIT`）

**units 投影（63号 §5.1：发布事务的后置投影，轻量同步）：**
- `project_units(artifact_id, revision) -> int`：DELETE 该 (artifact_id,revision) 既有 units → 逐页 `chunk_page_for_units` INSERT（embed_status='pending'）→ 返回单元数
- `units_status(artifact_id, revision) -> dict`：{total,done,failed,pending}
- `list_pending_units(artifact_id, revision, limit=32)`
- `update_unit_embeddings(artifact_id, revision, unit_keys, vectors)`：写 embedding + embed_status='done'。**vector 参数走文本字面量绑定**（`'[' + ','.join(map(str, vec)) + ']'::vector`，照抄挖掘侧 `retrieval_projection/repositories_pg.py:163` `_vec_literal` 口径——asyncpg 无 pgvector 适配器，直接传 list 会炸）
- `mark_units_failed(artifact_id, revision, unit_keys)`

**生命周期事务（镜像 db_artifacts 同名方法，逐步骤同构）：**
- `publish(artifact_id, *, expected_updated_at, change_summary, published_by) -> dict`：单连接单事务：①`SELECT * FROM kb_artifacts WHERE artifact_id=... FOR UPDATE` + expected_updated_at 全精度比对（失配 `stale_artifact`）②门禁：status ∈ draft/published/deprecated；usage_note 非空（否则 `usage_note_required`）；`draft_open=FALSE 且 current_revision>0` → `no_pending_changes`；**wiki 新增门禁：rev0 页数=0 → `wiki_no_pages`** ③`new_rev = GREATEST(current, MAX(kb_artifact_revisions.revision), COALESCE(大于0的 kb_table_rows 最大 revision,0), COALESCE(大于0的 kb_wiki_pages 最大 revision,0)) + 1` ④pages rev0→new_rev `INSERT..SELECT` + links rev0→new_rev `INSERT..SELECT`（各记 rowcount；页数=pages rowcount）⑤`INSERT INTO kb_artifact_revisions`：schema_json=页面清单 JSON（`{"pages":[{page_key,title,page_type,ordinal,origin}按 ordinal,page_key 排序],"toc":null,"glossary":[]}`，toc/glossary 批次3编译填充）、row_count=页数、change_summary、published_by ⑥DELETE rev0 pages+links ⑦UPDATE kb_artifacts SET current_revision/status='published'/draft_open=FALSE/updated_at=now()。返回 `{"revision":new_rev,"page_count":N}`（对齐表格 publish 返回键——表格返回 row_count，wiki 用 page_count，service 层归一）
- `rollback_to(artifact_id, revision)`：**纯指针拨回**（实际 `db_artifacts.rollback_to` L561-600 就是：FOR UPDATE+乐观锁比对+目标 revision 存在校验+UPDATE current_revision/status='published'——**数据不动，不碰 rev0**）。wiki 版逐字同构：同样的校验与 UPDATE，**不碰 pages/links/units**（按版本保留决策①，回滚后检索直接用目标版 units）。rev0 清空与 checkout 只属于 discard_draft/ensure_draft
- `discard_draft(artifact_id)`：镜像 L660-686，DELETE rev0 pages+links + draft_open=FALSE
- `ensure_draft(artifact_id)`：镜像 L688-729（幂等；draft_open=FALSE 且 current>0 → checkout：rev0 清空后 current→rev0 复制 pages+links；**checkout 不推进 updated_at**）

**错误：** 模块级 `class WikiError(Exception)` 带 code（invalid_page_key/wiki_no_pages/stale_artifact/no_pending_changes/usage_note_required/page_not_found/page_key_conflict…），service 层翻译（对齐 artifact_service._translate 模式）。

### 步骤

- [ ] **Step 1** 写 `test_wiki_links.py`（纯函数，无 PG）：normalize_page_key（中文标题/空/非法字符/空白折叠）、encode/decode_page_key（中文往返/非法 hex）、extract_wiki_links（普通/锚文本/去重/跳过 src:）、compute_page_edges（valid/broken/**alias 命中归一 canonical key**）、chunk_page_for_units（短页单块/超长 H2 分节/单节超长段落硬切/块前缀含标题）
- [ ] **Step 2** 写 `test_wiki_db.py`：`pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]` + 模块级 skip 门控（照抄 `test_artifacts_db.py:21-27` 的 `KB_RUN_POSTGRES_ACCEPTANCE` 模式；fixture 用 `tests/kb/conftest.py` 的 `async_pool`）。用例：页面 CRUD 往返；publish 全事务（建页→publish→断言 revisions 行 schema_json 页面清单+row_count、rev0 已清、快照页齐全、二次空 draft publish → no_pending_changes；rev0 无页 publish → wiki_no_pages）；rollback/discard/ensure_draft 镜像用例；units 投影（含 >2000 字分节；重复 project 幂等）；links 维护（replace+recompute：删目标页后边落 broken）
- [ ] **Step 3** 跑 `test_wiki_links.py` 红→实现 db_wiki.py 纯函数部分→绿
- [ ] **Step 4** 实现 WikiDB 全部方法（PG 用例跑不了本地——语法层面用 `$PY knowledge_mining/tests/kb/test_wiki_links.py -q` + 全 kb 套件回归确认无 import 破坏；test_wiki_db.py 留待有真库环境跑）
- [ ] **Step 5** Commit：`git add knowledge_mining/mining/kb/db_wiki.py knowledge_mining/tests/kb/test_wiki_db.py knowledge_mining/tests/kb/test_wiki_links.py` → `feat(wiki): WikiDB 专有表数据访问层（页面/链接/units 投影/生命周期事务）`

---

## Task 2: db_artifacts.py kind 接线（delete 级联 + ArtifactService 分支点）

**Files:**
- Modify: `knowledge_mining/mining/kb/db_artifacts.py:199-234`（delete_artifact）
- Modify: `knowledge_mining/mining/kb/services/artifact_service.py`（构造函数 + publish/rollback_to/discard_draft/diff_draft_vs_current 四处分支点）
- Modify: `knowledge_mining/tests/kb/test_artifacts_db.py`（PG 门控文件追加 delete 级联用例）
- Test: `knowledge_mining/tests/kb/test_artifacts_service.py`（追加 wiki 分支用例）

**要求：**
1. `delete_artifact` 事务内级联清单**追加四张**（顺序插在 `kb_artifact_batches` 删除之后）：`kb_wiki_page_units` → `kb_wiki_page_links` → `kb_wiki_pages` → `kb_artifact_sources`（自底向上；对 table 制品为空删无副作用）。批次1遗留缺口①，必做。
2. `ArtifactService.__init__(self, db, wiki=None)` 新增可选协作者；四处方法在拿到 artifact 行后插分支（**表格路径零改动**）：
   - `publish` L414-440：`_assert_role(ROLE_DESIGN)` 与 `_check_freshness` 之后、原表格逻辑之前：`if art["kind"] == "wiki": return await self._wiki.publish(user, art, change_summary=..., expected_updated_at=...)`（wiki 侧自带门禁与事务；返回值归一为表格同形 `{revision, row_count(=page_count), diff}`）
   - `rollback_to` / `discard_draft`：同位置同法分支（wiki 协作者方法名同名）
   - `diff_draft_vs_current`：kind=wiki → 委托 `self._wiki.diff(...)`（同形返回）
   - `deprecate/republish/list_revisions/成员治理/update_meta`：**不分支**（kind 无关）
3. wiki 协作者在本任务只是**注入点**——具体实现在 Task 3 的 `WikiLifecycle`。本任务的测试用**Stub**（记录调用的假对象）验证分支路由正确 + 表格路径行为不变。

### 步骤
- [ ] **Step 1** `test_artifacts_service.py` 追加：FakeArtifactDB 场景下 kind=wiki 的 publish/rollback/discard/diff 四方法均路由到注入的 stub（断言 stub 收到 artifact 行与参数）；kind=table 时 stub 不被调用（回归）
- [ ] **Step 2** 跑红→实现分支点→跑绿（含既有 service/routes 全量回归 `$PY knowledge_mining/tests/kb -q`）
- [ ] **Step 3** `test_artifacts_db.py` 追加 PG 用例：建 wiki 制品+页+units → `delete_artifact` → 四张 wiki 表 0 行（与既有表格级联用例同模式）
- [ ] **Step 4** Commit：`git add knowledge_mining/mining/kb/db_artifacts.py knowledge_mining/mining/kb/services/artifact_service.py knowledge_mining/tests/kb/test_artifacts_db.py knowledge_mining/tests/kb/test_artifacts_service.py` → `feat(wiki): delete 级联补四表 + ArtifactService 生命周期 kind 分支点`

---

## Task 3: WikiService + WikiLifecycle + 路由 + embedding 后置

**Files:**
- Create: `knowledge_mining/mining/kb/services/wiki_service.py`（WikiLifecycle + WikiService 同文件，两者强耦合）
- Create: `knowledge_mining/mining/kb/routes/wiki.py`
- Modify: `knowledge_mining/mining/kb/deps.py`（get_wiki_db/get_wiki_service/改造 get_artifact_service 注入 lifecycle）
- Modify: 路由注册点（找 `routes/artifacts.py` 的 router 被 include 的位置，同处追加 wiki router）
- Test: `knowledge_mining/tests/kb/test_wiki_service.py`、`knowledge_mining/tests/kb/test_wiki_routes.py`

**锚点：** `services/artifact_service.py` 的 `_assert_role` L226-258、`_require_design_session` L868-878、`_check_freshness` L118-130、错误翻译 `_translate`；`infra/llm_task_client.py`（LlmTaskClient：submit_embedding L130-157 / batch_status L178-208 / wait_for_tasks L210-253）。**⚠️ LlmTaskClient 是同步阻塞客户端**（httpx.Client + time.sleep 轮询，wait_for_tasks 默认 deadline 900s——挖掘侧只在 worker 线程用它）；配置来源照 `api/app.py:214-215`（`MiningConfig().llm_service_url`）；客户端实例**模块级单例**（每请求 new 一个 httpx.Client 不 close 会泄漏连接池）。路由测试模式照 `test_artifacts_routes.py`（真实 Service+Fake DB+dependency_overrides+httpx ASGITransport；`DOMAIN="cloud_core_network"`、URL 前缀 `/api/artifacts`）。

### WikiLifecycle（注入 ArtifactService 的协作者）

```python
class WikiLifecycle:
    def __init__(self, adb: ArtifactDB, wdb: WikiDB, task_client: LlmTaskClient): ...
    async def publish(self, user, art_row, *, change_summary, expected_updated_at) -> dict:
        # wdb.publish 事务 → 事务提交后：project_units(revision) + _spawn_embed(artifact_id, revision)
        # 返回 {revision, row_count=page_count, diff: wdb.diff_draft_vs_current(...) 提交前取}
    async def rollback_to(self, user, art_row, revision): ...   # 权限/新鲜度由 ArtifactService 前置完成，这里只调 wdb
    async def discard(self, user, art_row): ...
    async def diff(self, artifact_id) -> dict: ...
```

**embedding 后置（63号 §5.1：共享 llm_service 基建）：**
- `_spawn_embed(artifact_id, revision)`：`asyncio.create_task(asyncio.to_thread(self._embed_units, artifact_id, revision))`——**阻塞体全部在线程里跑**（LlmTaskClient 同步阻塞，直接在协程里调会冻住整个 mining 事件循环最长 900s）；模块级 in-flight 集合 `_EMBED_INFLIGHT: set[tuple[str,int]]` 防重入（发起时加入、finally 移除）
- `_embed_units`（同步函数，跑在 worker 线程）：循环 `list_pending_units(limit=32)` → `task_client.submit_embedding(texts, max_attempts=3, idempotency_key=f"wiki-unit:v1:{artifact_id}:{revision}:" + sha256("\x1f".join(content_texts))[:24])` → `wait_for_tasks` → `update_unit_embeddings`。**幂等键含 artifact_id+revision+内容哈希**（unit_key 不含版本，跨版本撞键会让 llm_service 命中旧任务拒绝复用→新版单元 failed/向量陈旧；内容哈希口径照抄挖掘 `infra/embedding.py:145-155` `_batch_key`）。**失败语义**：llm_service 报真终态失败（重试耗尽）→ `mark_units_failed`；**超时/瞬时异常 → 保持 pending**（下次发布或 status 愈合重试，pending 是可重试态、failed 不自动重试）；整体异常 → 日志，**永不向上抛**
- **重启自愈**：`GET /wiki/status` 时若 pending>0 且 (artifact_id,current_rev) 不在 in-flight → 顺手 `_spawn_embed`（幂等键保证 llm_service 侧不重复计费）

### WikiService（页面编辑编排）

```python
class WikiService:
    def __init__(self, artifact_service: ArtifactService, wdb: WikiDB, lifecycle: WikiLifecycle): ...
    # lifecycle 引用供 /wiki/status 重启自愈调用 _spawn_embed（deps 装配同一实例，无环）
```

- `upsert_page(user, artifact_id, *, page_key, title, page_type, frontmatter, body_md, provenance, flags, ordinal, expected_updated_at)`：
  1. `_require_design_session` 同义前置（复用 artifact_service 的 `_assert_role(ROLE_DESIGN)` + `_check_freshness` + `wdb.ensure_draft`——注意 ensure_draft 用 wdb 的）
  2. page_key 缺省 = `normalize_page_key(title)`；已存在不同页 → 更新；**origin 规则（63号 §3.2）**：人工写入口**恒写 origin='manual'**（machine 只能由批次3编译管线产生）；更新已有 machine 页同样翻 manual
  3. 专页 frontmatter 校验（63号 §4.3，frontmatter 严格校验）：contradiction 必含 claim_a/claim_b/severity(∈low|medium|high)；open_question 必含 question(以?结尾)/priority；glossary 必含 term；topic 无强制键。违规 → `WikiError("invalid_frontmatter")` 附字段名
  4. 落库 `wdb.upsert_page` → `compute_page_edges`+`wdb.replace_page_links`(from_page=本页) → `wdb.recompute_link_statuses`(全量,页删后断链账 63号 §3.1) → 返回完整页
- `delete_page(...)`：同前置；删页→`recompute_link_statuses`（指向它的边落 broken）
- `list_pages(user, artifact_id, *, draft=False, revision=None)`：draft=True → ROLE_DESIGN + rev0；缺省 → ROLE_RUNTIME_READ + current_revision（draft_open 且 current=0 → 空列表）
- `get_page(...)`：同上单页全字段
- `list_links(user, artifact_id, *, page_key, direction, draft)`：runtime 读 current / design 读 rev0
- `status(user, artifact_id)`：ROLE_RUNTIME_READ；{revision: current_revision, units: wdb.units_status(current)}；current=0 → units 全 0

### 路由（routes/wiki.py，`APIRouter(prefix="/api/artifacts", tags=["wiki-artifacts"])`）

| 端点 | 方法 | 角色 | 说明 |
|---|---|---|---|
| `/{artifact_id}/wiki/pages` | GET | runtime（draft=true→design） | query: draft/revision |
| `/{artifact_id}/wiki/page/{page_key}（hex 编码，网关铁律）` | GET | 同上 | 全字段；路由先 decode_page_key |
| `/{artifact_id}/wiki/page/{page_key}（hex）` | PUT | design | body: title/page_type/frontmatter/body_md/provenance?/flags?/ordinal?/page_key?/expected_updated_at |
| `/{artifact_id}/wiki/page/{page_key}（hex）` | DELETE | design | **锁令牌随 body**（WikiPageDeleteReq{expected_updated_at}——对齐表格 RowDeleteReq L149-150/L757-772 的 body 模式） |
| `/{artifact_id}/wiki/links` | GET | runtime（draft=true→design） | query: page_key/direction/draft |
| `/{artifact_id}/wiki/status` | GET | runtime | units 进度 |

- 错误翻译 `_map_error` 照抄 `routes/artifacts.py:155-188` 模式（WikiError code → status/信封一致）。
- **权限铁律**：rev0 对 runtime 角色不可见（63号 §5.5——viewer 传 draft=true 一律 403，同表格 diff/preview 模式）。

### 步骤
- [ ] **Step 1** `test_wiki_service.py`（FakeWikiDB 内存实现）：origin 恒 manual（新建+改 machine 页）；normalize_page_key 缺省/非法；专页校验四类型；链接重算（建 A/B 链接→删 B→broken）；revision 路由（draft/current）；status；embedding 后置（FakeTaskClient 记录 submit 调用参数 max_attempts=3/幂等键形态；失败批 mark_failed 不抛）
- [ ] **Step 2** 跑红→实现 wiki_service.py→绿
- [ ] **Step 3** `test_wiki_routes.py`：六端点全打（含权限矩阵：viewer PUT 403、draft 可见性、404 反探测、422 定位断言）；dependency_overrides 装 get_wiki_service
- [ ] **Step 4** 跑红→实现 routes/wiki.py + deps.py + 注册→绿；`$PY knowledge_mining/tests/kb -q` 全量回归
- [ ] **Step 5** Commit：`git add knowledge_mining/mining/kb/services/wiki_service.py knowledge_mining/mining/kb/routes/wiki.py knowledge_mining/mining/kb/deps.py <注册点文件> knowledge_mining/tests/kb/test_wiki_service.py knowledge_mining/tests/kb/test_wiki_routes.py` → `feat(wiki): WikiService/WikiLifecycle/wiki 路由六端点+发布后 units 投影与向量异步`

---

## Task 4: 前端 API 扩展 + WikiEditorPanel（设计态编辑器）

**Files:**
- Modify: `kb-ui/src/api/artifacts.ts`（追加 wiki 方法）、`kb-ui/src/types/artifacts.ts`（追加类型）
- Create: `kb-ui/src/components/artifacts/WikiEditorPanel.vue`
- Create: `kb-ui/src/utils/markdown.ts`（通用 renderMarkdown——从 `src/help/renderMarkdown.ts:37-40` 抽骨架，**去掉 help 章号链接逻辑**，help 版保留原样不动）
- Test: `kb-ui/src/api/__tests__/artifacts.spec.ts`（追加）、`kb-ui/src/components/artifacts/__tests__/WikiEditorPanel.spec.ts`

**锚点：** 组件挂载模式照 `ArtifactRowsPanel.vue`（props {artifactId,canWrite,expectedUpdatedAt,draftOpen} + emits changed + defineExpose(load)；DetailView :87-109 传法）；API 方法风格照 artifacts.ts:197-296（createProxyClient 已有）；乐观锁令牌 = `artifact.updated_at` 放请求体/query（DetailView :97 传法）。

**类型：** `WikiPageMeta{page_key,title,page_type,origin,ordinal,flags,updated_at,updated_by}`、`WikiPage extends WikiPageMeta + frontmatter/body_md/provenance`、`WikiLink{from_page,to_page,anchor_text,status}`、`WikiUnitsStatus{total,done,failed,pending}`。page_type/origin 字面量联合对齐后端枚举。

**API 方法：** `listWikiPages/getWikiPage/upsertWikiPage/deleteWikiPage/listWikiLinks/getWikiStatus`（URL 对齐 Task 3 路由表）。**page_key 走 URL path 时一律 hex 编码**（`encodePageKey`：UTF-8 → hex，工具函数放 `src/utils/markdown.ts` 同级或 api 模块内导出；后端路由收 hex 解码——网关铁律）。DELETE 的 expected_updated_at 随 **body**（对齐表格 deleteRow :294-300 模式）。

**WikiEditorPanel：**
- 左栏：页面列表（ordinal 排序；page_type 徽标四种；origin 徽标 machine/manual；canWrite 时"新建页"按钮+行删除确认）；右栏：编辑表单——title / page_type 下拉 / aliases 与 tags（逗号输入→frontmatter 数组）/ body_md textarea（rows≈16）+ **预览开关**（`renderMarkdown` sanitize 后 v-html）/ ordinal 数字
- 保存：PUT upsertWikiPage（带 expected_updated_at）；409 stale → ElMessage 提示并 emit changed 触发父刷新；专页类型切换时表单下方提示必填 frontmatter 键
- 新建页 page_key 缺省由后端归一，前端不重复实现（提交 title 即可；响应回传 page_key 用于刷新选中）
- viewer（canWrite=false）：只读展示（表单 disabled）
- spec：渲染列表/选择页/保存调用参数/409 处理/canWrite=false 禁用；mock api 模块（照 ArtifactRowsPanel.spec.ts 模式）

### 步骤
- [ ] **Step 1** types+api+spec 追加 → npm test 绿
- [ ] **Step 2** utils/markdown.ts + WikiEditorPanel.spec.ts 写用例 → 红
- [ ] **Step 3** 实现 WikiEditorPanel → 绿
- [ ] **Step 4** `npm run build`（类型真验证）零错
- [ ] **Step 5** Commit：显式路径 add → `feat(wiki-ui): wiki 页面编辑器面板+通用 markdown 渲染工具+API 客户端扩展`

---

## Task 5: WikiReaderPanel（阅读器）+ DetailView 拆守卫 + 批次门禁

**Files:**
- Create: `kb-ui/src/components/artifacts/WikiReaderPanel.vue`
- Modify: `kb-ui/src/utils/markdown.ts`（追加 wiki 预处理）、`kb-ui/src/views/artifacts/ArtifactDetailView.vue`（拆守卫三处）、`kb-ui/src/components/artifacts/ArtifactPublishDialog.vue`（**单位文案 kind 化**：现硬编码"新增 N 行/删除 N 行"（:26/:53），wiki 发布显示错误单位——加可选 prop `unitLabel`（默认"行"）或按 artifact.kind 切换"行/页"，表格路径显示不变）
- Test: `kb-ui/src/components/artifacts/__tests__/WikiReaderPanel.spec.ts`、`kb-ui/src/views/artifacts/__tests__/ArtifactDetailView.spec.ts`（改 wiki 守卫用例）

**锚点（批次1遗留缺口④）：** `ArtifactDetailView.vue` 占位守卫 :82-85（design EmptyState）、:133-136（runtime EmptyState）、watch 守卫 :364-369（`kind !== 'wiki'` 条件**保留**——wiki 不触发表格列/行 API）；spec wiki 用例 :107-116。

**markdown wiki 扩展（utils/markdown.ts）：**
- `renderWikiMarkdown(md)`：预处理器把 `[[页面key]]`/`[[页面key|锚文本]]` 替换为 `<a href="#" data-wiki-page="key">锚文本或key</a>`；`[[src:unit_id]]` 替换为 `<sup class="wiki-src" data-unit-id="id" title="unit_id">¶</sup>`；再走 marked→DOMPurify（**sanitize 配置需放行 `data-wiki-page`/`data-unit-id` 属性与 sup/a 标签**——DOMPurify ADD_ATTR）→ v-html
- `extractWikiNavTarget(el)`：阅读器点击委托用（点中 data-wiki-page → 跳页）

**WikiReaderPanel（props {artifactId}；runtime 只读，ROLE_RUNTIME_READ 数据源=current revision）：**
- 左：目录侧栏（listWikiPages：ordinal 排序，page_type 图标区分；当前页高亮）
- 中：渲染页（getWikiPage → renderWikiMarkdown → v-html；点击 data-wiki-page → 切页——目标匹配按 page_key **或 aliases**（listWikiPages 已回传 aliases），都未命中 → ElMessage "目标页面未发布"；页头显示 title/origin 徽标/updated_at）
- 右：上下文侧栏——该页链接（listWikiLinks direction=out）+ 被链（direction=in），broken 标红；溯源区（page.provenance 渲染为 kb_id/snapshot_id/unit_ids 计数与列表）；`[[src:...]]` 角标点击复制 unit_id（决策②：回跳 P1）
- 未发布（current_revision=0）：EmptyState "尚未发布版本"
- 三栏响应式：窄屏右侧栏折叠加载（el-collapse 或简单隐藏，不引入新依赖）

**DetailView 拆守卫：**
- :82-85 design EmptyState → `<WikiEditorPanel v-if="artifact.kind==='wiki'" :artifact-id :can-write="expected-updated-at="artifact.updated_at" :draft-open="artifact.draft_open" @changed="reloadAll" />`（原 v-else 表格三组件保持）
- :133-136 runtime EmptyState → `<WikiReaderPanel v-else-if="artifact.kind==='wiki'" :artifact-id />`（说明卡/元信息区在守卫之前，保留不动）
- watch 门 :364-369：保留 `kind!=='wiki'` 条件（不拉表格列/行）；wiki 分支无需在此拉数据（Reader 自加载）
- spec：原"wiki 守卫用例"改为断言 wiki 时渲染 WikiEditorPanel/WikiReaderPanel（shallowMount 桩）且**不调用表格 API**（listColumns/listRows 断言不变）

### 步骤
- [ ] **Step 1** WikiReaderPanel.spec.ts + DetailView.spec.ts 改造 → 红
- [ ] **Step 2** 实现 markdown 扩展 + Reader + DetailView 接线 → 绿
- [ ] **Step 3** `npm test && npm run build` 零错
- [ ] **Step 4** Python 全量门禁：`$PY knowledge_mining/tests -q` 与 merge-base 24c43d4 test-ID 级对比零新增失败（kb 环境基线 37f/261e 口径核对）
- [ ] **Step 5** Commit：显式路径 add → `feat(wiki-ui): wiki 阅读器（双链跳转/断链/溯源侧栏）+详情页拆占位接面板`
- [ ] **Step 6** 本文件末尾追加"批次2 完成记录"（提交链/偏差/门禁数字/批次3 接手注意事项）

---

## 验收口径（批次2 完成定义）

1. 手工闭环：建 wiki 制品→建页（topic+contradiction 各一）→链接自动生成→发布（row_count=页数/版本行含页面清单）→阅读器可见可跳→改页（origin 翻 manual）→再发布→两版本并存→回滚→阅读器跟指针。
2. 单元投影：发布即产 units（pending）；FakeTaskClient 语义下 done；失败批落 failed 不抛。
3. 删除闭环：硬删后四张 wiki 表零残留。
4. 表格制品全路径行为不变（既有测试全绿）。
5. 门禁：kb 套件零新增失败；前端 build 0 错；vitest 全绿。
