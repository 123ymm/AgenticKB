package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.domain.RetrievalCandidate;
import com.coremasterkb.serving.infrastructure.EmbeddingClient;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.ArtifactVisibleRow;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.RevisionRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiLinkRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiPageRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiUnitHitRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiUnitStatusRow;
import com.coremasterkb.serving.operator.operators.fuse.RrfSupport;
import com.coremasterkb.serving.util.JsonUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * wiki 制品（art_ + kind=wiki）Agent 检索服务（63号批次4）。
 *
 * <p><b>定位</b>（63号 §5.2 制品域独立检索路径）：不进范式引擎、不混库侧
 * {@code asset_retrieval_units_v2}——wiki units 自带 embedding 列，本服务直接打
 * {@link WikiArtifactToolMapper}。权限与 revision 定版复用
 * {@link ArtifactToolMapper#selectVisibleArtifact}（单 SQL 谓词折叠：不存在/无权/
 * 已停用/跨域同一 {@code artifact_not_found} 404 反探测；T1 审查核实——kind 列已
 * 在该 SQL SELECT 与 {@link ArtifactVisibleRow} 中，零改动）。只读
 * {@code current_revision}（§5.5：rev0 / 未发布对 Agent 不可见）。</p>
 *
 * <p><b>四个入口</b>（MCP query dict 协议定稿见批次4计划）：</p>
 * <ul>
 *   <li>{@link #card}——卡片：页数（revision 快照 row_count=发布定格页数）+
 *       units embedding 进度聚计；</li>
 *   <li>{@link #search}——三通道检索：dense（HNSW ANN，查询向量
 *       {@link EmbeddingClient} 现算）/ keyword（ILIKE 子串）/ fusion
 *       （{@link RrfSupport#fuse}，k=60，双通道等权）；</li>
 *   <li>{@link #readPage}——单页整读（frontmatter/provenance 解析为结构 + 双向
 *       链接摘要各前 20）；</li>
 *   <li>{@link #links}——链接单跳（direction=out/in）。</li>
 * </ul>
 *
 * <p><b>dense 降级口径</b>（照 DenseVectorOperator 先例：检索失败≠无命中，
 * 降级留痕不 500，keyword/融合通道继续可用）：units done=0（不发 embedding 调用
 * 省成本）/ embedding 调用抛错或服务未配置 / 返回向量 null 或空 /
 * 向量维度≠1024（llm_service 换模型后 {@code <=>} 对 VECTOR(1024) 直接 PG 报错）/
 * dense SQL 抛错——五路均降级为空 dense 通道 + {@code degradedDense=true}。</p>
 */
@Service
public class ArtifactWikiSearchService {

    private static final Logger log = LoggerFactory.getLogger(ArtifactWikiSearchService.class);

    static final String KIND_WIKI = "wiki";
    /** RRF 通道标识（63号 §5.2 wiki 检索专属通道，与范式引擎 fts/dense 不混名）。 */
    static final String CHANNEL_WIKI_DENSE = "wiki_dense";
    static final String CHANNEL_WIKI_KEYWORD = "wiki_keyword";
    private static final Set<String> MODES = Set.of("fusion", "dense", "keyword");
    private static final Map<String, Double> CHANNEL_WEIGHTS =
            Map.of(CHANNEL_WIKI_DENSE, 1.0, CHANNEL_WIKI_KEYWORD, 1.0);
    /** kb_wiki_page_units.embedding 的 DDL 定版维度（018_wiki_artifacts.sql VECTOR(1024)）。 */
    static final int EMBEDDING_DIM = 1024;
    /** snippet 截断长度（code point 计，防代理对劈半）。 */
    static final int SNIPPET_CODE_POINTS = 240;
    static final int DEFAULT_TOP_K = 10;
    static final int MAX_TOP_K = 50;
    /** dense 候选窗 = max(5*topK, 100)，硬顶 400（护栏：topK 夹取 [1,50] 后常态 ≤250）。 */
    static final int DENSE_BUDGET_FLOOR = 100;
    static final int DENSE_BUDGET_MULTIPLIER = 5;
    static final int DENSE_BUDGET_HARD_CAP = 400;
    /** keyword 通道取数窗（RRF 只消费名次，3 倍余量足够 topK 截断）。 */
    static final int KEYWORD_LIMIT_MULTIPLIER = 3;
    /** readPage 链接摘要每方向保留条数（完整清单走 links 入口）。 */
    static final int LINKS_SUMMARY_LIMIT = 20;
    /** RRF k 常数（与范式引擎 rrf 算子同值，25号 §6.6）。 */
    static final int RRF_K = 60;
    private static final String REF_PREFIX = "art_";

    private final WikiArtifactToolMapper wikiMapper;
    private final ArtifactToolMapper artifactMapper;
    private final EmbeddingClient embeddingClient;

    public ArtifactWikiSearchService(WikiArtifactToolMapper wikiMapper,
                                     ArtifactToolMapper artifactMapper,
                                     EmbeddingClient embeddingClient) {
        this.wikiMapper = wikiMapper;
        this.artifactMapper = artifactMapper;
        this.embeddingClient = embeddingClient;
    }

    // ------------------------------------------------------------------ DTO

    /** units embedding 状态聚计（卡片进度）。 */
    public record WikiEmbeddingProgress(long total, long done, long pending, long failed) {}

    /**
     * wiki 卡片（独立 record，不扩展 ArtifactCard——加字段会连坐 table 分支构造点
     * 与既有测试）。status 恒 "published"：可见性谓词含 status='published'，
     * 可见即已发布（ArtifactVisibleRow 不携 status 列，写死即真相）；updated_at
     * 该行亦无此列，不加列避免为卡片扩大 mapper 面。
     */
    public record WikiArtifactCard(String kind, String artifactRef, String title, String status,
                                   int revision, int pageCount,
                                   WikiEmbeddingProgress embedding) {}

    /** 检索单条命中（snippet = content_text 前 240 code point）。 */
    public record WikiSearchHit(String pageKey, String title, String unitKey, String snippet,
                                double score, List<String> channels) {}

    /** 检索视图：degradedDense = dense 通道降级留痕（结果仍可能来自 keyword）。 */
    public record WikiSearchView(String artifactRef, int revision, boolean degradedDense,
                                 List<WikiSearchHit> results) {}

    /** 链接条目（out：fromPage=本页；in：toPage=本页）。 */
    public record WikiLinkItem(String fromPage, String toPage, String anchorText,
                               String status) {}

    /** 页整读正文（frontmatter/provenance 已解析——写侧 JSONB，读侧 ::text JSON 串）。 */
    public record WikiPageDetail(String pageKey, String title, String pageType,
                                 Map<String, Object> frontmatter, String bodyMd,
                                 List<Object> provenance, String origin,
                                 OffsetDateTime updatedAt) {}

    /** 页链接摘要（out/in 各前 LINKS_SUMMARY_LIMIT 条）。 */
    public record WikiPageLinks(List<WikiLinkItem> out, List<WikiLinkItem> in) {}

    /** 页整读视图。 */
    public record WikiPageView(String artifactRef, int revision, WikiPageDetail page,
                               WikiPageLinks links) {}

    /** 链接单跳视图（direction 已归一为 out/in）。 */
    public record WikiLinksView(String artifactRef, int revision, String pageKey,
                                String direction, List<WikiLinkItem> links) {}

    // ------------------------------------------------------------------ card

    /**
     * wiki 卡片：页数取 revision 快照 row_count（wiki 发布事务定格页数，
     * db_wiki.publish 与 table 同构）；embedding 进度取 units 状态聚计。
     *
     * @throws IllegalArgumentException("artifact_not_found") 不存在/无权/已停用/跨域
     *         （GlobalExceptionHandler 映射 404 反探测）
     * @throws StructureToolException(unsupported_operation) 非 wiki 制品
     */
    public WikiArtifactCard card(String artifactRef, String domain, String username,
                                 boolean isAdmin, String keyId) {
        WikiContext ctx = requireWiki(artifactRef, domain, username, isAdmin, keyId);
        RevisionRow snapshot = artifactMapper.selectRevision(ctx.artifactId(), ctx.revision());
        if (snapshot == null) {
            // wiki 发布事务同样定格 revision 快照（row_count=页数）——缺失即完整性问题，
            // 与 table 分支 artifact_revision_missing 同口径
            throw new IllegalStateException("artifact_revision_missing");
        }
        WikiUnitStatusRow status = wikiMapper.selectWikiUnitsStatus(
                ctx.artifactId(), ctx.revision());
        return new WikiArtifactCard(KIND_WIKI, artifactRef, ctx.title(), "published",
                ctx.revision(), snapshot.rowCount() == null ? 0 : snapshot.rowCount(),
                progressOf(status));
    }

    // ------------------------------------------------------------------ search

    /**
     * 三通道检索。mode ∈ fusion（默认）/dense/keyword；topK 默认 10、夹取 [1,50]；
     * dense 通道五口径降级（见类注释）——降级不报错，keyword/融合继续返回。
     *
     * @throws StructureToolException(invalid_query) q 空白 / mode 非法
     * @throws IllegalArgumentException("artifact_not_found") 不可见（404 反探测）
     */
    public WikiSearchView search(String artifactRef, String q, String mode, Integer topK,
                                 String domain, String username, boolean isAdmin, String keyId) {
        WikiContext ctx = requireWiki(artifactRef, domain, username, isAdmin, keyId);
        if (q == null || q.isBlank()) {
            throw invalidQuery("q 必填且不能为空白（wiki 检索文本）", Map.of());
        }
        String effectiveMode = mode == null || mode.isBlank() ? "fusion" : mode;
        if (!MODES.contains(effectiveMode)) {
            throw invalidQuery("mode 非法: " + effectiveMode,
                    Map.of("allowed", List.of("fusion", "dense", "keyword")));
        }
        int k = topK == null ? DEFAULT_TOP_K : Math.max(1, Math.min(MAX_TOP_K, topK));

        boolean denseNeeded = "dense".equals(effectiveMode) || "fusion".equals(effectiveMode);
        boolean keywordNeeded = "keyword".equals(effectiveMode) || "fusion".equals(effectiveMode);

        DenseOutcome dense = denseNeeded ? denseChannel(ctx, q, k) : DenseOutcome.SKIP;
        // keyword 通道是保底（无降级语义）：SQL 失败即向上抛——吞掉会让"检索"变空成功
        List<WikiUnitHitRow> keywordRows = keywordNeeded
                ? wikiMapper.searchWikiUnitsKeyword(ctx.artifactId(), ctx.revision(),
                        q, k * KEYWORD_LIMIT_MULTIPLIER)
                : List.of();

        if ("dense".equals(effectiveMode)) {
            return new WikiSearchView(artifactRef, ctx.revision(), dense.degraded(),
                    singleChannelHits(dense.rows(), CHANNEL_WIKI_DENSE, k));
        }
        if ("keyword".equals(effectiveMode)) {
            return new WikiSearchView(artifactRef, ctx.revision(), false,
                    singleChannelHits(keywordRows, CHANNEL_WIKI_KEYWORD, k));
        }

        // fusion：两通道候选 → RRF（k=60，等权）→ 按 topK 截断
        List<RetrievalCandidate> candidates = new ArrayList<>();
        Map<String, WikiUnitHitRow> byUnit = new LinkedHashMap<>();
        appendChannel(candidates, byUnit, dense.rows(), CHANNEL_WIKI_DENSE);
        appendChannel(candidates, byUnit, keywordRows, CHANNEL_WIKI_KEYWORD);
        List<RetrievalCandidate> fused =
                RrfSupport.fuse(candidates, RRF_K, CHANNEL_WEIGHTS);
        List<WikiSearchHit> hits = new ArrayList<>(Math.min(k, fused.size()));
        for (RetrievalCandidate c : fused) {
            if (hits.size() >= k) {
                break;
            }
            WikiUnitHitRow row = byUnit.get(c.retrievalUnitId());
            if (row == null) {
                continue; // 防御：候选恒由 byUnit 构造，不可达
            }
            hits.add(new WikiSearchHit(row.pageKey(), row.title(), row.unitKey(),
                    snippet(row.contentText()), c.score(), channelsOf(c)));
        }
        return new WikiSearchView(artifactRef, ctx.revision(), dense.degraded(), hits);
    }

    // ------------------------------------------------------------------ readPage / links

    /**
     * 单页整读：frontmatter/provenance 解析为结构 + 双向链接摘要（各前 20 条，
     * 完整清单走 {@link #links}）。Java 侧收明文 page_key——内网 internal 通道
     * 不经网关，MCP/前端转发端点各自负责编码。
     *
     * @throws StructureToolException(page_not_found, 404) 页不在该 revision
     */
    public WikiPageView readPage(String artifactRef, String pageKey, String domain,
                                 String username, boolean isAdmin, String keyId) {
        WikiContext ctx = requireWiki(artifactRef, domain, username, isAdmin, keyId);
        requirePageKey(pageKey);
        WikiPageRow page = wikiMapper.selectWikiPage(
                ctx.artifactId(), ctx.revision(), pageKey);
        if (page == null) {
            throw pageNotFound(pageKey);
        }
        List<WikiLinkItem> out = linkItems(wikiMapper.selectWikiLinks(
                ctx.artifactId(), ctx.revision(), pageKey, "out"), LINKS_SUMMARY_LIMIT);
        List<WikiLinkItem> in = linkItems(wikiMapper.selectWikiLinks(
                ctx.artifactId(), ctx.revision(), pageKey, "in"), LINKS_SUMMARY_LIMIT);
        return new WikiPageView(artifactRef, ctx.revision(), toPageDetail(page),
                new WikiPageLinks(out, in));
    }

    /**
     * 链接单跳：direction ∈ out（默认）/in。
     *
     * @throws StructureToolException(invalid_query) pageKey 空白 / direction 非法
     */
    public WikiLinksView links(String artifactRef, String pageKey, String direction,
                               String domain, String username, boolean isAdmin, String keyId) {
        WikiContext ctx = requireWiki(artifactRef, domain, username, isAdmin, keyId);
        requirePageKey(pageKey);
        String dir = direction == null || direction.isBlank() ? "out" : direction;
        if (!"out".equals(dir) && !"in".equals(dir)) {
            throw invalidQuery("direction 非法: " + direction,
                    Map.of("allowed", List.of("out", "in")));
        }
        return new WikiLinksView(artifactRef, ctx.revision(), pageKey, dir,
                linkItems(wikiMapper.selectWikiLinks(
                        ctx.artifactId(), ctx.revision(), pageKey, dir), Integer.MAX_VALUE));
    }

    // ------------------------------------------------------------------ dense 通道（降级口径）

    /** dense 通道结果：degraded=true 时 rows 恒空。SKIP = 模式不启用（非降级）。 */
    private record DenseOutcome(boolean degraded, List<WikiUnitHitRow> rows) {
        static final DenseOutcome SKIP = new DenseOutcome(false, List.of());
        static DenseOutcome degraded(String reason) {
            log.warn("[wiki-search] dense 通道降级（reason={}）——keyword/融合通道继续可用", reason);
            return new DenseOutcome(true, List.of());
        }
    }

    private DenseOutcome denseChannel(WikiContext ctx, String q, int topK) {
        WikiUnitStatusRow status = wikiMapper.selectWikiUnitsStatus(
                ctx.artifactId(), ctx.revision());
        long done = status == null || status.done() == null ? 0L : status.done();
        if (done == 0) {
            // 无已嵌入 units：不发 embedding 调用省成本（63号 §5.2 降级口径①）
            return DenseOutcome.degraded("no_done_units");
        }
        if (embeddingClient == null || !embeddingClient.isConfigured()) {
            return DenseOutcome.degraded("embedding_not_configured");
        }
        float[] vec;
        try {
            vec = embeddingClient.embed(q);
        } catch (RuntimeException ex) {
            log.warn("[wiki-search] 查询向量化失败，dense 通道降级", ex);
            return DenseOutcome.degraded("embed_failed");
        }
        if (vec == null || vec.length == 0) {
            return DenseOutcome.degraded("empty_vector");
        }
        if (vec.length != EMBEDDING_DIM) {
            // 维度守卫：llm_service 换模型后 <=> 对 VECTOR(1024) 直接 PG 报错——降级不 500
            return DenseOutcome.degraded("dimension_mismatch_" + vec.length);
        }
        int budget = Math.min(DENSE_BUDGET_HARD_CAP,
                Math.max(DENSE_BUDGET_MULTIPLIER * topK, DENSE_BUDGET_FLOOR));
        try {
            List<WikiUnitHitRow> rows = wikiMapper.searchWikiUnitsDense(
                    ctx.artifactId(), ctx.revision(), formatVector(vec), budget);
            return new DenseOutcome(false, rows);
        } catch (RuntimeException ex) {
            log.warn("[wiki-search] dense SQL 失败，dense 通道降级", ex);
            return DenseOutcome.degraded("dense_sql_failed");
        }
    }

    // ------------------------------------------------------------------ 候选装配

    /**
     * 通道候选装配：channelRank = 通道内名次（1 基，行序即 SQL 排序序），
     * canonical = unit_key（units PK 含 artifact+revision，跨页唯一）——两通道
     * 同 unit 命中在 fuse 中对齐为一条融合位。
     */
    private static void appendChannel(List<RetrievalCandidate> out,
                                      Map<String, WikiUnitHitRow> byUnit,
                                      List<WikiUnitHitRow> rows, String channelId) {
        int rank = 0;
        for (WikiUnitHitRow row : rows) {
            rank++;
            double raw = row.score() == null ? 0.0 : row.score();
            out.add(new RetrievalCandidate(row.unitKey(), raw, channelId, Map.of(), null,
                    List.of(), null, row.unitKey(), null, null,
                    channelId, rank, raw, null, Map.of()));
            byUnit.putIfAbsent(row.unitKey(), row);
        }
    }

    /** 单通道视图：dense 外层无 LIMIT——service 层按 topK 截断（T1 移交护栏）。 */
    private static List<WikiSearchHit> singleChannelHits(List<WikiUnitHitRow> rows,
                                                         String channel, int topK) {
        List<WikiSearchHit> hits = new ArrayList<>();
        for (WikiUnitHitRow row : rows) {
            if (hits.size() >= topK) {
                break;
            }
            hits.add(new WikiSearchHit(row.pageKey(), row.title(), row.unitKey(),
                    snippet(row.contentText()), row.score() == null ? 0.0 : row.score(),
                    List.of(channel)));
        }
        return hits;
    }

    /** 融合候选的命中通道清单（fuse 写入 scoreChain.routeSources；单通道回退 channelId）。 */
    private static List<String> channelsOf(RetrievalCandidate c) {
        if (c.scoreChain() != null && c.scoreChain().routeSources() != null
                && !c.scoreChain().routeSources().isEmpty()) {
            return c.scoreChain().routeSources();
        }
        return c.channelId() != null && !c.channelId().isEmpty()
                ? List.of(c.channelId()) : List.of(c.source());
    }

    /** content_text 前 240 code point 截断（String.codePointCount 计数，防代理对劈半）。 */
    static String snippet(String contentText) {
        if (contentText == null) {
            return "";
        }
        if (contentText.codePointCount(0, contentText.length()) <= SNIPPET_CODE_POINTS) {
            return contentText;
        }
        return contentText.substring(0,
                contentText.offsetByCodePoints(0, SNIPPET_CODE_POINTS));
    }

    private static List<WikiLinkItem> linkItems(List<WikiLinkRow> rows, int limit) {
        List<WikiLinkItem> out = new ArrayList<>();
        for (WikiLinkRow r : rows) {
            if (out.size() >= limit) {
                break;
            }
            out.add(new WikiLinkItem(r.fromPage(), r.toPage(), r.anchorText(), r.status()));
        }
        return out;
    }

    private static WikiPageDetail toPageDetail(WikiPageRow page) {
        return new WikiPageDetail(page.pageKey(), page.title(), page.pageType(),
                JsonUtils.safeJsonParse(page.frontmatter()), page.bodyMd(),
                parseProvenance(page.provenance()), page.origin(), page.updatedAt());
    }

    /** provenance 写侧为 JSON 数组（批次3 group_provenance→list）——解析失败回退空表。 */
    private static List<Object> parseProvenance(String provenanceJson) {
        if (provenanceJson == null || provenanceJson.isBlank()) {
            return List.of();
        }
        try {
            return JsonUtils.mapper().readValue(provenanceJson, JsonUtils.mapper()
                    .getTypeFactory().constructCollectionType(List.class, Object.class));
        } catch (Exception e) {
            return List.of();
        }
    }

    private static WikiEmbeddingProgress progressOf(WikiUnitStatusRow status) {
        if (status == null) {
            return new WikiEmbeddingProgress(0, 0, 0, 0);
        }
        return new WikiEmbeddingProgress(nvl(status.total()), nvl(status.done()),
                nvl(status.pending()), nvl(status.failed()));
    }

    private static long nvl(Long v) {
        return v == null ? 0L : v;
    }

    // ------------------------------------------------------------------ 权限 / 参数校验

    /** 已放行的 wiki 制品上下文（revision 定版 + 卡片用 title）。 */
    private record WikiContext(String artifactId, String title, int revision) {}

    /** 权限与 kind 收口：可见性谓词（404 反探测）+ 仅 wiki + current_revision 定版。 */
    private WikiContext requireWiki(String artifactRef, String domain, String username,
                                    boolean isAdmin, String keyId) {
        if (domain == null || domain.isBlank()) {
            throw StructureToolException.unsupportedOperation(
                    "缺少 domain（制品按域路由）", Map.of());
        }
        String artifactId = parseRef(artifactRef);
        ArtifactVisibleRow artifact = artifactMapper.selectVisibleArtifact(
                artifactId, domain, username, isAdmin, keyId);
        if (artifact == null) {
            // 404 反探测：不存在/非成员/deprecated/跨域同一响应（谓词折叠在 SQL，
            // 与 ArtifactQueryService.query 同口径）
            throw new IllegalArgumentException("artifact_not_found");
        }
        if (!KIND_WIKI.equals(artifact.kind())) {
            throw StructureToolException.unsupportedOperation(
                    "仅 wiki 类制品支持该检索协议",
                    Map.of("kind", String.valueOf(artifact.kind())));
        }
        return new WikiContext(artifactId, artifact.title(),
                artifact.currentRevision() == null ? 0 : artifact.currentRevision());
    }

    private static void requirePageKey(String pageKey) {
        if (pageKey == null || pageKey.isBlank()) {
            throw invalidQuery("page_key 必填（wiki 页键）", Map.of());
        }
    }

    /** wiki 页不在该 revision（制品可见而页缺——404 typed，非反探测面）。 */
    private static StructureToolException pageNotFound(String pageKey) {
        return new StructureToolException("page_not_found", HttpStatus.NOT_FOUND,
                "wiki 页面不存在: " + pageKey, Map.of());
    }

    /** wiki 检索协议参数校验失败（400；q 空白/mode 非法/direction 非法）。 */
    private static StructureToolException invalidQuery(String message,
                                                       Map<String, Object> details) {
        return new StructureToolException("invalid_query", HttpStatus.BAD_REQUEST,
                message, details);
    }

    /** 与 ArtifactQueryService.parseRef 同文法（ref 收口：前缀 + 非空 id）。 */
    private static String parseRef(String artifactRef) {
        if (artifactRef == null || artifactRef.isBlank()) {
            throw StructureToolException.invalidRef("artifact_ref 必填");
        }
        if (!artifactRef.startsWith(REF_PREFIX)) {
            throw StructureToolException.invalidRef("artifact_ref 前缀非法（期望 art_）");
        }
        String id = artifactRef.substring(REF_PREFIX.length());
        if (id.isBlank()) {
            throw StructureToolException.invalidRef("artifact_ref 格式非法");
        }
        return id;
    }

    /** pgvector literal（照 DenseVectorOperator.formatVector，float 展开精度足够 ANN）。 */
    private static String formatVector(float[] vec) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }
}
