# Wiki 制品批次5：E2E 验收 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

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
- [ ] 起容器：`docker run -d --name wiki-b5-pg -e POSTGRES_USER=zdy -e POSTGRES_PASSWORD=zdy1234 -e POSTGRES_DB=kb_db_test -p 55441:5432 pgvector/pgvector:0.8.0-pg15`（等就绪）
- [ ] 首跑（建库建表走 conftest `_ensure_schema`/ensure_schema）：`MSYS_NO_PATHCONV=1 docker run --rm --network host? 否——容器互访：run 加 `--add-host=host.docker.internal:host-gateway` 或直接用宿主 IP；PG_HOST 指向宿主端口 55441`；命令模板：
  ```
  docker run --rm -v "<WT>:/w" -w /w -e PG_HOST=host.docker.internal -e PG_PORT=55441 -e PG_DBNAME=kb_db_test -e PG_USER=zdy -e PG_PASSWORD=zdy1234 -e KB_RUN_POSTGRES_ACCEPTANCE=1 -e KB_ALLOW_TEST_TRUNCATE=1 --add-host=host.docker.internal:host-gateway agentickb-release-validation:a74b940 sh -c "pip install -q pytest pytest-asyncio && python -m pytest knowledge_mining/tests/kb/test_wiki_db.py knowledge_mining/tests/kb/test_wiki_sources.py knowledge_mining/tests/kb/test_wiki_clustering.py -q"
  ```
  （域隔离用例若需 MINING_TEST_DOMAIN_PG_* 会自动 skip，不计失败）
- [ ] 逐文件跑绿；暴露的失败逐个修（实现 bug→修码+回归；测试断言错→改断言并记录）
- [ ] 把 test_wiki_compile.py（纯 Fake）+ kb 全套在真库 env 下再跑一遍（无 PG 依赖用例不受影响，确认 env 不污染）
- [ ] Commit（若有修复）：`fix(wiki): 真库执行暴露问题修复`；记录数字

## Task 2: Java ArtifactWikiSearchIT（真库 IT）

**Files:**
- Create: `agent_serving_java/src/test/java/com/coremasterkb/serving/structure/ArtifactWikiSearchIT.java`（`@Tag("pg-integration")`+`@ActiveProfiles("test-pg")`，照 DenseVectorAnnIT/TypedTableQueryIT 模式）
- Test 内容：一次性库（`*_test` 库名护栏照 AbstractPgIntegrationTest）建 kb_artifacts（published+current_revision=1）+pages+units（**真 1024 维向量 + 真 HNSW 建索引**——DDL 018 文本执行或最小建表+索引）→ ①dense 检索命中最近页且 **EXPLAIN 含 idx_wiki_units_hnsw**（照 DenseVectorAnnIT:133 模式）②keyword ILIKE 命中③fusion 双通道命中④embed_status='pending' 单元不被 dense 命中⑤非成员用户 404 反探测⑥page 读+links 单跳
- 跑法：`MSYS_NO_PATHCONV=1 docker run --rm -v "<WT>:/w" -w /w -v "C:\Users\fuzhi\.m2:/root/.m2" -e PG_HOST=host.docker.internal -e PG_PORT=55441 -e PG_DBNAME=serving_wiki_test -e PG_USER=zdy -e PG_PASSWORD=zdy1234 --add-host=host.docker.internal:host-gateway maven:3.9-eclipse-temurin-21 mvn -q -f agent_serving_java/pom.xml verify`（IT 库独立于 Python 的 kb_db_test，避免互清）
- [ ] 跑绿+失败修复；Commit：`test(wiki-serving): ArtifactWikiSearchIT 真库三通道+HNSW EXPLAIN+权限反探测`

## Task 3: 全链验收测试 + 验收矩阵 + 完成记录

**Files:**
- Create: `knowledge_mining/tests/kb/test_wiki_acceptance.py`（PG 门控+Fake LLM/Fake task client；照 test_artifact_acceptance.py 61号九用例模式）——七验收口径的本地化五项：
  1. **版本机制回归**：建制品→绑源（Fake inputs）→Fake 编译落 rev0→发布（revision=1/row_count/页面清单快照/units 投影 pending）→改页（origin 翻 manual）→再发布（rev2 两版本并存）→回滚指针→discard
  2. **origin 保护**：rev0 预置 manual 页→重编译（Fake LLM 同键 machine 页）→manual 页内容逐字不变+conflicts 账
  3. **引用规范**：编译产页 provenance 非空/unit_id 全在绑定语料内/悬空入 flags/密度告警生效（Fake LLM 产预置正文）
  4. **检索就绪链**：发布→project_units→Fake embedding 写向量（update_unit_embeddings 直接写 1024 维）→units_status done→（Java 侧三通道由 T2 IT 验，此处验 Python 侧投影/状态/重投影幂等）
  5. **人工编辑闭环**：改页→origin manual→重编译不丢→再发布→该版 units 重建（新内容在 content_text）
- [ ] 真库跑绿（同 T1 env）
- [ ] 完成记录+**验收矩阵**（63号 §七 1-7 逐项：本地化五项=本地已过；丢事实率+真语料重跑稳定性=**内网验收步骤清单**（真 llm_service+真语料：编译→抽检 20 问题对照原文；同语料重编译 diff≈0——幂等键缓存已奠基）；检索三通道全链（web 框→mining→serving）=部署后 E2E 冒烟清单（cmkb 容器内 curl 三端点））
- [ ] MCP 顶层浏览 wiki 卡片：cmkb 容器内 python 冒烟（get_knowledge art_ 卡片形态——若 cmkb 跑的是旧 jar 则记录为部署后步骤）
- [ ] Commit：`test(wiki): 全链验收（版本/origin/引用/投影/编辑闭环）+批次5完成记录`

## 验收口径（批次5 完成定义）

1. 本地真库：wiki PG 门控用例集全绿（数字记录）；Java IT 全绿（含 HNSW EXPLAIN）。
2. 全链验收五项本地化用例绿。
3. 验收矩阵交付：1-7 逐项状态+内网/部署后步骤清单。
4. 清理：wiki-b5-pg 容器删除；无生产代码回归（全套件差分零新增）。
