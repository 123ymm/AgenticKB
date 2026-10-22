# Wiki 制品批次4：Agent 消费（Java 检索+MCP 分流） 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布后的 wiki 成为 Agent 可检索的"隐藏知识库"——Java `ArtifactWikiSearchService` 固定三通道（dense ANN/keyword/fusion），MCP `get_knowledge` art_ 引用按 kind 分流（卡片/搜索/页面整读/链接单跳），网页阅读器接搜索框。

**Architecture:** **kind 分流的权威在 Java `ArtifactQueryService.query` 内部**——MCP `tools.py query_artifact` 已经把 query dict 原样透传，MCP 侧零代码改动（只补文档文案与契约测试）；权限谓词整段复用 `selectVisibleArtifact`（**核实：kind 列已在 SELECT 与 `ArtifactVisibleRow` 中，零改动**）；dense 通道照抄 `searchDenseV2` 两层 ANN 形态打在 `idx_wiki_units_hnsw` 上（域池会话参数 ef_search=600/force_custom_plan 已全局生效）；融合直接调 `RrfSupport.fuse`（**该类现包私有，须改 public**）；查询向量用 serving 既有 `EmbeddingClient` bean；web 搜索框走 mining 新 REST 端点 httpx 转发 serving internal（**同容器 supervisor 共享 env 已核实**：docker-compose 单 app 服务，SERVING_INTERNAL_URL/AUTH_SECRET 为容器级 environment）。

**Tech Stack:** Java Spring Boot + MyBatis XML + pgvector HNSW；Python FastAPI（mining 转发端点）；MCP FastMCP（文案）；Vue3。**零 DDL、零新依赖。**

**设计依据:** 63号 §5.2（制品域独立检索路径——不进范式引擎/不混库侧 units 表/复用可抽取公共部件）、§5.4（消费入口对照：卡片/页面整读/向量/关键词/图单跳/分流）、§5.5（只看 published 版；rev0 对 Agent 不可见）、§5.6（入口清单）。批次3 完成记录接手清单①。

**MCP art_ 的 wiki query 协议（本批次定稿，63号 §5.4 的落地）**：`get_knowledge("art_xxx")` → 卡片（kind=wiki：页数/embedding 进度）；`query={"q":"搜索文本"}` → fusion 搜索（可带 `"mode":"dense|keyword|fusion"` 与 `"top_k":N`）；`query={"page":"page_key"}` → 页面整读（frontmatter+正文+provenance+链接摘要）；`query={"links":"page_key","direction":"out|in"}` → 链接单跳。**只定义 dict 形态**——get_knowledge 签名是 `query: dict | None`（FastMCP pydantic 校验），字符串形态在 MCP 入口即被拒（审查已证）。与表格 DSL 键零冲突（表格键=where/select/order/cursor/aggregate；`mode` 在 query dict 内部故不触发 art_ 分支对顶层 mode 参数的拒绝）。

---

## 铁律（同前批次）

依赖冻结（**pom.xml/package.json/pyproject 不动**）；git add 只写显式路径；不提交 llm_service.yaml；**表格制品 art_ DSL 路径零行为变化**（wiki 分支纯插入）；既有测试全绿是硬门禁；注释中文；git 全路径 `"/c/Program Files/Git/cmd/git.exe"`。门禁命令内联：
- Java：`MSYS_NO_PATHCONV=1 docker run --rm -v "D:\mywork\AgenticKB\.claude\worktrees\wiki-artifact-mvp:/w" -w /w -v "C:\Users\fuzhi\.m2:/root/.m2" maven:3.9-eclipse-temurin-21 mvn -q -f agent_serving_java/pom.xml test`
- Python kb：`MSYS_NO_PATHCONV=1 docker run --rm -v "D:\mywork\AgenticKB\.claude\worktrees\wiki-artifact-mvp:/w" -w /w agentickb-release-validation:a74b940 sh -c "pip install -q pytest pytest-asyncio && python -m pytest knowledge_mining/tests/kb -q --continue-on-collection-errors"`（批次3 后基线 14f/496p/1s/252e，差分零新增）
- MCP：同容器换 `python -m pytest mcp_server/tests -q`（基线 165）
- 前端：`cd kb-ui && npm test && npm run build`（批次3 后基线 617 用例）

**ANN 边界注释要求**：searchWikiUnitsDense 的候选层 `WHERE embed_status='done'` + LIMIT 是 post-filter 截断语义（无 iterative_scan，与 009 勘误口径一致）——SQL 注释照 searchDenseV2 风格记录该边界（MVP ≤200 页规模无碍）。

---

## Task 1: Java WikiArtifactToolMapper（SQL 层）

**Files:**
- Create: `agent_serving_java/src/main/java/com/coremasterkb/serving/operator/mapper/WikiArtifactToolMapper.java` + `src/main/resources/mapper/WikiArtifactToolMapper.xml`
- （核实项：`ArtifactToolMapper` 的 `selectVisibleArtifact` 已 SELECT kind 且 `ArtifactVisibleRow` 已有字段——**零改动**；写一行确认注释即可）
- Test: `operator/WikiArtifactToolMapperXmlTest.java`（SQL 形态回归，照 AssetRetrievalUnitV2MapperXmlTest L73 起 ANN 形态断言模式）

**SQL 清单（XML）：**
- `selectWikiPage(artifactId, revision, pageKey)`：kb_wiki_pages 单页全字段（page_key/title/page_type/frontmatter/body_md/provenance/origin/updated_at）
- `selectWikiLinks(artifactId, revision, pageKey, direction)`：direction out/in（照 wiki 服务语义；MyBatis `<choose>`）
- `selectWikiUnitsStatus(artifactId, revision)`：COUNT(*) FILTER (embed_status='done'/'pending'/'failed') + total
- `searchWikiUnitsDense(artifactId, revision, queryVector, candidateBudget)`：**两层形态照抄 searchDenseV2（AssetRetrievalUnitV2Mapper.xml:170-199）**：候选层 `FROM kb_wiki_page_units WHERE artifact_id=#{artifactId} AND revision=#{revision} AND embed_status='done' ORDER BY embedding <=> #{queryVector}::vector LIMIT #{candidateBudget}`（即 idx_wiki_units_hnsw 入口）；外层 JOIN kb_wiki_pages 取 title，`(1 - (embedding <=> #{queryVector}::vector)) AS score`；返回 unit_key/page_key/title/content_text/score
- `searchWikiUnitsKeyword(artifactId, revision, lexicalQuery, limit)`：`content_text ILIKE '%'||#{lexicalQuery}||'%'`（psycopg 无 tsvector 列零 DDL；CJK 子串命中）过滤 + JOIN pages 取 title，score 恒 1.0（RRF 只消费名次），ORDER BY ordinal 语义稳定（units 无 ordinal——用 page_key, unit_key 字典序稳定）
- 参数化彻底（ILIKE 拼接值走 `#{}` 绑定不拼 `%`）——Lexical 参数本身含 `%`/`_` 时转义与否：**口径=不转义（子串模糊语义，与表格 DSL like 同族）**，注释记录

**XML 形态回归测试**：解析 XML 断言关键 SQL 片段（ANN 两层/`<=>` cosine/embed_status 过滤/ILIKE 绑定/方向 choose）——照 AssetRetrievalUnitV2MapperXmlTest:77 模式。

## Task 2: Java ArtifactWikiSearchService + 分流 + 三 internal 端点

**Files:**
- Create: `agent_serving_java/src/main/java/com/coremasterkb/serving/structure/ArtifactWikiSearchService.java`
- Modify: `operator/operators/fuse/RrfSupport.java`（**class 与 fuse 方法包私有 → public**，注释"为制品域检索开放"；同包既有消费者零影响）
- Modify: `structure/ArtifactQueryService.java`（query 方法 wiki 分流：query==null → wikiCard；query!=null 且 kind=wiki → 委托 wiki 服务解释 q/page/links 协议；**表格分支原样**；非 wiki 也非 table kind 的既有拒绝保留）
- Modify: `structure/InternalArtifactController.java`（三个新端点 + **`assembleQueryView` 加 wiki search/page/links 视图装配分支**——现状只 instanceof 三种表格视图其余抛 IllegalStateException("unknown artifact view")，wiki 返回新 DTO 不加分支即 500）
- Test: `structure/ArtifactWikiSearchServiceTest.java`（Mockito 单测）+ `ArtifactQueryServiceTest` 追加 wiki 分流用例 + controller 装配断言（wiki 三视图各一）

**ArtifactWikiSearchService：**
- `search(artifactRef, q, mode, topK, domain, username, isAdmin, keyId)`：①selectVisibleArtifact 权限（null→artifact_not_found 404 反探测；kind!=wiki→unsupportedOperation）②revision=artifact.currentRevision（只看 published）③mode ∈ fusion|dense|keyword（默认 fusion）④dense/fusion：EmbeddingClient 算查询向量（bean 注入，照 QueryEmbed 算子既有用法）；**双口径降级**（照 DenseVectorOperator L95-118）：unitsStatus.done==0 / embedding 调用失败 / **vec.length≠1024**（维度守卫——llm_service 换模型后 `<=>` 对 VECTOR(1024) 直接 PG 报错，须降级不 500）/ dense SQL 抛错 → 该通道空 + `denseVectorDegraded` 留痕（63号 §5.2 降级口径）⑤keyword：ILIKE ⑥fusion：`RrfSupport.fuse(candidates, k=60, Map.of("wiki_dense",1.0,"wiki_keyword",1.0))`（**单测构造 RetrievalCandidate 用 5 参 legacy 构造器**——channelId 会置 source、channelRank=0，fuse 按输入序派生名次，channelOf 回退兼容）；候选预算：dense candidateBudget=max(5*topK,100) 硬顶 400、keyword limit=topK*3）⑦返回 `{results:[{page_key,title,unit_key,snippet(content_text 前 240 个**code point**——String.codePointCount 截断防代理对劈半),score,channels}], degraded_dense: bool, revision}`
- `readPage(artifactRef, pageKey, ...)`：权限同上 → selectWikiPage（**Java 侧直接收明文 page_key**——内网 internal 通道不经网关，MCP/前端转发端点各自负责编码）；不存在→page_not_found）+ 该页 links 摘要（out+in 各前 20）→ `{page:{...}, links:{out:[...],in:[...]}}`
- `links(artifactRef, pageKey, direction, ...)`：selectWikiLinks → `{links:[...]}`
- `card(artifactRef, ...)`：**独立 `WikiArtifactCard` record**（不扩展 ArtifactCard——加字段会连坐 table 分支构造点与既有测试全部 `new ArtifactCard(...)`）：{kind:"wiki", artifact_id, title, status, page_count, embedding:{total,done,failed,pending}, updated_at}；page_count 取 selectRevision 的 row_count，embedding 取 unitsStatus
- **分流接线**（ArtifactQueryService.query）：kind=wiki && queryNode==null → wiki.card；kind=wiki && query 含 q → search；含 page → readPage；含 links → links；其余键 → unsupportedOperation（中文错误消息说明 wiki 协议）；**controller assembleQueryView 对应新增 wiki 四视图分支**（card/search/page/links——wiki card 用 WikiArtifactCard）

**InternalArtifactController 三端点**（照 withAuth 模式 L183-204）：`POST /api/internal/artifact-wiki-search`、`/artifact-wiki-page`、`/artifact-wiki-links`（body 含 artifact_ref/domain/username/key_id + 各自参数）——供 mining 转发与直连调试；MCP 继续走既有 `/artifact-query`（分流在 QueryService 内部，MCP 零改动）。

**单测**：Mockito 桩 mapper——三通道各一（含 fusion RRF 名次合成断言）；降级两路（done=0/embedding 抛）；权限 404/kind 拒绝；page/links 读；卡片字段；分流路由（null→card/q→search/page→readPage/links/未知键拒绝）；**表格路径回归**（table kind + DSL 用例不变）。

## Task 3: MCP 文案与契约测试

**Files:**
- Modify: `mcp_server/server.py` DEFAULT_INSTRUCTIONS art_ 段（:163-165 附近）——补 wiki 协议 dict 形态说明（{"q"}/{"page"}/{"links"}；**无字符串形态**）
- Test: `mcp_server/tests/test_artifact_tools.py` 追加——wiki query 形态透传契约（{"q","mode"}/{"page"}/{"links"} 原样到达 artifact-query 端点；表格 DSL 不受影响）。**注明局限**：这些测试直接调 Python 函数不过 FastMCP 的 pydantic 入参校验，字符串形态假绿正是此坑——契约测试只覆盖 dict 形态（合法入参），并在测试 docstring 写明"字符串形态由 get_knowledge 签名排除，勿加"。

（MCP 代码零改动——dict 透传链路已存在且无结构预校验（审查核实 server.py art_ 分支只拒 relation/顶层 mode）；测试钉住"未来有人改 query_artifact 时不得吞 wiki 形态"。）

## Task 4: mining 转发端点 + 前端搜索框

**Files:**
- Modify: `knowledge_mining/mining/kb/routes/wiki.py`（GET/POST `/{artifact_id}/wiki/search`）+ 新建 `knowledge_mining/mining/kb/wiki_search_client.py`（httpx 转发薄客户端，env `SERVING_INTERNAL_URL`（默认 http://127.0.0.1:8081）+`SERVING_INTERNAL_AUTH_SECRET`，同容器 supervisor 与 MCP 共享 env——已核实 compose 容器级 environment；单例）
- Test: `knowledge_mining/tests/kb/test_wiki_search_client.py`（无 PG：httpx MockTransport 桩 serving——转发参数/**domain 取自 _assert_role 返回的 artifact 行 domain_id**/鉴权头/错误翻译 404 反探测/503→502 中文提示/超时）
- Modify: `kb-ui/src/api/artifacts.ts`（searchWiki）+ `WikiReaderPanel.vue`（搜索框：输入+回车→结果列表（title+snippet）→点击跳页；fusion 默认；**请求序号防护**——连续提交迟到旧响应不覆盖新结果（批次3 CompilePanel epoch 同款坑）；`WikiReaderPanel.spec.ts` 追加）

**转发端点语义**：ROLE_RUNTIME_READ（wiki 服务 `_assert_role` 前置，404 反探测在 mining 侧即拦）；query：`q/mode/top_k`；body 带 `{artifact_ref, q, mode, top_k, domain=artifact行.domain_id, username, is_admin=false, key_id=null}`；响应 serving 原样透传；serving 错误（artifact_not_found/page_not_found→404、503→502 带中文提示）；超时 15s。**不做缓存**。

## Task 5: 批次门禁 + jar 纯源码构建校验 + 完成记录

- Java：`mvn test`（Level 1 全绿：484 基线+新增）+ `mvn -DskipTests package` 产 jar → **zipfile 校验**（jar 内 `BOOT-INF/classes/mapper/WikiArtifactToolMapper.xml` 存在且含 `idx_wiki_units_hnsw` 相关 SQL 标记 `searchWikiUnitsDense`——纯源码构建铁律，防陈旧 target/classes 混入；校验命令形态见 releases.json:21 先例）
- Python kb 套件差分（批次3 后基线 14f/496p/1s/252e）+ 全量抽核；MCP 165+新增；前端 npm test+build
- 本文件追加"批次4 完成记录"（提交链/门禁/协议定稿/偏差/批次5 接手；**显式记录**：质量账三本合一面板（批次3挂账）延至批次5 或后续；设计 §5.6 的 `/api/v1/artifacts` 前缀系设计笔误，实装 `/api/artifacts`（63号 §5.6 勘误））

---

## 验收口径（批次4 完成定义）

1. 三通道 Fake/单测级全通：dense（含 ANN SQL 形态回归）/keyword/fusion（RRF 名次正确）；dense 降级不报错。
2. 权限：非成员 404 反探测；rev0 不可见（检索只打 current_revision）；未发布制品给"未发布"错误。
3. MCP 契约：四种 query 形态透传钉死；表格 DSL 回归。
4. web 搜索框→转发端点→serving 链路（Mock 层）+ 阅读器跳页。
5. 门禁：Java mvn test 全绿+jar 校验；Python 差分零新增；MCP/前端全绿。
