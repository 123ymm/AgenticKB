package com.coremasterkb.serving.operator;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 63号批次4 wiki 检索 mapper 的 SQL 文本契约测试。PG 集成测试不可用时，
 * 这里锁定 SQL 关键语义：dense ANN 两层形态（候选层距离序 LIMIT → 外层
 * JOIN pages 取 title）、余弦距离算子、embed_status='done' 过滤、ILIKE
 * 参数绑定不拼接、links 方向 <choose>、units 状态 FILTER 聚计。
 */
@DisplayName("WikiArtifactToolMapper SQL contract")
class WikiArtifactToolMapperXmlTest {

    private static String mapperXml() throws Exception {
        try (InputStream in = WikiArtifactToolMapperXmlTest.class.getClassLoader()
                .getResourceAsStream("mapper/WikiArtifactToolMapper.xml")) {
            assertThat(in).as("mapper XML must be on classpath").isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    @DisplayName("dense 两层 ANN：候选层 done 过滤 + 余弦距离序 LIMIT，外层 JOIN pages 取 title")
    void denseAnnTwoLayerContract() throws Exception {
        String xml = mapperXml();

        // 候选层（最内层）：余弦距离表达式原样出现在 ORDER BY——HNSW
        // （idx_wiki_units_hnsw）入口形态，照 009 searchDenseV2 的 ANN 改写口径
        assertThat(xml).contains("(embedding &lt;=&gt; #{queryVector}::vector) AS distance");
        int orderByIdx = xml.indexOf("ORDER BY embedding &lt;=&gt; #{queryVector}::vector");
        int candidateLimitIdx = xml.indexOf("LIMIT #{candidateBudget}");
        assertThat(orderByIdx).as("候选层必须以距离表达式排序").isGreaterThan(-1);
        assertThat(candidateLimitIdx).as("候选层必须有候选预算 LIMIT").isGreaterThan(orderByIdx);

        // 候选层只取 embed_status='done' 的 units（post-filter 截断边界，
        // 边界语义由 XML 注释记录）
        assertThat(xml).contains("embed_status = 'done'");

        // 候选层制品/revision 收窄（wiki 检索只在制品内打）
        assertThat(xml).contains("FROM kb_wiki_page_units");

        // 外层：JOIN kb_wiki_pages（三键全匹配）取 title；score = 1 - distance
        assertThat(xml).contains("JOIN kb_wiki_pages p");
        assertThat(xml).contains("p.artifact_id = u.artifact_id");
        assertThat(xml).contains("p.revision = u.revision");
        assertThat(xml).contains("p.page_key = u.page_key");
        assertThat(xml).contains("p.title");
        assertThat(xml).contains("(1 - u.distance) AS score");
    }

    @Test
    @DisplayName("keyword：ILIKE 子串值走 #{} 绑定不拼接，score 恒 1.0，(page_key, unit_key) 稳定序")
    void keywordIlikeContract() throws Exception {
        String xml = mapperXml();

        // 值走预编译绑定：'%' || #{lexicalQuery} || '%' 是把绑定参数包进字面量
        // 百分号（无 ${} 拼接）；参数含 % / _ 不转义（子串模糊语义，与表格 DSL
        // contains 同族口径，由 XML 注释记录）
        assertThat(xml).contains("ILIKE ('%' || #{lexicalQuery} || '%')");
        assertThat(xml).doesNotContain("${lexicalQuery}");

        // score 恒 1.0：RRF 融合只消费名次，不消费分值
        assertThat(xml).contains("1.0 AS score");

        // units 无 ordinal——稳定序 = (page_key, unit_key) 字典序
        assertThat(xml).contains("ORDER BY u.page_key ASC, u.unit_key ASC");

        // keyword 同样 JOIN pages 取 title（制品/revision 收窄 + 页三键匹配）
        assertThat(xml).contains("AND u.content_text ILIKE");
    }

    @Test
    @DisplayName("links：direction 是 <choose> 枚举固定分支——in=to_page 收窄按 from_page 排，默认出链反之")
    void linksDirectionChooseContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("FROM kb_wiki_page_links l");
        // 方向枚举固定分支（无字符串拼接插值）
        assertThat(xml).contains("<when test=\"direction == 'in'\">");
        assertThat(xml).contains("l.to_page = #{pageKey}");
        assertThat(xml).contains("l.from_page = #{pageKey}");
        // 排序 = 对侧页 + anchor_text（PK 唯一，确定性序）
        assertThat(xml).contains("l.from_page");
        assertThat(xml).contains("l.to_page");
        assertThat(xml).contains("l.anchor_text ASC");
        // 链接行四列全取（status 供 broken 标注）
        assertThat(xml).contains("l.status");
    }

    @Test
    @DisplayName("units 状态：单趟 FILTER 聚合 done/pending/failed + total")
    void statusFilterAggregationContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("COUNT(*) AS total");
        assertThat(xml).contains("COUNT(*) FILTER (WHERE embed_status = 'done')");
        assertThat(xml).contains("COUNT(*) FILTER (WHERE embed_status = 'pending')");
        assertThat(xml).contains("COUNT(*) FILTER (WHERE embed_status = 'failed')");
    }

    @Test
    @DisplayName("单页整读：全字段投影，frontmatter/provenance 以 ::text 输出")
    void pageReadContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).contains("FROM kb_wiki_pages");
        assertThat(xml).contains("frontmatter::text AS frontmatter");
        assertThat(xml).contains("provenance::text AS provenance");
        assertThat(xml).contains("body_md");
        assertThat(xml).contains("page_type");
        assertThat(xml).contains("updated_at");
        // 单页三键收窄
        assertThat(xml).contains("page_key = #{pageKey}");
    }

    @Test
    @DisplayName("全文零 ${} 插值：向量/词法/键/预算全部 #{} 预编译绑定")
    void parameterizationContract() throws Exception {
        String xml = mapperXml();

        assertThat(xml).doesNotContain("${");
    }
}
