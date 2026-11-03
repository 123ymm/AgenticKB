package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.AgentServingApplication;
import com.coremasterkb.serving.infrastructure.EmbeddingClient;
import com.coremasterkb.serving.observability.ServingRuntimeSchemaInitializer;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import javax.sql.DataSource;
import java.lang.reflect.Field;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 63号批次5 wiki 制品检索真 PG 验收 IT（照 DenseVectorAnnIT 真库模式：真实
 * mapper（生产映射入口）+ 真 HNSW 索引 + 优雅跳过）。
 *
 * <p><b>覆盖面</b>（批次4 交付的 {@link ArtifactWikiSearchService} +
 * {@link WikiArtifactToolMapper} 全链路）：</p>
 * <ol>
 *   <li>dense 三近邻按余弦距离序 + 候选层 SQL 存在 HNSW 索引路径
 *       （enable_sort=off 下 EXPLAIN 含 idx_wiki_units_hnsw）；</li>
 *   <li>keyword ILIKE 子串：含关键词命中 / 不含不命中；</li>
 *   <li>fusion：两通道各造命中 → RRF(k=60,等权) 融合含双方且分次序正确；</li>
 *   <li>pending 单元不进 dense（post-filter 截断——向量再近也不可见）；</li>
 *   <li>非成员（无域绑定/无成员关系）→ artifact_not_found 404 反探测（走
 *       service.search 断言异常，单测已覆盖 service 层，此处验真库谓词折叠）；</li>
 *   <li>readPage 正文+frontmatter/provenance 解析 + links 双向单跳（含 broken）；</li>
 *   <li>card：pageCount=revision 快照 row_count（发布定格≠现存页数）+
 *       units embedding 四态计数。</li>
 * </ol>
 *
 * <p><b>一次性库口径</b>：独立库（默认 serving_wiki_test，避免与 Python 测试库
 * kb_db_test 互清）——@BeforeAll 用裸 JDBC 建最小表集（DDL 真相源 017/018，
 * 列集见 SCHEMA_DDL 注释）+ 直接建 HNSW 索引（018 ④段守卫在此简化）。库名护栏
 * {@code *_test} 后缀 + 显式排除 kb_db_test，本 IT 会 DROP/CREATE 表，绝不指向
 * 共享库。</p>
 *
 * <p><b>上下文就绪桩</b>：serving 启动链 fail-closed 校验
 * （ServingRuntimeSchemaInitializer）要求迁移 ledger 存在期望 marker 行，空库上
 * @SpringBootTest 上下文起不来。@BeforeAll（先于上下文加载）补一行 stub——版本与
 * 校验和常量从校验器反射读取，避免镜像清单刷新后漂移。本 IT 自建表不走迁移链，
 * stub 仅为通过就绪校验。</p>
 *
 * <p><b>EmbeddingClient 桩</b>：真库 IT 不打 llm_service——手工装配
 * {@code new ArtifactWikiSearchService(真mapper, 真mapper, mock)}，mock 只固定
 * 查询向量（e0），dense 通道其余全部走真 SQL。</p>
 */
@SpringBootTest(classes = AgentServingApplication.class)
@ActiveProfiles("test-pg")
@Tag("pg-integration")
@DisplayName("63号批次5 wiki 制品检索真库 IT")
class ArtifactWikiSearchIT {

    private static final int DIM = 1024;
    private static final int REVISION = 3;
    private static final String DOMAIN = "cloud_core_network";
    private static final String TOKEN = UUID.randomUUID().toString().substring(0, 8);
    private static final String ARTIFACT_ID = "wiki-it-" + TOKEN;
    private static final String REF = "art_" + ARTIFACT_ID;
    private static final String MEMBER_USERNAME = "wiki-it-alice-" + TOKEN;
    private static final String OUTSIDER_USERNAME = "wiki-it-mallory-" + TOKEN;
    private static final String MEMBER_USER_ID = "uid-" + TOKEN + "-1";
    private static final String OUTSIDER_USER_ID = "uid-" + TOKEN + "-2";

    /**
     * 已知几何：查询向量与页向量都取 e0-e1 平面单位向量（θ=0/0.6/1.2）——
     * 与查询向量的余弦距离严格递增，三近邻次序确定（无并列）。
     */
    private static final float[] QUERY_VEC = planeUnit(0.0);
    private static final float[] V1 = planeUnit(0.0);
    private static final float[] V2 = planeUnit(0.6);
    private static final float[] V3 = planeUnit(1.2);

    /** 页正文（keyword 通道素材：p1/p3 含"核心网切片"，仅 p3 含"争议"，p2 都不含）。 */
    private static final String CONTENT_P1 =
            "核心网切片是接入侧资源切分的关键机制，覆盖保障、带宽隔离与SLA分级。";
    private static final String CONTENT_P2 =
            "术语对照页：接入网、承载网、核心网与传输网的中英映射及缩写全表。";
    private static final String CONTENT_P3 =
            "核心网切片争议：静态配置与动态编排两种口径并存，投产前必须确认回退方案。";

    /** 与迁移 ledger.py LEDGER_DDL 同文（stub 就绪校验用）。 */
    private static final String LEDGER_DDL = """
            CREATE TABLE IF NOT EXISTS cmkb_schema_migrations (
                migration_id TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                app_version TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                details_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """;

    /**
     * 最小表集（真相源 databases/kb/schemas/，仅取被测 SQL 的 JOIN/谓词面）：
     * kb_artifacts/kb_artifact_revisions/kb_artifact_members = 017 全列；
     * kb_wiki_pages/kb_wiki_page_links/kb_wiki_page_units = 018 ③段全列；
     * kb_users = 001+006 谓词所需列（username/status/deleted_at）；
     * user_domains = 013 谓词所需列。keyId 恒 null → mcp 钥匙两表不需要。
     */
    private static final String[] SCHEMA_DDL = {
            // 017 kb_artifacts 全列
            """
            CREATE TABLE kb_artifacts (
                artifact_id      TEXT PRIMARY KEY,
                domain_id        TEXT NOT NULL,
                kind             TEXT NOT NULL DEFAULT 'table'
                                 CONSTRAINT ck_kb_artifacts_kind CHECK (kind IN ('table','wiki')),
                title            TEXT NOT NULL,
                description      TEXT NOT NULL DEFAULT '',
                usage_note       TEXT NOT NULL DEFAULT '',
                owner_user_id    TEXT NOT NULL,
                visibility       TEXT NOT NULL DEFAULT 'private'
                                 CONSTRAINT ck_kb_artifacts_visibility CHECK (visibility IN ('private','public')),
                status           TEXT NOT NULL DEFAULT 'draft'
                                 CONSTRAINT ck_kb_artifacts_status CHECK (status IN ('draft','published','deprecated')),
                current_revision INTEGER NOT NULL DEFAULT 0,
                draft_open       BOOLEAN NOT NULL DEFAULT FALSE,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            // 017 kb_artifact_revisions 全列（card 的 row_count=发布定格页数来源）
            """
            CREATE TABLE kb_artifact_revisions (
                artifact_id    TEXT NOT NULL,
                revision       INTEGER NOT NULL,
                schema_json    JSONB NOT NULL,
                row_count      INTEGER NOT NULL,
                change_summary TEXT NOT NULL DEFAULT '',
                published_by   TEXT NOT NULL,
                published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (artifact_id, revision)
            )
            """,
            // 017 kb_artifact_members 全列（谓词 m.role 腿）
            """
            CREATE TABLE kb_artifact_members (
                artifact_id TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'viewer'
                            CONSTRAINT ck_kb_artifact_members_role CHECK (role IN ('owner','editor','viewer')),
                PRIMARY KEY (artifact_id, user_id)
            )
            """,
            // 001+006 最小列集：selectVisibleArtifact 只消费 username/status/deleted_at
            """
            CREATE TABLE kb_users (
                id         TEXT PRIMARY KEY,
                username   TEXT NOT NULL UNIQUE,
                status     TEXT NOT NULL DEFAULT 'active'
                           CONSTRAINT ck_kb_users_status CHECK (status IN ('active','disabled')),
                deleted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            // 013 最小列集：谓词 EXISTS (user_domains) 域绑定腿
            """
            CREATE TABLE user_domains (
                user_id     TEXT NOT NULL,
                domain      TEXT NOT NULL,
                domain_role TEXT NOT NULL DEFAULT 'member',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, domain)
            )
            """,
            // 018 ③段 kb_wiki_pages 全列
            """
            CREATE TABLE kb_wiki_pages (
                artifact_id TEXT NOT NULL,
                revision    INTEGER NOT NULL,
                page_key    TEXT NOT NULL,
                title       TEXT NOT NULL,
                page_type   TEXT NOT NULL DEFAULT 'topic'
                            CONSTRAINT ck_wiki_pages_type
                            CHECK (page_type IN ('topic','glossary','contradiction','open_question')),
                frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
                body_md     TEXT NOT NULL DEFAULT '',
                origin      TEXT NOT NULL DEFAULT 'machine'
                            CONSTRAINT ck_wiki_pages_origin CHECK (origin IN ('machine','manual')),
                provenance  JSONB NOT NULL DEFAULT '[]'::jsonb,
                flags       JSONB NOT NULL DEFAULT '{}'::jsonb,
                ordinal     INTEGER NOT NULL DEFAULT 0,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_by  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (artifact_id, revision, page_key)
            )
            """,
            // 018 ③段 kb_wiki_page_links 全列
            """
            CREATE TABLE kb_wiki_page_links (
                artifact_id TEXT NOT NULL,
                revision    INTEGER NOT NULL,
                from_page   TEXT NOT NULL,
                to_page     TEXT NOT NULL,
                anchor_text TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'valid'
                            CONSTRAINT ck_wiki_links_status CHECK (status IN ('valid','broken')),
                PRIMARY KEY (artifact_id, revision, from_page, to_page, anchor_text)
            )
            """,
            // 018 ③段 kb_wiki_page_units 全列（embedding VECTOR(1024) + 三态 embed_status）
            """
            CREATE TABLE kb_wiki_page_units (
                artifact_id  TEXT NOT NULL,
                revision     INTEGER NOT NULL,
                unit_key     TEXT NOT NULL,
                page_key     TEXT NOT NULL,
                content_text TEXT NOT NULL,
                embedding    VECTOR(1024),
                embed_status TEXT NOT NULL DEFAULT 'pending'
                             CONSTRAINT ck_wiki_units_embed
                             CHECK (embed_status IN ('pending','done','failed')),
                PRIMARY KEY (artifact_id, revision, unit_key)
            )
            """,
    };

    /** 本 IT 自有表（DROP/CREATE 每次重跑收形；ledger 不在此列——上下文共享）。 */
    private static final String[] OWNED_TABLES = {
            "kb_wiki_page_units", "kb_wiki_page_links", "kb_wiki_pages",
            "kb_artifact_members", "kb_artifact_revisions", "kb_artifacts",
            "user_domains", "kb_users",
    };

    @Autowired
    private DataSource dataSource;

    @Autowired
    private WikiArtifactToolMapper wikiMapper;

    @Autowired
    private ArtifactToolMapper artifactMapper;

    private JdbcTemplate jdbc;
    private EmbeddingClient embeddingClient;
    private ArtifactWikiSearchService service;

    // ------------------------------------------------------------------ 建库

    /**
     * 一次性库自举：护栏 → pgvector → ledger stub → 最小表集 → HNSW。
     * static @BeforeAll 先于 Spring 上下文加载执行——ledger stub 必须赶在
     * ServingRuntimeSchemaInitializer 的 fail-closed 校验前落库。
     */
    @BeforeAll
    static void createOneOffSchema() throws Exception {
        String host = env("PG_HOST", "localhost");
        String port = env("PG_PORT", "15432");
        String db = env("PG_DBNAME", "test_db");
        String url = "jdbc:postgresql://" + host + ":" + port + "/" + db;

        // 护栏（铁律）：本 IT 会 DROP/CREATE 表——只允许 *_test 一次性库，
        // 且显式排除 Python 测试库 kb_db_test（同以 _test 结尾但不归 Java IT 管）。
        assumeTrue(db.endsWith("_test") && !"kb_db_test".equals(db),
                "PG_DBNAME 必须为 *_test 一次性库（当前=" + db + "）— skipping");

        try (Connection conn = DriverManager.getConnection(
                url, env("PG_USER", "zdy"), env("PG_PASSWORD", "zdy1234"))) {
            assumeTrue(conn.isValid(3), "PostgreSQL not reachable — skipping");
        } catch (SQLException e) {
            assumeTrue(false, "PostgreSQL not reachable — skipping");
        }

        try (Connection conn = DriverManager.getConnection(
                url, env("PG_USER", "zdy"), env("PG_PASSWORD", "zdy1234"));
             Statement st = conn.createStatement()) {

            // pgvector 可用性（VECTOR 列 + HNSW 前置）：不可用即跳过（与 DenseVectorAnnIT 同口径）
            try {
                st.execute("CREATE EXTENSION IF NOT EXISTS vector");
            } catch (SQLException e) {
                assumeTrue(false, "pgvector 扩展不可用 — skipping: " + e.getMessage());
            }
            assumeTrue(pgvectorAtLeast08(st), "pgvector >= 0.8 required — skipping");

            // ledger stub：反射读取启动校验常量，镜像清单刷新不需同步本 IT
            String version = schemaInitializerConstant("EXPECTED_SCHEMA_VERSION");
            String checksum = schemaInitializerConstant("EXPECTED_SCHEMA_CHECKSUM");
            st.execute(LEDGER_DDL);
            st.execute("INSERT INTO cmkb_schema_migrations "
                    + "(migration_id, checksum, app_version, details_json) VALUES ('schema/" + version
                    + "', '" + checksum + "', 'wiki-it-stub', "
                    + "'{\"schema_version\":\"" + version + "\"}'::jsonb) "
                    + "ON CONFLICT (migration_id) DO NOTHING");

            // 自有表重建（重跑收形；DDL 异常直接抛——真问题不该静默跳过）
            for (String table : OWNED_TABLES) {
                st.execute("DROP TABLE IF EXISTS " + table + " CASCADE");
            }
            for (String ddl : SCHEMA_DDL) {
                st.execute(ddl);
            }

            // HNSW（018 ④段守卫简化为直接建；串行口径照 009 教训——容器 64MB /dev/shm）
            st.execute("SET max_parallel_maintenance_workers = 0");
            st.execute("SET maintenance_work_mem = '256MB'");
            st.execute("CREATE INDEX idx_wiki_units_hnsw "
                    + "ON kb_wiki_page_units USING hnsw (embedding vector_cosine_ops)");
        }
    }

    @BeforeEach
    void setUp() {
        jdbc = new JdbcTemplate(dataSource);
        for (String table : OWNED_TABLES) {
            jdbc.update("DELETE FROM " + table);
        }
        embeddingClient = mock(EmbeddingClient.class);
        when(embeddingClient.isConfigured()).thenReturn(true);
        when(embeddingClient.embed(anyString())).thenReturn(QUERY_VEC);
        // 手工装配：mapper 走 Spring 上下文真 MyBatis bean，仅 embedding 打桩
        service = new ArtifactWikiSearchService(wikiMapper, artifactMapper, embeddingClient);
    }

    // ------------------------------------------------------------------ dense

    @Test
    @DisplayName("dense 三近邻按余弦距离序：θ=0 页 score=1.0 居首，严格递减，title 从 pages JOIN")
    void denseThreeNeighborsByDistance() {
        seedBaseFixture();

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "切片", "dense", 10, DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(out.degradedDense()).isFalse();
        assertThat(out.revision()).isEqualTo(REVISION);
        assertThat(out.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::pageKey)
                .containsExactly("p1", "p2", "p3");
        assertThat(out.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("u1", "u2", "u3");
        // 查询向量与 p1 向量重合（θ=0）→ 余弦距离 0 → score=1.0
        assertThat(out.results().get(0).score()).isCloseTo(1.0, within(1e-6));
        // 距离序 → 分数严格递减
        for (int i = 1; i < out.results().size(); i++) {
            assertThat(out.results().get(i - 1).score())
                    .isGreaterThan(out.results().get(i).score());
        }
        // title 由外层 JOIN kb_wiki_pages 取得
        assertThat(out.results().get(0).title()).isEqualTo("切片总览");
        assertThat(out.results().get(0).channels()).containsExactly("wiki_dense");
        assertThat(out.results().get(0).snippet()).contains("核心网切片");
    }

    @Test
    @DisplayName("dense 候选层 SQL 存在 HNSW 索引路径（enable_sort=off 下 EXPLAIN 含 idx_wiki_units_hnsw）")
    void denseHnswIndexPathAvailable() throws Exception {
        seedBaseFixture();
        String q = literal(QUERY_VEC);
        // 与 WikiArtifactToolMapper.xml searchWikiUnitsDense 候选层同形态
        //（最内层：收窄 + embed_status 过滤 + 距离序 LIMIT——HNSW 的标准入口形态）
        String probe = "EXPLAIN (COSTS OFF) SELECT unit_key, page_key, content_text, "
                + "artifact_id, revision, (embedding <=> '" + q + "'::vector) AS distance "
                + "FROM kb_wiki_page_units "
                + "WHERE artifact_id = '" + ARTIFACT_ID + "' AND revision = " + REVISION
                + " AND embed_status = 'done' "
                + "ORDER BY embedding <=> '" + q + "'::vector LIMIT 100";

        // 照 DenseVectorAnnIT 口径：enable_sort=off 禁掉 Sort 后，唯一能提供距离序的
        // 路径就是 HNSW 索引扫描；小表默认旋钮选精确扫是 planner 正常成本判断。
        StringBuilder plan = new StringBuilder();
        try (Connection conn = dataSource.getConnection();
             Statement st = conn.createStatement()) {
            st.execute("SET enable_sort = off");
            try {
                try (ResultSet rs = st.executeQuery(probe)) {
                    while (rs.next()) {
                        plan.append(rs.getString(1)).append('\n');
                    }
                }
            } finally {
                st.execute("RESET enable_sort");
            }
        }

        assertThat(plan.toString())
                .as("候选层必须能走 idx_wiki_units_hnsw（SQL 回退成全量形态或索引缺失会失败）")
                .contains("idx_wiki_units_hnsw");
    }

    // ------------------------------------------------------------------ keyword

    @Test
    @DisplayName("keyword ILIKE：含关键词页命中（字典序稳定）、不含页不命中；零 embedding 调用")
    void keywordIlikeHitAndMiss() {
        seedBaseFixture();

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "核心网切片", "keyword", 10, DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(out.degradedDense()).isFalse();
        // p1/p3 含"核心网切片"，p2 不含；稳定序 = (page_key, unit_key) 字典序
        assertThat(out.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::pageKey)
                .containsExactly("p1", "p3");
        assertThat(out.results())
                .allSatisfy(h -> {
                    assertThat(h.score()).isEqualTo(1.0);
                    assertThat(h.channels()).containsExactly("wiki_keyword");
                    assertThat(h.title()).isNotBlank();
                });
        // keyword 单通道不发 embedding 调用（IT 层复验降级省成本口径）
        verify(embeddingClient, never()).embed(anyString());
    }

    // ------------------------------------------------------------------ fusion

    @Test
    @DisplayName("fusion：dense(p1,p2,p3) × keyword(仅p3) → RRF 融合 p3 居首（双通道命中）且分次序正确")
    void fusionRrfCombinesBothChannels() {
        seedBaseFixture();

        // "争议"仅 p3 含 → keyword 通道单命中；dense 通道按距离序 p1,p2,p3
        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "争议", "fusion", 10, DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(out.degradedDense()).isFalse();
        // RRF(k=60,等权)：p3=1/63+1/61 > p1=1/61 > p2=1/62 —— 融合结果含双方通道命中
        assertThat(out.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::pageKey)
                .containsExactly("p3", "p1", "p2");
        assertThat(out.results().get(0).channels())
                .containsExactlyInAnyOrder("wiki_dense", "wiki_keyword");
        assertThat(out.results().get(0).score())
                .isCloseTo(1.0 / 63 + 1.0 / 61, within(1e-12));
        assertThat(out.results().get(1).channels()).containsExactly("wiki_dense");
    }

    // ------------------------------------------------------------------ pending 过滤

    @Test
    @DisplayName("pending 单元不进 dense：与查询向量重合（距离 0）也不命中（post-filter 截断语义）")
    void pendingUnitExcludedFromDense() {
        seedBaseFixture();
        insertPage("p4", "待嵌入页", "topic", "{}", "# 待嵌入", "[]");
        // pending 单元向量 = 查询向量本体：全局最近，但 embed_status 过滤必须截掉
        insertUnit("u4", "p4", "pending", QUERY_VEC);

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "切片", "dense", 10, DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(out.degradedDense()).isFalse();
        assertThat(out.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::pageKey)
                .containsExactly("p1", "p2", "p3");
    }

    // ------------------------------------------------------------------ 权限反探测

    @Test
    @DisplayName("非成员（有账号但无域绑定/无成员关系）→ artifact_not_found；同用户 admin 分支放行（正向对照）")
    void nonMember404Counterprobe() {
        seedBaseFixture();

        // outsider 有 active 账号但绑在别的域：u.id 非空、域绑定腿落空 → 谓词空手
        assertThatThrownBy(() -> service.search(
                REF, "切片", "fusion", null, DOMAIN, OUTSIDER_USERNAME, false, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");

        // 正向对照：同一 outsider 走站点 admin 分支 → 放行（证明空手确因成员/域绑定腿）
        ArtifactWikiSearchService.WikiSearchView admin =
                service.search(REF, "核心网切片", "keyword", 10, DOMAIN, OUTSIDER_USERNAME, true, null);
        assertThat(admin.results())
                .extracting(ArtifactWikiSearchService.WikiSearchHit::pageKey)
                .contains("p1");
    }

    // ------------------------------------------------------------------ readPage / links

    @Test
    @DisplayName("readPage：正文+frontmatter/provenance 解析+双向链接摘要；links 单跳 out/in 含 broken；幽灵页 404")
    void readPageAndLinksOneHop() {
        seedBaseFixture();
        insertLink("p1", "p2", "参见术语表", "valid");
        insertLink("p1", "ghost", "失效引用", "broken");
        insertLink("p3", "p1", "回链", "valid");

        ArtifactWikiSearchService.WikiPageView view =
                service.readPage(REF, "p1", DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(view.revision()).isEqualTo(REVISION);
        ArtifactWikiSearchService.WikiPageDetail page = view.page();
        assertThat(page.pageKey()).isEqualTo("p1");
        assertThat(page.title()).isEqualTo("切片总览");
        assertThat(page.pageType()).isEqualTo("topic");
        assertThat(page.frontmatter()).containsEntry("tags", List.of("切片"));
        assertThat(page.bodyMd()).contains("核心网切片");
        assertThat(page.provenance()).hasSize(1);
        assertThat(page.origin()).isEqualTo("machine");
        assertThat(page.updatedAt()).isNotNull();

        // readPage 链接摘要：out 两条（ghost < p2 字典序，含 broken）、in 一条
        assertThat(view.links().out())
                .extracting(ArtifactWikiSearchService.WikiLinkItem::toPage)
                .containsExactly("ghost", "p2");
        assertThat(view.links().out().get(0).status()).isEqualTo("broken");
        assertThat(view.links().in()).hasSize(1);
        assertThat(view.links().in().get(0).fromPage()).isEqualTo("p3");

        // links 入口单跳：out 全量 / in 入链
        ArtifactWikiSearchService.WikiLinksView out =
                service.links(REF, "p1", "out", DOMAIN, MEMBER_USERNAME, false, null);
        assertThat(out.direction()).isEqualTo("out");
        assertThat(out.links()).hasSize(2);
        ArtifactWikiSearchService.WikiLinksView in =
                service.links(REF, "p1", "in", DOMAIN, MEMBER_USERNAME, false, null);
        assertThat(in.direction()).isEqualTo("in");
        assertThat(in.links())
                .extracting(ArtifactWikiSearchService.WikiLinkItem::fromPage)
                .containsExactly("p3");
        assertThat(in.links().get(0).anchorText()).isEqualTo("回链");

        // broken 链接目标不是页：readPage → page_not_found（404 typed）
        assertThatThrownBy(() -> service.readPage(REF, "ghost", DOMAIN, MEMBER_USERNAME, false, null))
                .isInstanceOf(StructureToolException.class)
                .satisfies(e -> assertThat(((StructureToolException) e).code())
                        .isEqualTo("page_not_found"));
    }

    // ------------------------------------------------------------------ card

    @Test
    @DisplayName("card：pageCount=revision 快照 row_count（定格≠现存 5 页）；embedding 四态计数（5/3/1/1）")
    void cardSnapshotAndEmbeddingProgress() {
        seedBaseFixture();
        insertPage("p4", "待嵌入页", "topic", "{}", "# 待嵌入", "[]");
        insertUnit("u4", "p4", "pending", QUERY_VEC);
        insertPage("p5", "失败页", "topic", "{}", "# 失败", "[]");
        insertUnit("u5", "p5", "failed", V2);

        ArtifactWikiSearchService.WikiArtifactCard card =
                service.card(REF, DOMAIN, MEMBER_USERNAME, false, null);

        assertThat(card.kind()).isEqualTo("wiki");
        assertThat(card.artifactRef()).isEqualTo(REF);
        assertThat(card.title()).isEqualTo("切片运维 wiki");
        assertThat(card.status()).isEqualTo("published");
        assertThat(card.revision()).isEqualTo(REVISION);
        // 页数取发布定格快照（row_count=3），不是现存物理页数（5）
        assertThat(card.pageCount()).isEqualTo(3);
        // units 四态聚计：total=5 done=3 pending=1 failed=1（真 FILTER COUNT 单趟）
        assertThat(card.embedding()).isEqualTo(
                new ArtifactWikiSearchService.WikiEmbeddingProgress(5L, 3L, 1L, 1L));
    }

    // ------------------------------------------------------------------ 播种

    /** 基础夹具：成员/非成员用户+域绑定、published wiki 制品（rev3、快照 row_count=3）、3 页 3 done 单元。 */
    private void seedBaseFixture() {
        jdbc.update("INSERT INTO kb_users (id, username) VALUES (?,?)",
                MEMBER_USER_ID, MEMBER_USERNAME);
        jdbc.update("INSERT INTO kb_users (id, username) VALUES (?,?)",
                OUTSIDER_USER_ID, OUTSIDER_USERNAME);
        jdbc.update("INSERT INTO user_domains (user_id, domain) VALUES (?,?)",
                MEMBER_USER_ID, DOMAIN);
        // outsider 绑在别的域：账号解析成功但 EXISTS user_domains 域绑定腿落空
        jdbc.update("INSERT INTO user_domains (user_id, domain) VALUES (?,?)",
                OUTSIDER_USER_ID, "other-domain");
        jdbc.update("INSERT INTO kb_artifacts (artifact_id, domain_id, kind, title, "
                        + "owner_user_id, visibility, status, current_revision) "
                        + "VALUES (?,?,?,?,?,'private','published',?)",
                ARTIFACT_ID, DOMAIN, "wiki", "切片运维 wiki", MEMBER_USER_ID, REVISION);
        jdbc.update("INSERT INTO kb_artifact_revisions (artifact_id, revision, schema_json, "
                        + "row_count, change_summary, published_by) VALUES (?,?,'{}'::jsonb,?,?,?)",
                ARTIFACT_ID, REVISION, 3, "IT 发布", MEMBER_USER_ID);
        // 成员行（editor）兼测谓词 m.role 腿；owner_user_id 腿由制品行兜底
        jdbc.update("INSERT INTO kb_artifact_members (artifact_id, user_id, role) VALUES (?,?,'editor')",
                ARTIFACT_ID, MEMBER_USER_ID);

        insertPage("p1", "切片总览", "topic",
                "{\"tags\":[\"切片\"],\"aliases\":[\"slice\"]}",
                "# 核心网切片总览\n\n覆盖保障、带宽隔离与 SLA 分级。", "[{\"kb\":\"kb-1\",\"doc\":\"doc-9\"}]");
        insertPage("p2", "术语对照", "glossary", "{\"tags\":[\"术语\"]}",
                "# 术语对照\n\n接入网 / 承载网 / 核心网。", "[]");
        insertPage("p3", "口径矛盾", "contradiction", "{}",
                "# 口径矛盾\n\n静态配置 vs 动态编排。", "[]");
        insertUnit("u1", "p1", "done", V1);
        insertUnit("u2", "p2", "done", V2);
        insertUnit("u3", "p3", "done", V3);
    }

    private void insertPage(String pageKey, String title, String pageType,
                            String frontmatterJson, String bodyMd, String provenanceJson) {
        jdbc.update("INSERT INTO kb_wiki_pages (artifact_id, revision, page_key, title, "
                        + "page_type, frontmatter, body_md, origin, provenance, flags, ordinal, updated_by) "
                        + "VALUES (?,?,?,?,?,?::jsonb,?,'machine',?::jsonb,'{}'::jsonb,0,'wiki-it')",
                ARTIFACT_ID, REVISION, pageKey, title, pageType, frontmatterJson,
                bodyMd, provenanceJson);
    }

    private void insertUnit(String unitKey, String pageKey, String embedStatus, float[] vec) {
        jdbc.update("INSERT INTO kb_wiki_page_units (artifact_id, revision, unit_key, page_key, "
                        + "content_text, embedding, embed_status) VALUES (?,?,?,?,?,'" + literal(vec) + "'::vector,?)",
                ARTIFACT_ID, REVISION, unitKey, pageKey, contentOf(pageKey), embedStatus);
    }

    private void insertLink(String fromPage, String toPage, String anchor, String status) {
        jdbc.update("INSERT INTO kb_wiki_page_links (artifact_id, revision, from_page, "
                        + "to_page, anchor_text, status) VALUES (?,?,?,?,?,?)",
                ARTIFACT_ID, REVISION, fromPage, toPage, anchor, status);
    }

    private static String contentOf(String pageKey) {
        return switch (pageKey) {
            case "p1" -> CONTENT_P1;
            case "p2" -> CONTENT_P2;
            case "p3" -> CONTENT_P3;
            // 附加页（p4/p5）正文不参与 keyword 断言，取确定性占位即可
            default -> "wiki-it 附加页正文 " + pageKey;
        };
    }

    // ------------------------------------------------------------------ 工具

    /** e0-e1 平面单位向量（其余维 0）：与 θ=0 查询向量的余弦距离 = 1-cos(θ)。 */
    private static float[] planeUnit(double theta) {
        float[] v = new float[DIM];
        v[0] = (float) Math.cos(theta);
        v[1] = (float) Math.sin(theta);
        return v;
    }

    /** pgvector literal（照 DenseVectorAnnIT：float 展开精度足够 ANN）。 */
    private static String literal(float[] vec) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

    private static boolean pgvectorAtLeast08(Statement st) throws SQLException {
        try (ResultSet rs = st.executeQuery(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'")) {
            if (!rs.next()) {
                return false;
            }
            String[] parts = rs.getString(1).split("\\.");
            int[] ver = new int[2];
            for (int i = 0; i < Math.min(2, parts.length); i++) {
                ver[i] = Integer.parseInt(parts[i].replaceAll("\\D", ""));
            }
            return ver[0] > 0 || ver[1] >= 8;
        } catch (Exception e) {
            return false;
        }
    }

    /** 读启动校验器的私有常量（镜像清单刷新自动跟随，不需手工同步本 IT）。 */
    private static String schemaInitializerConstant(String name) throws Exception {
        Field field = ServingRuntimeSchemaInitializer.class.getDeclaredField(name);
        field.setAccessible(true);
        return (String) field.get(null);
    }

    private static String env(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isBlank() ? fallback : value;
    }
}
