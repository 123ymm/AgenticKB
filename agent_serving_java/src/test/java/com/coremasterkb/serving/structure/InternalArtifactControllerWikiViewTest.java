package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.config.ServingProperties;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * 63号批次4 internal 端点 wiki 视图装配契约：assembleQueryView 对 wiki 四视图
 * （card/search/page/links）各自装 Map（snake_case 键，照 ArtifactCard 装配风格）——
 * 缺分支会 IllegalStateException 500（改造前只认三种表格视图）。
 * 表格三视图装配由既有契约守护（ArtifactQueryServiceTest XML/端点行为），此处不重复。
 */
@DisplayName("InternalArtifactController wiki 视图装配")
class InternalArtifactControllerWikiViewTest {

    private static final String REF = "art_" + "5c4b3a2f".repeat(4);
    private static final ObjectMapper JSON = new ObjectMapper();

    private ArtifactQueryService artifactService;
    private ArtifactWikiSearchService wikiService;
    private InternalArtifactController controller;

    @BeforeEach
    void setUp() {
        artifactService = mock(ArtifactQueryService.class);
        wikiService = mock(ArtifactWikiSearchService.class);
        controller = new InternalArtifactController(
                new ServingProperties(null, null, null, null, null, null,
                        new ServingProperties.InternalAuth("s3cret")),
                artifactService, wikiService);
    }

    private static JsonNode body(String json) {
        try {
            return JSON.readTree(json);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> ok(ResponseEntity<?> resp) {
        assertThat(resp.getStatusCode().value()).isEqualTo(200);
        return (Map<String, Object>) resp.getBody();
    }

    @Test
    @DisplayName("artifact-query + wiki query=null → wiki_artifact_card 装配（embedding 四键）")
    void cardViewAssembly() {
        when(artifactService.query(eq(REF), isNull(), eq("odn"), eq("alice"),
                eq(false), isNull()))
                .thenReturn(new ArtifactWikiSearchService.WikiArtifactCard(
                        "wiki", REF, "运维知识 wiki", "published", 2, 12,
                        new ArtifactWikiSearchService.WikiEmbeddingProgress(30, 28, 1, 1)));

        Map<String, Object> out = ok(controller.artifactQuery(
                body("{\"artifact_ref\":\"" + REF + "\",\"domain\":\"odn\",\"username\":\"alice\"}"),
                "s3cret"));

        assertThat(out.get("view")).isEqualTo("wiki_artifact_card");
        assertThat(out.get("artifact_ref")).isEqualTo(REF);
        assertThat(out.get("kind")).isEqualTo("wiki");
        assertThat(out.get("status")).isEqualTo("published");
        assertThat(out.get("page_count")).isEqualTo(12);
        Map<String, Object> embedding = (Map<String, Object>) out.get("embedding");
        assertThat(embedding).containsEntry("total", 30L)
                .containsEntry("done", 28L)
                .containsEntry("pending", 1L)
                .containsEntry("failed", 1L);
    }

    @Test
    @DisplayName("artifact-wiki-search 端点 → wiki_search 装配（degraded_dense/results 行键）")
    void searchViewAssembly() {
        when(wikiService.search(REF, "接口报错", "fusion", 5, "odn", "alice", false, null))
                .thenReturn(new ArtifactWikiSearchService.WikiSearchView(REF, 2, true,
                        List.of(new ArtifactWikiSearchService.WikiSearchHit(
                                "home", "首页", "u-1", "片段", 0.0325,
                                List.of("wiki_dense", "wiki_keyword")))));

        ResponseEntity<?> resp = controller.artifactWikiSearch(body(
                        "{\"artifact_ref\":\"" + REF + "\",\"q\":\"接口报错\",\"mode\":\"fusion\","
                                + "\"top_k\":5,\"domain\":\"odn\",\"username\":\"alice\"}"),
                "s3cret");
        Map<String, Object> out = ok(resp);

        assertThat(out.get("view")).isEqualTo("wiki_search");
        assertThat(out.get("revision")).isEqualTo(2);
        assertThat(out.get("degraded_dense")).isEqualTo(true);
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        assertThat(results).hasSize(1);
        assertThat(results.get(0)).containsEntry("page_key", "home")
                .containsEntry("title", "首页")
                .containsEntry("unit_key", "u-1")
                .containsEntry("snippet", "片段")
                .containsKey("score")
                .containsEntry("channels", List.of("wiki_dense", "wiki_keyword"));
    }

    @Test
    @DisplayName("artifact-wiki-page 端点 → wiki_page 装配（page 整读 + links.out/in 摘要）")
    void pageViewAssembly() {
        when(wikiService.readPage(REF, "home", "odn", "alice", false, null))
                .thenReturn(new ArtifactWikiSearchService.WikiPageView(REF, 2,
                        new ArtifactWikiSearchService.WikiPageDetail(
                                "home", "首页", "overview", Map.of("tags", List.of("net")),
                                "# 正文", List.of(Map.of("source", "kb-1/doc-9")), "mined",
                                OffsetDateTime.parse("2026-10-20T09:30:00+08:00")),
                        new ArtifactWikiSearchService.WikiPageLinks(
                                List.of(new ArtifactWikiSearchService.WikiLinkItem(
                                        "home", "p2", "锚", "active")),
                                List.of())));

        Map<String, Object> out = ok(controller.artifactWikiPage(body(
                        "{\"artifact_ref\":\"" + REF + "\",\"page_key\":\"home\","
                                + "\"domain\":\"odn\",\"username\":\"alice\"}"),
                "s3cret"));

        assertThat(out.get("view")).isEqualTo("wiki_page");
        Map<String, Object> page = (Map<String, Object>) out.get("page");
        assertThat(page).containsEntry("page_key", "home")
                .containsEntry("title", "首页")
                .containsEntry("page_type", "overview")
                .containsEntry("frontmatter", Map.of("tags", List.of("net")))
                .containsEntry("body_md", "# 正文")
                .containsEntry("origin", "mined")
                .containsEntry("updated_at", "2026-10-20T09:30+08:00");
        Map<String, Object> links = (Map<String, Object>) out.get("links");
        assertThat((List<Map<String, Object>>) links.get("out")).hasSize(1);
        assertThat(((List<Map<String, Object>>) links.get("out")).get(0))
                .containsEntry("from_page", "home")
                .containsEntry("to_page", "p2")
                .containsEntry("anchor_text", "锚")
                .containsEntry("status", "active");
        assertThat((List<?>) links.get("in")).isEmpty();
    }

    @Test
    @DisplayName("artifact-wiki-links 端点 → wiki_links 装配（direction 透传）")
    void linksViewAssembly() {
        when(wikiService.links(REF, "home", "in", "odn", "alice", false, null))
                .thenReturn(new ArtifactWikiSearchService.WikiLinksView(REF, 2, "home", "in",
                        List.of(new ArtifactWikiSearchService.WikiLinkItem(
                                "p9", "home", "回链", "active"))));

        Map<String, Object> out = ok(controller.artifactWikiLinks(body(
                        "{\"artifact_ref\":\"" + REF + "\",\"page_key\":\"home\","
                                + "\"direction\":\"in\",\"domain\":\"odn\",\"username\":\"alice\"}"),
                "s3cret"));

        assertThat(out.get("view")).isEqualTo("wiki_links");
        assertThat(out.get("page_key")).isEqualTo("home");
        assertThat(out.get("direction")).isEqualTo("in");
        List<Map<String, Object>> items = (List<Map<String, Object>>) out.get("links");
        assertThat(items).hasSize(1);
        assertThat(items.get(0)).containsEntry("from_page", "p9")
                .containsEntry("to_page", "home");
    }

    @Test
    @DisplayName("鉴权：X-Internal-Auth 缺失/错值 → 401；secret 未配置 → 503")
    void authEnforcedOnWikiEndpoints() {
        ResponseEntity<?> noAuth = controller.artifactWikiSearch(body(
                "{\"artifact_ref\":\"" + REF + "\",\"q\":\"x\",\"domain\":\"odn\"}"), null);
        assertThat(noAuth.getStatusCode().value()).isEqualTo(401);

        ResponseEntity<?> badAuth = controller.artifactWikiPage(body(
                "{\"artifact_ref\":\"" + REF + "\",\"page_key\":\"home\",\"domain\":\"odn\"}"),
                "wrong");
        assertThat(badAuth.getStatusCode().value()).isEqualTo(401);

        InternalArtifactController bare = new InternalArtifactController(
                new ServingProperties(null, null, null, null, null, null, null),
                artifactService, wikiService);
        assertThat(bare.artifactWikiLinks(body(
                        "{\"artifact_ref\":\"" + REF + "\",\"page_key\":\"home\",\"domain\":\"odn\"}"),
                "s3cret").getStatusCode().value()).isEqualTo(503);
    }

    @Test
    @DisplayName("错误映射：invalid_query 经 handleTyped → 400 + code/message 透传")
    void typedErrorMapping() {
        // 直调不经 Spring MVC 派发（@ExceptionHandler 不参与）——typed 错误到 HTTP 的
        // 映射点就是 handleTyped 本身，直调断言其契约
        ResponseEntity<?> resp = controller.handleTyped(new StructureToolException(
                "invalid_query", org.springframework.http.HttpStatus.BAD_REQUEST,
                "q 必填且不能为空白（wiki 检索文本）", Map.of()));

        assertThat(resp.getStatusCode().value()).isEqualTo(400);
        @SuppressWarnings("unchecked")
        Map<String, Object> err = (Map<String, Object>) ((Map<String, Object>) resp.getBody())
                .get("error");
        assertThat(err.get("code")).isEqualTo("invalid_query");
        assertThat(err.get("message")).isEqualTo("q 必填且不能为空白（wiki 检索文本）");
    }
}
