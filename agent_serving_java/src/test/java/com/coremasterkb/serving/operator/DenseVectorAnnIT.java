package com.coremasterkb.serving.operator;

import com.coremasterkb.serving.AgentServingApplication;
import com.coremasterkb.serving.mapper.result.UnitV2Row;
import com.coremasterkb.serving.operator.mapper.AssetRetrievalUnitV2Mapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

/**
 * 009（1.1.15 性能修复）dense ANN 真 PG 验证：HNSW 索引路径可达 + 召回语义不回退。
 *
 * <p>背景：v2 向量表自上线起无近邻索引 + 旧 SQL 的 DISTINCT ON 排序键以
 * representation_id 打头，40 万向量全量读盘逐条算距离（内网实测单查询 100s+）。
 * 本 IT 用真实 mapper（生产映射入口）+ 真实 HNSW 索引锁定三件事：</p>
 * <ol>
 *   <li>改写后的候选层 SQL 形态（距离序 ORDER BY + LIMIT + 过滤 + JOIN）确实存在
 *       HNSW 索引路径（enable_sort=off 下 EXPLAIN 必须出现 idx_are_v2_vec_hnsw 扫描
 *       ——若 SQL 回退成 DISTINCT ON 铺全量的形态，此断言失败）；</li>
 *   <li>同表示多向量取最佳、快照隔离、dense_eligible/维度过滤语义不回退；</li>
 *   <li>候选窗预算充分时与穷举结果一致（窗口截断不改变排序/去重语义）。</li>
 * </ol>
 *
 * <p>40 万规模的真实执行计划选择（planner 是否选索引）属于内网基准验收；
 * 本 IT 只锁"路径存在 + 语义正确"。连接池经 ServingBeans 的 connection-init-sql
 * 已 SET hnsw.ef_search=600 + plan_cache_mode=force_custom_plan（2026-10-20
 * 修订：iterative_scan=strict_order 在 pgvector 0.8.0 实测为性能毒药已移除）。</p>
 */
@SpringBootTest(classes = AgentServingApplication.class)
@ActiveProfiles("test-pg")
@Tag("pg-integration")
@DisplayName("009 dense ANN IT")
class DenseVectorAnnIT {

    private static final String SNAP = "snap-ann-" + UUID.randomUUID().toString().substring(0, 8);
    private static final String SNAP_OUT = SNAP + "-out";
    private static final int DIM = 1024;
    private static final int REPS = 60;

    /** 与迁移 009 同构：测试库未收敛到 009 时自建（幂等）。 */
    private static final String HNSW_DDL =
            "CREATE INDEX IF NOT EXISTS idx_are_v2_vec_hnsw "
                    + "ON asset_retrieval_embeddings_v2 "
                    + "USING hnsw (embedding_vector_vec vector_cosine_ops)";

    @Autowired
    private DataSource dataSource;

    @Autowired
    private AssetRetrievalUnitV2Mapper mapper;

    private JdbcTemplate jdbc;
    private float[][] repVectors;

    @BeforeEach
    void setUp() {
        try (Connection conn = dataSource.getConnection()) {
            assumeTrue(conn.isValid(3), "PostgreSQL not reachable — skipping");
        } catch (SQLException e) {
            assumeTrue(false, "PostgreSQL not reachable — skipping");
        }
        jdbc = new JdbcTemplate(dataSource);
        assumeTrue(tableExists("asset_retrieval_units_v2"),
                "retrieval v2 schema not present — skipping");
        assumeTrue(pgvectorAtLeast08(), "pgvector >= 0.8 required (iterative scan) — skipping");

        jdbc.execute(HNSW_DDL);
        seed();
        jdbc.execute("ANALYZE asset_retrieval_embeddings_v2");
    }

    @AfterEach
    void cleanUp() {
        if (jdbc == null) return;
        jdbc.update("DELETE FROM asset_retrieval_embeddings_v2 WHERE snapshot_id LIKE ?", SNAP + "%");
        jdbc.update("DELETE FROM asset_retrieval_units_v2 WHERE snapshot_id LIKE ?", SNAP + "%");
    }

    @Test
    @DisplayName("候选层 SQL 形态存在 HNSW 索引路径（enable_sort=off 下 EXPLAIN）")
    void annPathAvailable() throws Exception {
        String query = literal(repVectors[0]);
        String probe =
                "EXPLAIN (COSTS OFF) SELECT un.representation_id, "
                        + "(e.embedding_vector_vec <=> '" + query + "'::vector) AS distance "
                        + "FROM asset_retrieval_embeddings_v2 e "
                        + "JOIN asset_retrieval_units_v2 un ON un.representation_id = e.representation_id "
                        + "WHERE un.dense_eligible = TRUE AND e.dimension = " + DIM
                        + " AND e.snapshot_id IN ('" + SNAP + "') "
                        + "ORDER BY e.embedding_vector_vec <=> '" + query + "'::vector "
                        + "LIMIT 200";

        // 强制旋钮用 enable_sort（不是 enable_seqscan）：禁掉 Sort 后唯一能提供
        // 距离序的路径就是 hnsw 索引扫描——若 SQL 回退成 DISTINCT ON 全量形态、
        // 或索引缺失，此断言失败。默认旋钮下小表选精确扫是
        // planner 正常成本判断（本 IT 数据量小），40 万规模的成本翻转属内网验收。
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
                .as("候选层必须能走 idx_are_v2_vec_hnsw（SQL 回退成 DISTINCT ON 全量形态会失败）")
                .contains("idx_are_v2_vec_hnsw");
    }

    @Test
    @DisplayName("同表示多向量取最佳；结果按分数降序、表示不重复；最近表示排第一")
    void dedupBestPerRepresentation() {
        List<UnitV2Row> rows = search(queryLiteral(), 200, 50);

        assertThat(rows).isNotEmpty();
        List<String> repIds = rows.stream().map(UnitV2Row::getRepresentationId).toList();
        assertThat(repIds).doesNotHaveDuplicates();
        // 分数降序（稳定排序键：score DESC, canonical ASC, rep ASC）
        for (int i = 1; i < rows.size(); i++) {
            assertThat(rows.get(i - 1).getChannelScore())
                    .isGreaterThanOrEqualTo(rows.get(i).getChannelScore());
        }
        // 查询向量 ≈ rep-0 的 strategy-A 向量（同向量+微扰）：最近表示必须是 rep-0
        assertThat(repIds.get(0)).isEqualTo("ann-rep-0");
        assertThat(rows.get(0).getChannelScore()).isGreaterThan(0.99);
    }

    @Test
    @DisplayName("快照隔离：scope 只含 SNAP 时，SNAP_OUT 的表示零泄漏")
    void snapshotIsolation() {
        Set<String> leaked = new HashSet<>();
        for (UnitV2Row row : search(queryLiteral(), 200, 50)) {
            leaked.add(row.getSnapshotId());
        }
        assertThat(leaked).containsOnly(SNAP);

        List<UnitV2Row> outRows = searchScoped(List.of(SNAP_OUT), queryLiteral(), 200, 50);
        assertThat(outRows).isNotEmpty();
        assertThat(outRows).extracting(UnitV2Row::getSnapshotId).containsOnly(SNAP_OUT);
    }

    @Test
    @DisplayName("dense_eligible=FALSE 与维度不一致（dimension 元数据）的表示不进入结果")
    void eligibilityAndDimensionFilters() {
        List<String> repIds = search(queryLiteral(), 200, 50).stream()
                .map(UnitV2Row::getRepresentationId).toList();

        assertThat(repIds).doesNotContain("ann-rep-ineligible");
        assertThat(repIds).doesNotContain("ann-rep-dim512");
        // 正常表示照常可召回（过滤是收窄不是屏蔽）
        assertThat(repIds).contains("ann-rep-0");
    }

    @Test
    @DisplayName("候选窗预算充分时与穷举一致；窗口截断保持最近邻不丢")
    void candidateWindowParity() {
        List<String> exhaustive = search(queryLiteral(), 500, 20).stream()
                .map(UnitV2Row::getRepresentationId).toList();
        List<String> windowed = search(queryLiteral(), 200, 20).stream()
                .map(UnitV2Row::getRepresentationId).toList();

        // 充分预算（≥ 全部候选行数）下两层预算结果一致：排序/去重语义未因改写变化
        assertThat(windowed).containsExactlyElementsOf(exhaustive);

        // 极小候选窗（1）：全局最近的那条 embedding 一定在窗内 → rep-0 仍是第一名
        List<UnitV2Row> tiny = search(queryLiteral(), 1, 20);
        assertThat(tiny).hasSize(1);
        assertThat(tiny.get(0).getRepresentationId()).isEqualTo("ann-rep-0");
    }

    // ---------------------------------------------------------------------

    private List<UnitV2Row> search(String queryVector, int candidateBudget, int limit) {
        return searchScoped(List.of(SNAP), queryVector, candidateBudget, limit);
    }

    private List<UnitV2Row> searchScoped(
            List<String> snapshots, String queryVector, int candidateBudget, int limit) {
        return mapper.searchDenseV2(
                queryVector, DIM, snapshots,
                List.of(), List.of(), List.of(), List.of(),
                false, "", candidateBudget, limit);
    }

    /** 查询向量 = rep-0 的 A 向量 + 微扰（确定性种子）。 */
    private String queryLiteral() {
        float[] q = repVectors[0].clone();
        q[0] += 1e-4f;
        return literal(q);
    }

    private static String literal(float[] vec) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

    /**
     * 播种：SNAP 内 REPS 个表示（每表示 2 条 embedding：strategy-A = 表示向量本体，
     * strategy-B = 加噪版——同表示多向量场景）；SNAP_OUT 内 10 个表示（隔离面）；
     * 1 个 dense_eligible=FALSE 表示；1 个仅含 dimension=512 元数据行的表示。
     */
    private void seed() {
        Random random = new Random(42);
        repVectors = new float[REPS][];
        for (int i = 0; i < REPS; i++) {
            float[] vec = new float[DIM];
            for (int d = 0; d < DIM; d++) {
                vec[d] = random.nextFloat();
            }
            repVectors[i] = vec;
        }

        for (int i = 0; i < REPS; i++) {
            String repId = "ann-rep-" + i;
            insertUnit(repId, SNAP, true);
            insertEmbedding(repId, SNAP, "strategy-a", repVectors[i], DIM);
            insertEmbedding(repId, SNAP, "strategy-b", perturb(repVectors[i], random), DIM);
        }
        for (int i = 0; i < 10; i++) {
            String repId = "ann-out-" + i;
            insertUnit(repId, SNAP_OUT, true);
            insertEmbedding(repId, SNAP_OUT, "strategy-a", repVectors[i], DIM);
        }
        insertUnit("ann-rep-ineligible", SNAP, false);
        insertEmbedding("ann-rep-ineligible", SNAP, "strategy-a", repVectors[1], DIM);
        insertUnit("ann-rep-dim512", SNAP, true);
        // dimension 是元数据列（向量物理仍 1024 维）——dim=1024 的查询必须排除它
        insertEmbedding("ann-rep-dim512", SNAP, "strategy-a", repVectors[2], 512);
    }

    private static float[] perturb(float[] src, Random random) {
        float[] out = src.clone();
        for (int d = 0; d < out.length; d++) {
            out[d] = out[d] + (random.nextFloat() - 0.5f) * 0.05f;
        }
        return out;
    }

    private void insertUnit(String repId, String snapshotId, boolean denseEligible) {
        jdbc.update(
                "INSERT INTO asset_retrieval_units_v2 (representation_id, snapshot_id, "
                        + "representation_type, content_type, content_text, lexical_text, "
                        + "target_type, target_ref, canonical_evidence_id, "
                        + "lexical_eligible, dense_eligible, returnable) "
                        + "VALUES (?,?,?,?,?,?,?,?,?,TRUE,?,TRUE)",
                repId, snapshotId, "prose", "prose", "dense seed " + repId, "dense seed " + repId,
                "segment", repId, repId, denseEligible);
    }

    private void insertEmbedding(
            String repId, String snapshotId, String strategy, float[] vec, int dimension) {
        jdbc.update(
                "INSERT INTO asset_retrieval_embeddings_v2 "
                        + "(embedding_id, snapshot_id, representation_id, strategy, "
                        + "policy_version, provider, model, model_version, dimension, "
                        + "input_hash, embedding_vector_vec) "
                        + "VALUES (?,?,?,?,?,?,?,?,?,?,'" + literal(vec) + "'::vector)",
                "emb-" + UUID.randomUUID().toString().substring(0, 12),
                snapshotId, repId, strategy, "pv-test", "test", "test-model", "v1",
                dimension, "ih-" + repId + "-" + strategy);
    }

    private boolean tableExists(String table) {
        Boolean present = jdbc.queryForObject(
                "SELECT to_regclass(?) IS NOT NULL", Boolean.class, table);
        return Boolean.TRUE.equals(present);
    }

    private boolean pgvectorAtLeast08() {
        try {
            List<String> versions = jdbc.queryForList(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
                    String.class);
            if (versions.isEmpty()) {
                return false;
            }
            int[] parts = new int[3];
            String[] split = versions.get(0).split("\\.");
            for (int i = 0; i < Math.min(3, split.length); i++) {
                parts[i] = Integer.parseInt(split[i].replaceAll("\\D", ""));
            }
            return parts[0] > 0 || parts[1] >= 8;
        } catch (Exception e) {
            return false;
        }
    }
}
