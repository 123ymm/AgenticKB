package com.coremasterkb.serving.operator;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * v2 召回 mapper 的 SQL 文本契约测试（批次8 R2）。PG 集成测试（AbstractPgIntegrationTest，
 * {@code mvn verify -Dgroups=pg-integration}）不可用时，这里锁定 SQL 关键语义：
 * eligibility 下推、'simple' + plainto_tsquery 同源契约、参数化 JSONB containment、
 * 维度一致、scope 在 LIMIT 前下推。
 */
@DisplayName("AssetRetrievalUnitV2Mapper SQL contract")
class AssetRetrievalUnitV2MapperXmlTest {

    private static String mapperXml() throws Exception {
        try (InputStream in = AssetRetrievalUnitV2MapperXmlTest.class.getClassLoader()
                .getResourceAsStream("mapper/AssetRetrievalUnitV2Mapper.xml")) {
            assertThat(in).as("mapper XML must be on classpath").isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    @DisplayName("fts searches the v2 units table, lexical_eligible only, before LIMIT")
    void ftsCoreContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("FROM asset_retrieval_units_v2");
        assertThat(xml).contains("lexical_eligible = TRUE");
        // 同源分词契约：'simple' 配置 + 参数化 plainto_tsquery（查询侧 jieba 预分词后喂入）
        assertThat(xml).contains("plainto_tsquery('simple', #{lexicalQuery})");
        assertThat(xml).contains("search_vector @@ plainto_tsquery");
        assertThat(xml).contains("ts_rank(search_vector");
        // scope/filters 在 Top-K 前下推：WHERE 内含 snapshot_id IN
        assertThat(xml).contains("snapshot_id IN");
    }

    @Test
    @DisplayName("hard filters push down as parameterized JSONB containment + typed IN lists")
    void filterPushdownContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("facets_json @&gt; #{docParam}::jsonb");
        assertThat(xml).contains("representation_type IN");
        assertThat(xml).contains("content_type IN");
        assertThat(xml).contains("target_ref IN");
        // 参数化：不含字符串拼接的 facets 过滤（没有 ${} 插值出现在这些过滤附近）
        assertThat(xml).doesNotContain("${docParam}");
        assertThat(xml).doesNotContain("${lexicalQuery}");
    }

    @Test
    @DisplayName("dense joins v2 embeddings with units, dense_eligible + dimension match")
    void denseCoreContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("FROM asset_retrieval_embeddings_v2 e");
        assertThat(xml).contains("JOIN asset_retrieval_units_v2 un");
        assertThat(xml).contains("un.dense_eligible = TRUE");
        assertThat(xml).contains("e.dimension = #{dim}");
        assertThat(xml).contains("e.embedding_vector_vec &lt;=&gt; #{queryVector}::vector");
        // 旧 unit_type / text_kind 映射不得再出现
        assertThat(xml).doesNotContain("unit_type");
        assertThat(xml).doesNotContain("text_kind");
    }

    @Test
    @DisplayName("009: dense 是 ANN 形态——距离序候选 LIMIT 先于 DISTINCT ON，两层预算独立")
    void denseAnnCandidateContract() throws Exception {
        String xml = mapperXml();

        // 候选层（最内层）：ORDER BY 距离 + LIMIT candidateBudget——HNSW(idx_are_v2_vec_hnsw)
        // + pgvector 迭代扫描的标准入口形态；距离表达式必须原样出现在 ORDER BY
        int orderByIdx = xml.indexOf("ORDER BY e.embedding_vector_vec &lt;=&gt; #{queryVector}::vector");
        int candidateLimitIdx = xml.indexOf("LIMIT #{candidateBudget}");
        assertThat(orderByIdx).as("候选层必须以距离表达式排序").isGreaterThan(-1);
        assertThat(candidateLimitIdx).as("候选层必须有 embedding 级预算 LIMIT").isGreaterThan(orderByIdx);

        // 去重层在候选层之上：DISTINCT ON 按表示取最佳距离（次序键是别名 distance，
        // 不再是向量表达式——防止去重层退化回全量距离物化）
        assertThat(xml).contains("DISTINCT ON (c.representation_id)");
        assertThat(xml).contains("ORDER BY c.representation_id, c.distance");
        assertThat(xml).contains("AS distance");

        // 表示层预算独立存在（LIMIT #{limit} 在去重层之后）
        int dedupIdx = xml.indexOf("ORDER BY c.representation_id, c.distance");
        int limitIdx = xml.indexOf("LIMIT #{limit}", dedupIdx);
        assertThat(limitIdx).isGreaterThan(dedupIdx);

        // 旧全扫形态不得回归：DISTINCT ON 直接铺全量 + representation_id 打头排序
        assertThat(xml).doesNotContain("ORDER BY e.representation_id, e.embedding_vector_vec");

        // 参数化：候选预算无 ${} 插值
        assertThat(xml).doesNotContain("${candidateBudget}");
    }

    @Test
    @DisplayName("canonical evidence id is selected for channel aggregation")
    void canonicalColumnSelected() throws Exception {
        String xml = mapperXml();
        assertThat(xml).contains("canonical_evidence_id");
    }
    @Test
    @DisplayName("A2: 章节范围以 section_ref 物理列下推，descendants 走 SQL 内递归闭包")
    void sectionScopeContract() throws Exception {
        String xml = mapperXml();

        // exact：section_ref IN 主匹配 + 存量 NULL 行 target_ref 回落（同一 predicate）
        assertThat(xml).contains("section_ref IN");
        assertThat(xml).contains("un.section_ref IS NULL AND un.target_ref IN");
        // descendants：SQL 内递归闭包（不做应用层 IN-list），FTS/dense 共用
        // P1-4/P2-15：闭包携带 (snapshot_id, ref) 对且只遍历 section 节点
        assertThat(xml).contains("WITH RECURSIVE sec(snapshot_id, ref)");
        assertThat(xml).contains("JOIN sec ON c.parent_ref = sec.ref");
        assertThat(xml).contains("c.snapshot_id = sec.snapshot_id");
        // 闭包限定同快照集（结构节点与召回单元同 snapshot 语义）
        assertThat(xml).contains("n.snapshot_id IN");
        // 守卫查询：每 root 闭包计数
        assertThat(xml).contains("SELECT root, count(*) AS total FROM sec GROUP BY root");
        // 参数化：种子 ref 无 ${} 插值
        assertThat(xml).doesNotContain("${tr}");
        assertThat(xml).doesNotContain("${sid}");
    }
}
