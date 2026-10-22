package com.coremasterkb.serving.operator.mapper;

import org.apache.ibatis.annotations.Param;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * 63号批次4 wiki 制品检索 mapper（kb_wiki_pages / kb_wiki_page_links /
 * kb_wiki_page_units，DDL 真相源 {@code databases/kb/schemas/018_wiki_artifacts.sql}，
 * Python 写侧 = knowledge_mining mining/kb/wiki_*）。
 *
 * <p>制品域独立检索路径（63号 §5.2）：不进范式引擎、不混库侧
 * asset_retrieval_units_v2——wiki units 自带 embedding 列（VECTOR(1024)，
 * HNSW = idx_wiki_units_hnsw）。权限与 revision 定版由调用方
 * （ArtifactWikiSearchService，selectVisibleArtifact.currentRevision）收口，
 * 本 mapper 只管制品内检索。</p>
 *
 * <p>安全契约与 {@link ArtifactToolMapper} 同源：向量 literal / 词法串 / 键
 * 全部 {@code #{}} 预编译绑定，方向是 mapper {@code <choose>} 固定分支，
 * 全文无字符串拼接插值。</p>
 */
public interface WikiArtifactToolMapper {

    /** units embedding 状态聚计（COUNT 单趟返回；制品+revision 无 units 时全 0）。 */
    record WikiUnitStatusRow(Long total, Long done, Long pending, Long failed) {}

    /** 检索单元命中行（dense=1-余弦距离、keyword 恒 1.0；title 来自所属页）。 */
    record WikiUnitHitRow(String unitKey, String pageKey, String title,
                          String contentText, Double score) {}

    /** 单页整读行（frontmatter/provenance 为 JSONB ::text 的 JSON 字符串）。 */
    record WikiPageRow(String pageKey, String title, String pageType, String frontmatter,
                       String bodyMd, String provenance, String origin,
                       OffsetDateTime updatedAt) {}

    /** 页链接行（out：from_page=本页；in：to_page=本页）。 */
    record WikiLinkRow(String fromPage, String toPage, String anchorText, String status) {}

    /**
     * units embedding 状态聚计：wiki 卡片进度与 dense 通道降级判定
     * （done==0 → 该通道空手降级，63号 §5.2）共用；COUNT 恒返回一行。
     */
    WikiUnitStatusRow selectWikiUnitsStatus(
            @Param("artifactId") String artifactId, @Param("revision") int revision);

    /**
     * dense 通道：HNSW ANN 两层形态（照 009 searchDenseV2）——候选层距离序
     * LIMIT candidateBudget（即 idx_wiki_units_hnsw 入口），外层 JOIN pages
     * 取 title、score = 1 - 余弦距离。post-filter 截断边界（embed_status
     * 过滤 + LIMIT，无 iterative_scan）见 XML 注释。
     *
     * @param queryVector     pgvector literal，e.g. {@code [0.1,0.2,...]}
     * @param candidateBudget 候选窗（调用方按 topK 放大并设硬顶）
     */
    List<WikiUnitHitRow> searchWikiUnitsDense(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("queryVector") String queryVector,
            @Param("candidateBudget") int candidateBudget);

    /**
     * keyword 通道：content_text ILIKE 子串匹配（无 tsvector 列零 DDL，
     * CJK 子串命中）；score 恒 1.0（RRF 只消费名次）；units 无 ordinal——
     * (page_key, unit_key) 字典序稳定。值含 % / _ 不转义（子串模糊语义，
     * 与表格 DSL contains 同族，XML 注释记录）。
     */
    List<WikiUnitHitRow> searchWikiUnitsKeyword(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("lexicalQuery") String lexicalQuery,
            @Param("limit") int limit);

    /**
     * 单页整读：frontmatter/provenance 以 ::text 输出；无行 = 页不存在
     * （调用方判 page_not_found）。
     */
    WikiPageRow selectWikiPage(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("pageKey") String pageKey);

    /**
     * 页链接单跳：direction=in 入链（to_page 固定）/ 其余（默认 out）出链
     * （from_page 固定）；排序 = 对侧页 + anchor_text（PK 唯一，确定性序）。
     */
    List<WikiLinkRow> selectWikiLinks(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("pageKey") String pageKey, @Param("direction") String direction);
}
