package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.config.ServingProperties;
import com.coremasterkb.serving.domainpack.DomainContext;
import com.coremasterkb.serving.structure.ArtifactQueryService.AggregateView;
import com.coremasterkb.serving.structure.ArtifactQueryService.ArtifactAggregate;
import com.coremasterkb.serving.structure.ArtifactQueryService.ArtifactCard;
import com.coremasterkb.serving.structure.ArtifactQueryService.ArtifactListResult;
import com.coremasterkb.serving.structure.ArtifactQueryService.ArtifactRows;
import com.coremasterkb.serving.structure.ArtifactQueryService.ArtifactSummary;
import com.coremasterkb.serving.structure.ArtifactQueryService.ColumnSchema;
import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * art_ 制品 internal REST（61号批次3 前半——表格知识制品 Agent 消费面）。
 *
 * <p>仅供 mcp_server 容器转发调用（照抄 {@link InternalStructureController} 的
 * X-Internal-Auth 模式）：mcp_server 已按用户密钥验明 username / is_admin，
 * 本组端点信任该身份并做<b>资源级授权</b>（kb_artifacts 成员谓词折叠进
 * mapper 单 SQL，不存在/无权/已停用/跨域同一 {@code artifact_not_found}
 * 404 反探测，由 GlobalExceptionHandler 映射——对齐 kb_not_found 口径）。</p>
 *
 * <p>两个端点：</p>
 * <ul>
 *   <li>{@code POST /api/internal/artifact-query}——art_ ref + 白名单 DSL：
 *       无 query → artifact_card（元信息 + 列能力 + revision 快照）；
 *       有 query → artifact_rows（provenance 随行）/ artifact_aggregate；</li>
 *   <li>{@code POST /api/internal/artifact-list}——发现通道（域内已发布可见制品，
 *       updated_at DESC keyset 翻页）。</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/internal")
public class InternalArtifactController {

    private static final Logger log = LoggerFactory.getLogger(InternalArtifactController.class);

    private final ServingProperties properties;
    private final ArtifactQueryService artifactService;

    public InternalArtifactController(ServingProperties properties,
                                      ArtifactQueryService artifactService) {
        this.properties = properties;
        this.artifactService = artifactService;
    }

    // ------------------------------------------------------------------ endpoints

    /**
     * art_ ref 制品查询。body: {artifact_ref, query{select/where/order_by/limit/
     * cursor/aggregate}|null, domain, username, is_admin, key_id?}——query 缺省 =
     * 卡片视图；key_id 非空 = MCP 钥匙通道（008：须在钥匙开放子集内）。
     */
    @PostMapping("/artifact-query")
    public ResponseEntity<?> artifactQuery(
            @RequestBody JsonNode body,
            @RequestHeader(value = "X-Internal-Auth", required = false) String internalAuth) {
        return withAuth(internalAuth, body, () -> {
            String artifactRef = requiredText(body, "artifact_ref", "artifact_ref 必填");
            Object result = artifactService.query(artifactRef, body.get("query"),
                    text(body, "domain"), text(body, "username"),
                    body.path("is_admin").asBoolean(false),
                    text(body, "key_id"));
            return ResponseEntity.ok(assembleQueryView(artifactRef, result));
        });
    }

    /**
     * 发现通道。body: {domain, username, is_admin, key_id?, cursor?, limit?}——
     * key_id 非空时只列钥匙开放子集内的制品（008）。
     */
    @PostMapping("/artifact-list")
    public ResponseEntity<?> artifactList(
            @RequestBody JsonNode body,
            @RequestHeader(value = "X-Internal-Auth", required = false) String internalAuth) {
        return withAuth(internalAuth, body, () -> {
            ArtifactListResult result = artifactService.list(
                    text(body, "domain"), text(body, "username"),
                    body.path("is_admin").asBoolean(false),
                    text(body, "key_id"),
                    text(body, "cursor"), intOrNull(body.get("limit")));
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("view", "artifacts");
            out.put("artifacts", result.artifacts().stream()
                    .map(this::summaryView).toList());
            out.put("cursor", result.cursor());
            out.put("has_more", result.hasMore());
            return ResponseEntity.ok(out);
        });
    }

    // ------------------------------------------------------------------ view assembly

    private Map<String, Object> assembleQueryView(String artifactRef, Object result) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (result instanceof ArtifactCard card) {
            out.put("view", "artifact_card");
            out.put("artifact_ref", card.artifactRef());
            out.put("title", card.title());
            out.put("description", card.description());
            out.put("usage_note", card.usageNote());
            out.put("revision", card.revision());
            out.put("row_count", card.rowCount());
            out.put("kind", card.kind());
            out.put("columns", card.columns().stream().map(this::columnView).toList());
            return out;
        }
        if (result instanceof ArtifactRows rows) {
            out.put("view", "artifact_rows");
            out.put("artifact_ref", rows.artifactRef());
            out.put("columns", rows.columns().stream().map(this::columnView).toList());
            out.put("rows", rows.rows().stream().map(this::rowView).toList());
            out.put("cursor", rows.cursor());
            out.put("has_more", rows.hasMore());
            return out;
        }
        if (result instanceof ArtifactAggregate agg) {
            out.put("view", "artifact_aggregate");
            out.put("artifact_ref", agg.artifactRef());
            AggregateView a = agg.aggregate();
            Map<String, Object> aggregate = new LinkedHashMap<>();
            aggregate.put("op", a.op());
            aggregate.put("field", a.field());
            aggregate.put("value", a.value());
            aggregate.put("row_count", a.rowCount());
            out.put("aggregate", aggregate);
            return out;
        }
        throw new IllegalStateException("unknown artifact view for " + artifactRef);
    }

    private Map<String, Object> columnView(ColumnSchema c) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("name", c.name());
        out.put("type", c.type());
        out.put("sortable", c.sortable());
        out.put("can_aggregate", c.canAggregate());
        out.put("operations", c.operations());
        return out;
    }

    private Map<String, Object> rowView(ArtifactQueryService.ArtifactRowView r) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("row_key", r.rowKey());
        out.put("data", r.data());
        out.put("provenance", r.provenance());
        return out;
    }

    private Map<String, Object> summaryView(ArtifactSummary s) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("artifact_ref", s.artifactRef());
        out.put("title", s.title());
        out.put("description", s.description());
        out.put("usage_note", s.usageNote());
        out.put("revision", s.revision());
        out.put("row_count", s.rowCount());
        out.put("kind", s.kind());
        return out;
    }

    // ------------------------------------------------------------------ auth + plumbing

    private interface AuthedCall {
        ResponseEntity<?> apply();
    }

    private ResponseEntity<?> withAuth(String internalAuth, JsonNode body, AuthedCall call) {
        String expected = properties.internalAuth() != null
                ? properties.internalAuth().secret() : "";
        if (expected == null || expected.isBlank()) {
            log.warn("[internal-api] serving.internal-auth.secret 未配置——端点拒绝服务（503）");
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(Map.of("error", "internal_auth_not_configured"));
        }
        if (internalAuth == null || !MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                internalAuth.getBytes(StandardCharsets.UTF_8))) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Map.of("error", "unauthenticated"));
        }

        DomainContext.set(text(body, "domain"));
        try {
            return call.apply();
        } finally {
            DomainContext.clear();
        }
    }

    @ExceptionHandler(StructureToolException.class)
    public ResponseEntity<?> handleTyped(StructureToolException ex) {
        Map<String, Object> error = new LinkedHashMap<>();
        error.put("code", ex.code());
        error.put("message", ex.getMessage());
        if (!ex.details().isEmpty()) {
            error.put("details", ex.details());
        }
        return ResponseEntity.status(ex.status()).body(Map.of("error", error));
    }

    // ------------------------------------------------------------------ body parsing

    private static String text(JsonNode body, String field) {
        if (body == null) return null;
        JsonNode v = body.get(field);
        return (v != null && v.isTextual() && !v.asText().isBlank()) ? v.asText() : null;
    }

    private static String requiredText(JsonNode body, String field, String message) {
        String v = text(body, field);
        if (v == null) {
            throw StructureToolException.invalidRef(message);
        }
        return v;
    }

    private static Integer intOrNull(JsonNode v) {
        return (v != null && v.isNumber()) ? v.asInt() : null;
    }
}
