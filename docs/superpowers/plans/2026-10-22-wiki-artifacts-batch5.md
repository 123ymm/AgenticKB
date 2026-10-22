# Wiki 制品批次5：E2E 验收 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 把批次1-4 全部 Mock/单测级的实现放到**真 PG（本地 pgvector 容器）**上验收——既有 PG 门控用例集真实执行、Java 检索 IT（HNSW 真索引）、全链验收测试——并交付 63号 §七 验收矩阵（可本地化项全过；真语料项给内网验收步骤清单）。

**Architecture:** 本地起独立 pgvector 容器（`pgvector/pgvector:0.8.0-pg15`，独立端口，**不碰正在运行的 codex-wiki-audit 容器与 cmkb**）；Python 侧用测试 conftest 既有的 env 预填（`PG_HOST/PG_PORT/PG_DBNAME=kb_db_test/PG_USER/PG_PASSWORD`）+ `KB_RUN_POSTGRES_ACCEPTANCE=1` + `KB_ALLOW_TEST_TRUNCATE=1` 在 a74b940（py3.11）跑 wiki PG 文件（test_wiki_db.py 等——py3.11 可收集；**test_artifacts_db.py 是 py3.12 语法（f-string 反斜杠），本地跑不了，留内网**）；Java 侧 `mvn verify` + 同名 env（application-test-pg.yml）跑 pg-integration IT。**py3.12 全量真库预演**留内网 apply 前步骤（1.1.16 发版清单）。

**Tech Stack:** 现有全部；零新依赖、零 DDL、零生产代码改动（本批次以跑测试+修真库暴露的问题为主）。

**设计依据:** 63号 §七 验收 1-7 + 批次4 完成记录接手清单（真库 E2E 是批次5 主战场/jar 已校验/MCP 顶层浏览验一下）。

---

## 铁律（同前批次）+ 本批次特定

- pgvector 容器名带 `wiki-b5` 前缀独占端口，用完 `docker rm -f` 清理；不动 codex-wiki-audit-*/cmkb。
- 真库暴露的实现 bug：修实现不修测试（除非测试断言本身错了——裁定权在控制器）。
- git add 只写显式路径；生产代码改动须有对应回归测试。

---

## Task 1: 本地真库环境 + 既有 PG 门控用例集真实执行

**Steps:**
- [x] 起容器：`docker run -d --name wiki-b5-pg -e POSTGRES_USER=zdy -e POSTGRES_PASSWORD=zdy1234 -e POSTGRES_DB=kb_db_test -p 55441:5432 pgvector/pgvector:0.8.0-pg15`（等就绪）
- [x] 首跑（建库建表走 conftest `_ensure_schema`/ensure_schema）：`MSYS_NO_PATHCONV=1 docker run --rm --network host? 否——容器互访：run 加 `--add-host=host.docker.internal:host-gateway` 或直接用宿主 IP；PG_HOST 指向宿主端口 55441`；命令模板：
  ```
  docker run --rm -v "<WT>:/w" -w /w -e PG_HOST=host.docker.internal -e PG_PORT=55441 -e PG_DBNAME=kb_db_test -e PG_USER=zdy -e PG_PASSWORD=zdy1234 -e KB_RUN_POSTGRES_ACCEPTANCE=1 -e KB_ALLOW_TEST_TRUNCATE=1 --add-host=host.docker.internal:host-gateway agentickb-release-validation:a74b940 sh -c "pip install -q pytest pytest-asyncio && python -m pytest knowledge_mining/tests/kb/test_wiki_db.py knowledge_mining/tests/kb/test_wiki_sources.py knowledge_mining/tests/kb/test_wiki_clustering.py -q"
  ```
  （域隔离用例若需 MINING_TEST_DOMAIN_PG_* 会自动 skip，不计失败）
- [x] 逐文件跑绿；暴露的失败逐个修（实现 bug→修码+回归；测试断言错→改断言并记录）
- [x] 把 test_wiki_compile.py（纯 Fake）+ kb 全套在真库 env 下再跑一遍（无 PG 依赖用例不受影响，确认 env 不污染）
- [x] Commit（若有修复）：`fix(wiki): 真库执行暴露问题修复`；记录数字

## Task 2: Java ArtifactWikiSearchIT（真库 IT）

**Files:**
- Create: `agent_serving_java/src/test/java/com/coremasterkb/serving/structure/ArtifactWikiSearchIT.java`（`@Tag("pg-integration")`+`@ActiveProfiles("test-pg")`，照 DenseVectorAnnIT/TypedTableQueryIT 模式）
- Test 内容：一次性库（`*_test` 库名护栏照 AbstractPgIntegrationTest）建 kb_artifacts（published+current_revision=1）+pages+units（**真 1024 维向量 + 真 HNSW 建索引**——DDL 018 文本执行或最小建表+索引）→ ①dense 检索命中最近页且 **EXPLAIN 含 idx_wiki_units_hnsw**（照 DenseVectorAnnIT:133 模式）②keyword ILIKE 命中③fusion 双通道命中④embed_status='pending' 单元不被 dense 命中⑤非成员用户 404 反探测⑥page 读+links 单跳
- 跑法：`MSYS_NO_PATHCONV=1 docker run --rm -v "<WT>:/w" -w /w -v "C:\Users\fuzhi\.m2:/root/.m2" -e PG_HOST=host.docker.internal -e PG_PORT=55441 -e PG_DBNAME=serving_wiki_test -e PG_USER=zdy -e PG_PASSWORD=zdy1234 --add-host=host.docker.internal:host-gateway maven:3.9-eclipse-temurin-21 mvn -q -f agent_serving_java/pom.xml verify`（IT 库独立于 Python 的 kb_db_test，避免互清）
- [x] 跑绿+失败修复；Commit：`test(wiki-serving): ArtifactWikiSearchIT 真库三通道+HNSW EXPLAIN+权限反探测`

## Task 3: 全链验收测试 + 验收矩阵 + 完成记录

**Files:**
- Create: `knowledge_mining/tests/kb/test_wiki_acceptance.py`（PG 门控+Fake LLM/Fake task client；照 test_artifact_acceptance.py 61号九用例模式）——七验收口径的本地化五项：
  1. **版本机制回归**：建制品→绑源（Fake inputs）→Fake 编译落 rev0→发布（revision=1/row_count/页面清单快照/units 投影 pending）→改页（origin 翻 manual）→再发布（rev2 两版本并存）→回滚指针→discard
  2. **origin 保护**：rev0 预置 manual 页→重编译（Fake LLM 同键 machine 页）→manual 页内容逐字不变+conflicts 账
  3. **引用规范**：编译产页 provenance 非空/unit_id 全在绑定语料内/悬空入 flags/密度告警生效（Fake LLM 产预置正文）
  4. **检索就绪链**：发布→project_units→Fake embedding 写向量（update_unit_embeddings 直接写 1024 维）→units_status done→（Java 侧三通道由 T2 IT 验，此处验 Python 侧投影/状态/重投影幂等）
  5. **人工编辑闭环**：改页→origin manual→重编译不丢→再发布→该版 units 重建（新内容在 content_text）
- [x] 真库跑绿（同 T1 env）
- [x] 完成记录+**验收矩阵**（63号 §七 1-7 逐项：本地化五项=本地已过；丢事实率+真语料重跑稳定性=**内网验收步骤清单**（真 llm_service+真语料：编译→抽检 20 问题对照原文；同语料重编译 diff≈0——幂等键缓存已奠基）；检索三通道全链（web 框→mining→serving）=部署后 E2E 冒烟清单（cmkb 容器内 curl 三端点））
- [x] MCP 顶层浏览 wiki 卡片：cmkb 容器内 python 冒烟（get_knowledge art_ 卡片形态——若 cmkb 跑的是旧 jar 则记录为部署后步骤）
- [x] Commit：`test(wiki): 全链验收（版本/origin/引用/投影/编辑闭环）+批次5完成记录`

## 验收口径（批次5 完成定义）

1. 本地真库：wiki PG 门控用例集全绿（数字记录）；Java IT 全绿（含 HNSW EXPLAIN）。
2. 全链验收五项本地化用例绿。
3. 验收矩阵交付：1-7 逐项状态+内网/部署后步骤清单。
4. 清理：wiki-b5-pg 容器删除；无生产代码回归（全套件差分零新增）。

---

## 批次5 完成记录（2026-10-23 定稿）

**提交链**（基于批次4 完成点 9b2c972/220577a）：86daf74 计划 → febeb93 T1 真库基建修复（conftest `_truncate_all` 清单容错化——001 已退役表在全新建库上 TRUNCATE UndefinedTable；两处 PG 断言勘误；wiki PG 门控 50/50）→ ca58ddd T2 ArtifactWikiSearchIT（真库三通道+HNSW EXPLAIN+权限反探测，8/8）→ 本提交 T3 全链验收五用例+完成记录。

**门禁数字**：
- T1 wiki PG 门控（test_wiki_db 17 + test_wiki_sources 11 + test_wiki_clustering 22）**50/50**；
- T2 Java pg-integration `ArtifactWikiSearchIT` **8/8**（denseThreeNeighbors / denseHnswIndexPathAvailable（EXPLAIN 含 idx_wiki_units_hnsw）/ keywordIlike / fusionRrf / pendingUnitExcludedFromDense / nonMember404Counterprobe / readPageAndLinksOneHop / cardSnapshotAndEmbeddingProgress）；
- T3 `test_wiki_acceptance.py` **5/5**（真 PG 首跑即绿，14.85s）；
- wiki 面全套（test_wiki_* 9 文件含验收）：真库 env **158/158**（60s）；
- kb 全套件回归（真库 env，test_artifacts_db.py 系 py3.12 语法本地不收集、按计划留内网）：含/不含验收文件两跑在**同一先在挂点**截断（`test_user_lifecycle_integration.py` 并发用例——pool max=2 环境性死等，详见偏差①），截断前 609 项结果**逐项一致**（59f/530p/20e；20 错误全部为 test_ops_usage_queries fixture 错、59 失败均为先在环境性失败，失败清单零 wiki 面）——**差分零新增**成立。

**验收文件**（`knowledge_mining/tests/kb/test_wiki_acceptance.py`，PG 门控）：真装配（ArtifactDB/WikiDB/CompileInputsDB 同 async_pool + ArtifactService(adb, wiki=lifecycle)/WikiLifecycle/WikiService/CompileService）+ 两枚假件（FakeGenerationClient 页正文出队、`_FlatTaskClient` embedding 通道压平——向量由 update_unit_embeddings 直写 1024 维）。**边界裁定**（文件 docstring 已写明）：用例 1/4/5 的「编译产页」绕过完整 CompileService（39 用例已覆盖）以 `wdb.upsert_page(origin='machine')` 直写 rev0 模拟；用例 2/3（及用例 5 的重编译步）必须打真实现 `page_flags`/`write_machine_page`/`validate_pages_for_publish`。乐观锁全程真实（每操作取最新 updated_at 全精度令牌）。

**验收矩阵（63号 §七 1-7）**：

| # | 验收项 | 状态 | 证据 / 待办步骤 |
|---|--------|------|----------------|
| 1 | 丢事实率（编译质量） | **内网验收** | 真 llm_service+真语料：部署 1.1.16→绑真库目录→编译→抽检 20-50 真实问题对照原文（人工评定） |
| 2 | 重跑稳定性 | 本地已奠基+**内网复验** | 本地：幂等键四参冻结+聚类确定性（输入序固定）单测已钉死；内网：同语料重编译 machine 页 diff≈0（幂等键命中 llm_service 缓存） |
| 3 | 检索三通道 | 本地✅+**部署后冒烟** | 本地：Java IT 8/8（HNSW EXPLAIN 命中真索引）；部署后：web 阅读器搜索框 / mining 转发端点 / MCP query 三入口各 curl 一发 |
| 4 | 版本机制 | **本地✅** | 用例1：rev1/rev2 双版本并存、rev1 快照逐字不变、units 按版本保留、回滚纯指针（rev0/rev2 数据不动）、discard 清 rev0+draft_open=False |
| 5 | origin 保护 | **本地✅** | 用例2：人工改页后重编译 manual 页逐字不动+conflicts 账（记录形态对齐真管线）；machine 同键覆盖更新 |
| 6 | 引用规范 | **本地✅** | 用例3：悬空计数正确/密度告警置位/合法页零告警（真 page_flags）；发布前校验悬空 blocking→发布 publish_validation_failed（409 家族）零副作用；修复后放行、密度降 warning |
| 7 | 编辑闭环 | **本地✅** | 用例5：人工改页→origin manual→重编译不丢（真 write_machine_page conflict）→再发布→rev2 units content_text 含新内容（「改完能搜到」存储层证明） |

补充：用例4（units 投影链）覆盖 §七3 的 Python 侧——投影 pending→1024 维直写 done=2→超长页分节（>2000 字→`页A#sN` 多 unit）→重投影幂等→pending 不进 done 计数。

**内网/部署后步骤清单（1.1.16 发版配套）**：
1. py3.12 全量真库预演：`python -m pytest knowledge_mining/tests/kb -q`（含 test_artifacts_db.py——py3.11 容器不收集）+ 全量 kb 全套件（本地 env 挂点①内网 pool 配置下应不复现，若复现按偏差①排查）。
2. 真语料丢事实率抽检（§七1）：部署→绑定真库目录→编译→抽检 20-50 真实问题对照原文。
3. 真语料重跑稳定性（§七2）：同语料重编译，machine 页 diff≈0。
4. web/MCP 检索三入口冒烟（§七3 部署后半）：阅读器搜索框、mining `/api/artifacts/{id}/wiki/search`、MCP `{"artifact_id":..., "query": {...}}` 各一发。
5. MCP 顶层浏览 wiki 卡片：`get_knowledge()`（query=null）卡片区 wiki 形态（page_count+embedding 四态进度）——需 1.1.16 新链路部署后验（cmkb 现役 1.1.15 无 wiki 通道，本批次未冒烟，按计划记为部署后步骤）。
6. wiki-b5-pg 容器清理（控制器执行）：`docker rm -f wiki-b5-pg`。

**容器清理记录**：wiki-b5-pg（pgvector/pgvector:0.8.0-pg15，宿主 55441）本任务结束时仍在役（终局全量门禁可复用），**由控制器在批次5 全部收尾后删除**；全程未触碰 codex-wiki-audit-20261022-db 与 cmkb。

**偏差记录**：
1. **kb 全套件在本地真库 env 存在先在挂点**：`test_user_lifecycle_integration.py` 并发用例（截断点在 609/796——该组 `test_concurrent_*` 用例在 kb conftest pool max=2 下死等）。含/不含验收文件两跑在同一位置截断且前 609 项逐项一致，证明与本批次无关（验收文件为纯新增、字母序排在挂点之后）；该面修复属用户生命周期线，不阻断本批次。截断点之后的用例：wiki 面 158/158 已由单跑补齐；其余非 wiki 34 项（user_lifecycle 尾部）维持先在状态留内网全量预演覆盖。
2. test_artifacts_db.py 为 py3.12 语法（f-string 反斜杠），py3.11 验证容器不收集——按计划 Architecture 节既定口径留内网。
3. MCP 顶层浏览 wiki 卡片未在 cmkb 冒烟（现役 1.1.15 无 wiki 通道）——按计划记为部署后步骤，未碰 cmkb 容器。
