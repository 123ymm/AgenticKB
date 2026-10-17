package com.coremasterkb.serving.operator.mapper;

import com.coremasterkb.serving.operator.mapper.StructureToolMapper.Criterion;
import org.apache.ibatis.annotations.Param;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * 61号批次3（前半）art_ 制品消费面 mapper（kb_artifacts 六表，DDL 真相源
 * {@code databases/kb/schemas/017_kb_artifacts.sql}，Python 写侧 =
 * knowledge_mining mining/kb/db_artifacts.py）。
 *
 * <p>安全契约与 {@link StructureToolMapper} 同源：字段名只以
 * {@code data->>#{field}} 绑定值进入 SQL（白名单校验在 Java 侧
 * {@code ArtifactQueryService}），操作符/排序方向/聚合函数是 mapper
 * {@code <choose>} 固定分支，全文零 {@code ${}} 插值。</p>
 *
 * <p>权限谓词折叠进单 SQL（404 反探测）：不存在 / 非成员 / 已停用 / 跨域
 * 同一空手返回——对齐 {@code KbAccessService} 的 kb_not_found 口径
 * （站点 admin 全通，但 status/domain 条件对 admin 同样生效）。</p>
 */
public interface ArtifactToolMapper {

    /** 可见制品行（谓词放行才有行；current_revision 即运行态读版本）。 */
    record ArtifactVisibleRow(String artifactId, String domainId, String kind,
                              Integer currentRevision, String title, String description,
                              String usageNote) {}

    /** 发布定格的 revision 快照（schema_json 列定义 + 行数）。 */
    record RevisionRow(String schemaJson, Integer rowCount) {}

    /**
     * 行结果。{@code sortValue} 仅自定义排序时非空（数值列=numeric、其余=文本、
     * 缺值=NULL）——供 (sort_value, ordinal, row_key) keyset cursor 编码。
     */
    record ArtifactRowValue(String rowKey, String dataJson, String provenanceJson,
                            Integer ordinal, Object sortValue) {}

    /** 聚合结果：符合过滤的行数 + 聚合值（sum/min/max/avg 无匹配行时 value 可为 null）。 */
    record AggregateValueRow(Long rowCount, Object value) {
        public AggregateValueRow(Long rowCount, Double value) { this(rowCount, (Object) value); }
        public AggregateValueRow(Long rowCount, String value) { this(rowCount, (Object) value); }
    }

    /** 发现通道行（row_count 来自 current_revision 快照；未发布/wiki 无行 → null）。 */
    record ArtifactSummaryRow(String artifactId, String title, String description,
                              String usageNote, Integer currentRevision, String kind,
                              OffsetDateTime updatedAt, Integer rowCount) {}

    // ---- 制品可见性 / revision 快照 ----------------------------------------------

    /**
     * 权限谓词（单 SQL）：{@code status='published'} AND {@code domain_id=#{domain}}
     * AND (isAdmin OR 成员 OR owner)。空手 = 不存在/无权/已停用/跨域（同响应）。
     */
    ArtifactVisibleRow selectVisibleArtifact(
            @Param("artifactId") String artifactId, @Param("domain") String domain,
            @Param("username") String username, @Param("isAdmin") boolean isAdmin);

    /** 指定 revision 的 schema 快照 + row_count（发布定格，61号 §4.2）。 */
    RevisionRow selectRevision(
            @Param("artifactId") String artifactId, @Param("revision") int revision);

    // ---- DSL 行查询 / 聚合 ---------------------------------------------------------

    /**
     * 行查询：criteria（参数绑定 + 枚举分支）→ 稳定排序（自定义列 + ordinal/row_key
     * tie-break）→ keyset 翻页（默认序=(ordinal,row_key)；自定义序=(sort_value,ordinal,
     * row_key)，NULL 块语义见 XML 注释）→ LIMIT 收窄（service 传 limit+1 探测 has_more）。
     */
    List<ArtifactRowValue> selectArtifactRows(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("criteria") List<Criterion> criteria,
            @Param("orderField") String orderField, @Param("orderDir") String orderDir,
            @Param("numericOrder") boolean numericOrder,
            @Param("afterOrdinal") int afterOrdinal, @Param("afterRowKey") String afterRowKey,
            @Param("afterSortValue") String afterSortValue,
            @Param("limit") int limit);

    /**
     * 聚合：op ∈ count/sum/min/max/avg（固定分支；aggField 白名单 + 参数绑定；
     * textualAggregate = date 列 min/max 走 ISO 文本字典序）。COUNT(*) 与聚合值同趟返回。
     */
    AggregateValueRow aggregateArtifactRows(
            @Param("artifactId") String artifactId, @Param("revision") int revision,
            @Param("criteria") List<Criterion> criteria,
            @Param("aggOp") String aggOp, @Param("aggField") String aggField,
            @Param("textualAggregate") boolean textualAggregate);

    // ---- 发现通道 ------------------------------------------------------------------

    /**
     * 域内已发布且可见的制品清单（谓词同 {@link #selectVisibleArtifact}），
     * (updated_at DESC, artifact_id ASC) 稳定序 + keyset 翻页。
     */
    List<ArtifactSummaryRow> selectArtifactSummaries(
            @Param("domain") String domain, @Param("username") String username,
            @Param("isAdmin") boolean isAdmin,
            @Param("afterUpdatedAt") OffsetDateTime afterUpdatedAt,
            @Param("afterArtifactId") String afterArtifactId,
            @Param("limit") int limit);
}
