package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.infrastructure.EmbeddingClient;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.ArtifactVisibleRow;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.RevisionRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiLinkRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiPageRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiUnitHitRow;
import com.coremasterkb.serving.operator.mapper.WikiArtifactToolMapper.WikiUnitStatusRow;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 63号批次4 wiki 制品检索服务契约：
 * <ul>
 *   <li>三通道：fusion（RRF 名次合成 + topK 截断）/ dense（ANN 行序 + 截断）/
 *       keyword（ILIKE 行序）；</li>
 *   <li>dense 五口径降级：done=0（不发 embedding 调用）/ embed 抛 / 向量 null /
 *       维度≠1024 / dense SQL 抛——降级不报错，degradedDense=true 留痕；</li>
 *   <li>权限：不可见 → artifact_not_found（404 反探测）；非 wiki kind 拒绝；</li>
 *   <li>参数：q 空白 / mode 非法 / direction 非法 → invalid_query；
 *       topK 夹取 [1,50]（预算/取数窗联动）；</li>
 *   <li>readPage：frontmatter/provenance 解析 + 链接摘要各前 20 + page_not_found；</li>
 *   <li>card：页数=revision 快照 row_count + embedding 进度 + status=published；</li>
 *   <li>snippet：240 code point 截断（emoji 代理对边界不劈半）。</li>
 * </ul>
 */
@DisplayName("ArtifactWikiSearchService")
class ArtifactWikiSearchServiceTest {

    private static final String ID = "5c4b3a2f".repeat(4);
    private static final String REF = "art_" + ID;
    private static final String DOMAIN = "odn";

    private WikiArtifactToolMapper wikiMapper;
    private ArtifactToolMapper artifactMapper;
    private EmbeddingClient embeddingClient;
    private ArtifactWikiSearchService service;

    @BeforeEach
    void setUp() {
        wikiMapper = mock(WikiArtifactToolMapper.class);
        artifactMapper = mock(ArtifactToolMapper.class);
        embeddingClient = mock(EmbeddingClient.class);
        service = new ArtifactWikiSearchService(wikiMapper, artifactMapper, embeddingClient);

        // 默认：本域已发布 wiki、rev2、成员 alice 可见、units 已嵌入
        when(artifactMapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                .thenReturn(visibleWiki(2));
        when(wikiMapper.selectWikiUnitsStatus(ID, 2))
                .thenReturn(new WikiUnitStatusRow(30L, 28L, 1L, 1L));
        when(embeddingClient.isConfigured()).thenReturn(true);
    }

    private static ArtifactVisibleRow visibleWiki(int revision) {
        return new ArtifactVisibleRow(ID, DOMAIN, "wiki", revision, "运维知识 wiki", null, null);
    }

    private static WikiUnitHitRow hit(String unitKey, String pageKey, double score) {
        return new WikiUnitHitRow(unitKey, pageKey, "页-" + pageKey,
                "unit " + unitKey + " 内容", score);
    }

    private static float[] vec(int dim) {
        float[] v = new float[dim];
        for (int i = 0; i < dim; i++) {
            v[i] = 0.01f * (i % 7) - 0.03f;
        }
        return v;
    }

    private static String vectorLiteral(float[] v) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < v.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(v[i]);
        }
        return sb.append(']').toString();
    }

    // ---- 三通道 ------------------------------------------------------------

    @Test
    @DisplayName("fusion：双通道 RRF 名次合成——双通道命中 B 融合分居首，topK 截断")
    void fusionRrfRankSynthesis() {
        float[] v = vec(1024);
        when(embeddingClient.embed("查询")).thenReturn(v);
        // dense 距离序：A(0.9) B(0.8) C(0.7)；keyword 字典序：B D
        when(wikiMapper.searchWikiUnitsDense(eq(ID), eq(2), eq(vectorLiteral(v)), eq(100)))
                .thenReturn(List.of(hit("uA", "p1", 0.9), hit("uB", "p2", 0.8),
                        hit("uC", "p3", 0.7)));
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 6))
                .thenReturn(List.of(hit("uB", "p2", 1.0), hit("uD", "p4", 1.0)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", null, 2, DOMAIN, "alice", false, null);

        assertThat(out.artifactRef()).isEqualTo(REF);
        assertThat(out.revision()).isEqualTo(2);
        assertThat(out.degradedDense()).isFalse();
        // topK=2 截断：RRF 分 B(1/62+1/61) > A(1/61) > D(1/62) > C(1/63)
        assertThat(out.results()).extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("uB", "uA");
        double expectedB = 1.0 / (60 + 2) + 1.0 / (60 + 1);
        assertThat(out.results().get(0).score()).isCloseTo(expectedB,
                org.assertj.core.data.Offset.offset(1e-12));
        assertThat(out.results().get(0).channels())
                .containsExactlyInAnyOrder("wiki_dense", "wiki_keyword");
        assertThat(out.results().get(1).channels()).containsExactly("wiki_dense");
        assertThat(out.results().get(0).snippet()).isEqualTo("unit uB 内容");
        assertThat(out.results().get(0).pageKey()).isEqualTo("p2");
        assertThat(out.results().get(0).title()).isEqualTo("页-p2");
    }

    @Test
    @DisplayName("dense 单通道：行序即距离序，按 topK 截断（外层 SQL 无 LIMIT），keyword 零调用")
    void denseOnlyTruncatesToTopK() {
        float[] v = vec(1024);
        when(embeddingClient.embed("查询")).thenReturn(v);
        when(wikiMapper.searchWikiUnitsDense(eq(ID), eq(2), anyString(), eq(100)))
                .thenReturn(List.of(hit("uA", "p1", 0.9), hit("uB", "p2", 0.8),
                        hit("uC", "p3", 0.7)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "dense", 2, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isFalse();
        assertThat(out.results()).extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("uA", "uB");
        assertThat(out.results().get(0).score()).isEqualTo(0.9);
        assertThat(out.results().get(0).channels()).containsExactly("wiki_dense");
        verify(wikiMapper, never()).searchWikiUnitsKeyword(any(), anyInt(), any(), anyInt());
    }

    @Test
    @DisplayName("keyword 单通道：零 embedding/状态调用，score=1.0，topK 截断")
    void keywordOnlySkipsEmbedding() {
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 3))
                .thenReturn(List.of(hit("uB", "p2", 1.0), hit("uD", "p4", 1.0)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "keyword", 1, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isFalse();
        assertThat(out.results()).hasSize(1);
        assertThat(out.results().get(0).unitKey()).isEqualTo("uB");
        assertThat(out.results().get(0).score()).isEqualTo(1.0);
        assertThat(out.results().get(0).channels()).containsExactly("wiki_keyword");
        verify(embeddingClient, never()).embed(anyString());
        verify(wikiMapper, never()).selectWikiUnitsStatus(any(), anyInt());
    }

    // ---- dense 五口径降级 ----------------------------------------------------

    @Test
    @DisplayName("降级①：done=0 → dense 空 + degradedDense=true，不发 embedding 调用省成本")
    void degradedWhenNoDoneUnits() {
        when(wikiMapper.selectWikiUnitsStatus(ID, 2))
                .thenReturn(new WikiUnitStatusRow(5L, 0L, 4L, 1L));
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 30))
                .thenReturn(List.of(hit("uD", "p4", 1.0)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "fusion", null, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isTrue();
        assertThat(out.results()).extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("uD");
        verify(embeddingClient, never()).embed(anyString());
    }

    @Test
    @DisplayName("降级②：embedding 调用抛错 → dense 空 + degradedDense=true（fusion 仍返回 keyword 命中）")
    void degradedWhenEmbedThrows() {
        when(embeddingClient.embed("查询")).thenThrow(new RuntimeException("llm down"));
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 30))
                .thenReturn(List.of(hit("uD", "p4", 1.0)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "fusion", null, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isTrue();
        assertThat(out.results()).extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("uD");
    }

    @Test
    @DisplayName("降级③：embedding 返回 null → dense 空 + degradedDense=true")
    void degradedWhenVectorNull() {
        when(embeddingClient.embed("查询")).thenReturn(null);
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 30)).thenReturn(List.of());

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "dense", null, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isTrue();
        assertThat(out.results()).isEmpty();
    }

    @Test
    @DisplayName("降级④：向量维度≠1024（llm_service 换模型）→ 降级不 500，dense SQL 零调用")
    void degradedOnDimensionMismatch() {
        when(embeddingClient.embed("查询")).thenReturn(vec(512));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "dense", null, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isTrue();
        assertThat(out.results()).isEmpty();
        verify(wikiMapper, never()).searchWikiUnitsDense(any(), anyInt(), any(), anyInt());
    }

    @Test
    @DisplayName("降级⑤：dense SQL 抛错 → dense 空 + degradedDense=true（keyword 继续可用）")
    void degradedOnDenseSqlFailure() {
        when(embeddingClient.embed("查询")).thenReturn(vec(1024));
        when(wikiMapper.searchWikiUnitsDense(any(), anyInt(), any(), anyInt()))
                .thenThrow(new RuntimeException("hnsw boom"));
        when(wikiMapper.searchWikiUnitsKeyword(ID, 2, "查询", 30))
                .thenReturn(List.of(hit("uD", "p4", 1.0)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "fusion", null, DOMAIN, "alice", false, null);

        assertThat(out.degradedDense()).isTrue();
        assertThat(out.results()).extracting(ArtifactWikiSearchService.WikiSearchHit::unitKey)
                .containsExactly("uD");
    }

    // ---- 权限 / kind --------------------------------------------------------

    @Test
    @DisplayName("不可见（谓词空手）→ artifact_not_found（404 反探测，与 table 通道同口径）")
    void invisibleFoldsToArtifactNotFound() {
        when(artifactMapper.selectVisibleArtifact(ID, DOMAIN, "mallory", false, null))
                .thenReturn(null);

        assertThatThrownBy(() -> service.search(REF, "查询", null, null, DOMAIN, "mallory", false, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");
        assertThatThrownBy(() -> service.card(REF, DOMAIN, "mallory", false, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");
    }

    @Test
    @DisplayName("kind=table 走 wiki 协议 → unsupported_operation（中文消息 + kind 留痕）")
    void nonWikiKindRejected() {
        when(artifactMapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                .thenReturn(new ArtifactVisibleRow(ID, DOMAIN, "table", 3, "T", "D", "U"));

        assertThatThrownBy(() -> service.search(REF, "查询", null, null, DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .hasMessageContaining("仅 wiki 类制品支持该检索协议")
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("unsupported_operation");
    }

    // ---- 参数校验 -------------------------------------------------------------

    @Test
    @DisplayName("q 空白 / mode 非法 → invalid_query（400 typed）")
    void invalidQueryRejected() {
        assertThatThrownBy(() -> service.search(REF, "  ", null, null, DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("invalid_query");
        assertThatThrownBy(() -> service.search(REF, null, null, null, DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("invalid_query");
        assertThatThrownBy(() -> service.search(REF, "查询", "vector", null, DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .satisfies(e -> {
                    StructureToolException ste = (StructureToolException) e;
                    assertThat(ste.code()).isEqualTo("invalid_query");
                    assertThat(ste.details()).containsKey("allowed");
                });
    }

    @Test
    @DisplayName("topK 夹取 [1,50]：0→1、999→50；预算/取数窗联动（budget=250、keyword limit=150）")
    void topKClampedAndBudgetsFollow() {
        when(embeddingClient.embed("查询")).thenReturn(vec(1024));
        when(wikiMapper.searchWikiUnitsDense(any(), anyInt(), any(), anyInt()))
                .thenReturn(List.of());
        when(wikiMapper.searchWikiUnitsKeyword(any(), anyInt(), any(), anyInt()))
                .thenReturn(List.of());

        // 999 → 50：budget = max(5*50,100)=250，keyword limit = 50*3=150
        service.search(REF, "查询", "fusion", 999, DOMAIN, "alice", false, null);
        verify(wikiMapper).searchWikiUnitsDense(eq(ID), eq(2), any(), eq(250));
        verify(wikiMapper).searchWikiUnitsKeyword(ID, 2, "查询", 150);

        // 0 → 1：budget = max(5,100)=100，keyword limit = 3
        service.search(REF, "查询", "fusion", 0, DOMAIN, "alice", false, null);
        verify(wikiMapper).searchWikiUnitsDense(eq(ID), eq(2), any(), eq(100));
        verify(wikiMapper).searchWikiUnitsKeyword(ID, 2, "查询", 3);
    }

    // ---- readPage / links ------------------------------------------------------

    @Test
    @DisplayName("readPage：整页字段 + frontmatter/provenance 解析 + 双向链接摘要各前 20")
    void readPageParsesAndSummarizesLinks() {
        OffsetDateTime ts = OffsetDateTime.parse("2026-10-20T09:30:00+08:00");
        when(wikiMapper.selectWikiPage(ID, 2, "home"))
                .thenReturn(new WikiPageRow("home", "首页", "overview",
                        "{\"tags\":[\"net\"]}", "# 正文", "[{\"source\":\"kb-1/doc-9\"}]",
                        "mined", ts));
        when(wikiMapper.selectWikiLinks(ID, 2, "home", "out"))
                .thenReturn(IntStream.range(0, 25)
                        .mapToObj(i -> new WikiLinkRow("home", "p" + i, "锚" + i, "active"))
                        .toList());
        when(wikiMapper.selectWikiLinks(ID, 2, "home", "in"))
                .thenReturn(List.of(new WikiLinkRow("p9", "home", "回链", "active")));

        ArtifactWikiSearchService.WikiPageView out =
                service.readPage(REF, "home", DOMAIN, "alice", false, null);

        assertThat(out.revision()).isEqualTo(2);
        ArtifactWikiSearchService.WikiPageDetail page = out.page();
        assertThat(page.pageKey()).isEqualTo("home");
        assertThat(page.title()).isEqualTo("首页");
        assertThat(page.pageType()).isEqualTo("overview");
        assertThat(page.frontmatter()).containsEntry("tags", List.of("net"));
        assertThat(page.bodyMd()).isEqualTo("# 正文");
        assertThat(page.provenance()).hasSize(1);
        assertThat(page.origin()).isEqualTo("mined");
        assertThat(page.updatedAt()).isEqualTo(ts);
        // 摘要截断：out 前 20 / in 全量
        assertThat(out.links().out()).hasSize(20);
        assertThat(out.links().out().get(19).toPage()).isEqualTo("p19");
        assertThat(out.links().in()).hasSize(1);
        assertThat(out.links().in().get(0).fromPage()).isEqualTo("p9");
    }

    @Test
    @DisplayName("readPage：页不在该 revision → page_not_found（404 typed）")
    void readPageMissing() {
        when(wikiMapper.selectWikiPage(ID, 2, "ghost")).thenReturn(null);

        assertThatThrownBy(() -> service.readPage(REF, "ghost", DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .satisfies(e -> {
                    StructureToolException ste = (StructureToolException) e;
                    assertThat(ste.code()).isEqualTo("page_not_found");
                    assertThat(ste.status().value()).isEqualTo(404);
                });
    }

    @Test
    @DisplayName("links：direction 归一（null→out）+ in 通道下沉 + 非法 direction 拒绝")
    void linksDirectionHandling() {
        when(wikiMapper.selectWikiLinks(ID, 2, "home", "in"))
                .thenReturn(List.of(new WikiLinkRow("p9", "home", "回链", "active")));

        ArtifactWikiSearchService.WikiLinksView in =
                service.links(REF, "home", "in", DOMAIN, "alice", false, null);
        assertThat(in.direction()).isEqualTo("in");
        assertThat(in.links()).hasSize(1);
        assertThat(in.links().get(0).anchorText()).isEqualTo("回链");

        when(wikiMapper.selectWikiLinks(ID, 2, "home", "out")).thenReturn(List.of());
        assertThat(service.links(REF, "home", null, DOMAIN, "alice", false, null).direction())
                .isEqualTo("out");
        assertThat(service.links(REF, "home", "  ", DOMAIN, "alice", false, null).direction())
                .isEqualTo("out");

        assertThatThrownBy(() -> service.links(REF, "home", "both", DOMAIN, "alice", false, null))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("invalid_query");
    }

    // ---- card -------------------------------------------------------------------

    @Test
    @DisplayName("card：页数=revision 快照 row_count、embedding 进度、status=published、rev 定版")
    void cardFields() {
        when(artifactMapper.selectRevision(ID, 2)).thenReturn(new RevisionRow(null, 128));

        ArtifactWikiSearchService.WikiArtifactCard card =
                service.card(REF, DOMAIN, "alice", false, null);

        assertThat(card.kind()).isEqualTo("wiki");
        assertThat(card.artifactRef()).isEqualTo(REF);
        assertThat(card.title()).isEqualTo("运维知识 wiki");
        // 谓词保证可见即 published（ArtifactVisibleRow 无 status 列——写死即真相）
        assertThat(card.status()).isEqualTo("published");
        assertThat(card.revision()).isEqualTo(2);
        assertThat(card.pageCount()).isEqualTo(128);
        assertThat(card.embedding().total()).isEqualTo(30L);
        assertThat(card.embedding().done()).isEqualTo(28L);
        assertThat(card.embedding().pending()).isEqualTo(1L);
        assertThat(card.embedding().failed()).isEqualTo(1L);
    }

    @Test
    @DisplayName("card：revision 快照缺失（wiki 发布事务保证定格）→ artifact_revision_missing")
    void cardMissingRevisionSnapshot() {
        when(artifactMapper.selectRevision(ID, 2)).thenReturn(null);

        assertThatThrownBy(() -> service.card(REF, DOMAIN, "alice", false, null))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("artifact_revision_missing");
    }

    // ---- snippet ----------------------------------------------------------------

    @Test
    @DisplayName("snippet：240 code point 截断——emoji 代理对边界不劈半，短文原样")
    void snippetTruncatesOnCodePoints() {
        // 240 个 emoji（每个 1 code point / 2 char）+ 尾巴：截断点恰在代理对之间
        String emoji240 = "😀".repeat(240);
        String content = emoji240 + "尾";
        String snippet = ArtifactWikiSearchService.snippet(content);
        assertThat(snippet).isEqualTo(emoji240);
        assertThat(snippet.codePointCount(0, snippet.length())).isEqualTo(240);
        assertThat(snippet.length()).isEqualTo(480);
        // 不以孤立高代理结尾（劈半即末字符是高代理且无低代理配对）
        assertThat(Character.isHighSurrogate(snippet.charAt(snippet.length() - 1))).isFalse();

        // 短文/空值原样
        assertThat(ArtifactWikiSearchService.snippet("短内容")).isEqualTo("短内容");
        assertThat(ArtifactWikiSearchService.snippet(null)).isEmpty();
    }

    @Test
    @DisplayName("检索结果 snippet 走 code point 截断（超长 content_text 不劈代理对）")
    void searchSnippetAppliesCodePointTruncation() {
        when(embeddingClient.embed("查询")).thenReturn(vec(1024));
        String longContent = "😀".repeat(241);
        when(wikiMapper.searchWikiUnitsDense(eq(ID), eq(2), anyString(), anyInt()))
                .thenReturn(List.of(new WikiUnitHitRow("uA", "p1", "T", longContent, 0.9)));

        ArtifactWikiSearchService.WikiSearchView out =
                service.search(REF, "查询", "dense", null, DOMAIN, "alice", false, null);

        assertThat(out.results()).hasSize(1);
        String snippet = out.results().get(0).snippet();
        assertThat(snippet.codePointCount(0, snippet.length())).isEqualTo(240);
        assertThat(snippet).isEqualTo("😀".repeat(240));
    }
}
