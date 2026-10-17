package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.AggregateValueRow;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.ArtifactRowValue;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.ArtifactSummaryRow;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.ArtifactVisibleRow;
import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper.RevisionRow;
import com.coremasterkb.serving.operator.mapper.StructureToolMapper.Criterion;
import com.coremasterkb.serving.structure.StructuredQueryService.Aggregate;
import com.coremasterkb.serving.structure.StructuredQueryService.OrderClause;
import com.coremasterkb.serving.structure.StructuredQueryService.QuerySpec;
import com.coremasterkb.serving.structure.StructuredQueryService.WhereClause;
import com.coremasterkb.serving.util.JsonUtils;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * art_ 制品查询服务（61号批次3 前半——表格知识制品 Agent 消费面）。
 *
 * <p>消费模型与 {@link StructuredQueryService}（st_ 通道）同源：</p>
 * <ul>
 *   <li><b>DSL</b>：复用 {@link StructureQueryDsl#parseSpec}（形状严格校验）与
 *       {@link StructuredQueryService} 的操作符白名单/类型规则（包内共享常量，
 *       不搞第二套语义）；字段名 = schema_json 列名，白名单校验后仅以绑定参数
 *       进入 SQL（{@code data->>#{field}}）。</li>
 *   <li><b>alias 展开（用例⑨）</b>：DSL 字段名先经 revision 快照 schema_json 的
 *       alias 数组映射到当前列名，再过白名单——列改名/回滚后旧名兼容读。</li>
 *   <li><b>权限谓词</b>：mapper 单 SQL 折叠（status='published' AND 同域 AND
 *       admin/成员/owner），不存在/无权/已停用/跨域同一
 *       {@code IllegalArgumentException("artifact_not_found")}（GlobalExceptionHandler
 *       映射 404——对齐 kb_not_found 反探测口径；admin 仍受 status/domain 约束）。</li>
 *   <li><b>三视图</b>：无 query → artifact_card（列能力 + revision 快照元信息）；
 *       有 query → artifact_rows（provenance 随行，用例②）/ artifact_aggregate。</li>
 *   <li><b>翻页</b>：行=(sort_value, ordinal, row_key) keyset（默认序=ordinal+row_key）；
 *       发现=(updated_at DESC, artifact_id) keyset。cursor 是本服务签发的 base64url
 *       JSON，非法/与 order_by 不一致按 typed error 拒绝（不错页、不静默从头）。</li>
 * </ul>
 */
@Service
public class ArtifactQueryService {

    static final String REF_PREFIX = "art_";
    static final int MAX_LIMIT = 200;
    static final int DEFAULT_LIMIT = 50;
    /** where 子句数与 in 数组长度上限（29号 R0 复审护栏，与 st_ 同值）。 */
    static final int MAX_WHERE_CLAUSES = 32;
    static final int MAX_IN_VALUES = 200;
    /** 无过滤聚合的行数护栏（与 st_ 同值；revision.row_count 已是发布定格事实，无需扫描）。 */
    static final int UNFILTERED_AGGREGATE_ROW_GUARD = 50_000;

    private static final Set<String> NUMERIC_TYPES = Set.of("int", "float");
    private static final Set<String> FILTER_OPS = StructuredQueryService.FILTER_OPS;
    private static final Set<String> AGG_OPS = StructuredQueryService.AGG_OPS;
    private static final Set<String> NUMERIC_ONLY_OPS = StructuredQueryService.NUMERIC_ONLY_OPS;

    private final ArtifactToolMapper mapper;

    public ArtifactQueryService(ArtifactToolMapper mapper) {
        this.mapper = mapper;
    }

    // ------------------------------------------------------------------ DTO

    public record ColumnSchema(String name, String type, boolean sortable,
                               boolean canAggregate, List<String> operations) {}

    public record ArtifactCard(String artifactRef, String title, String description,
                               String usageNote, int revision, int rowCount, String kind,
                               List<ColumnSchema> columns) {}

    public record ArtifactRowView(String rowKey, Map<String, Object> data,
                                  List<Object> provenance) {}

    public record ArtifactRows(String artifactRef, List<ColumnSchema> columns,
                               List<ArtifactRowView> rows, String cursor, boolean hasMore) {}

    public record AggregateView(String op, String field, Object value, long rowCount) {}

    public record ArtifactAggregate(String artifactRef, AggregateView aggregate) {}

    public record ArtifactSummary(String artifactRef, String title, String description,
                                  String usageNote, int revision, int rowCount, String kind) {}

    public record ArtifactListResult(List<ArtifactSummary> artifacts, String cursor,
                                     boolean hasMore) {}

    /** 行 keyset cursor 的解码形态；{@code ordered} = 签发时带自定义排序。 */
    record RowCursor(int ordinal, String rowKey, String sortValue, boolean ordered) {}

    /** 发现通道 keyset cursor 的解码形态。 */
    record ListCursor(OffsetDateTime updatedAt, String artifactId) {}

    /** revision 快照里的列定义（alias 供旧名兼容读）。 */
    record ColumnDef(String name, String type, List<String> aliases) {}

    // ------------------------------------------------------------------ entry

    /**
     * art_ ref 制品查询：queryNode 缺省/null → artifact_card；否则按 DSL 解析成
     * rows / aggregate 视图。
     *
     * @throws IllegalArgumentException("artifact_not_found") 不存在/无权/已停用/跨域
     *         （GlobalExceptionHandler 映射 404）
     * @throws StructureToolException DSL 校验失败（invalid_ref/unknown_field/
     *         type_mismatch/unsupported_operation/result_too_large）
     */
    public Object query(String artifactRef, JsonNode queryNode, String domain,
                        String username, boolean isAdmin) {
        requireDomain(domain);
        String artifactId = parseRef(artifactRef);

        ArtifactVisibleRow artifact = mapper.selectVisibleArtifact(artifactId, domain,
                username, isAdmin);
        if (artifact == null) {
            // 404 反探测：不存在/非成员/deprecated/跨域同一响应（谓词折叠在 SQL）
            throw new IllegalArgumentException("artifact_not_found");
        }

        int revision = artifact.currentRevision() == null ? 0 : artifact.currentRevision();
        RevisionRow snapshot = mapper.selectRevision(artifactId, revision);
        if (snapshot == null) {
            // published 事务保证 current_revision≥1 且快照同行落库——缺失即数据完整性问题
            throw new IllegalStateException("artifact_revision_missing");
        }
        List<ColumnDef> columns = parseColumns(snapshot.schemaJson());
        List<ColumnSchema> schemas = columns.stream().map(ArtifactQueryService::schemaOf).toList();

        if (queryNode == null || queryNode.isNull()) {
            return new ArtifactCard(artifactRef, artifact.title(), artifact.description(),
                    artifact.usageNote(), revision, snapshot.rowCount() == null
                            ? 0 : snapshot.rowCount(),
                    artifact.kind(), schemas);
        }
        if (!"table".equals(artifact.kind())) {
            throw StructureToolException.unsupportedOperation(
                    "仅 table 类制品支持 DSL 查询（wiki 走来源挂载阅读）", Map.of());
        }
        if (columns.isEmpty()) {
            throw StructureToolException.structuredQueryUnavailable(
                    "该制品没有可用列（schema 快照为空），无法查询");
        }

        QuerySpec spec = StructureQueryDsl.parseSpec(queryNode);
        Table table = new Table(artifactId, revision, columns, schemas,
                snapshot.rowCount() == null ? 0 : snapshot.rowCount());

        List<Criterion> criteria = new ArrayList<>();
        if (spec.where() != null) {
            if (spec.where().size() > MAX_WHERE_CLAUSES) {
                throw StructureToolException.resultTooLarge(
                        "where 子句超上限 " + MAX_WHERE_CLAUSES);
            }
            for (WhereClause w : spec.where()) {
                criteria.add(toCriterion(w, table));
            }
        }
        if (spec.aggregate() != null && spec.aggregate().op() != null) {
            return aggregateMode(artifactRef, spec.aggregate(), criteria, table);
        }
        return rowsMode(artifactRef, spec, criteria, table);
    }

    /**
     * 发现通道：域内已发布且可见的制品清单（updated_at DESC keyset 翻页）。
     */
    public ArtifactListResult list(String domain, String username, boolean isAdmin,
                                   String cursor, Integer limit) {
        requireDomain(domain);
        int effective = normalizedLimit(limit);
        ListCursor after = decodeListCursor(cursor);

        List<ArtifactSummaryRow> fetched = mapper.selectArtifactSummaries(
                domain, username, isAdmin,
                after == null ? null : after.updatedAt(),
                after == null ? null : after.artifactId(),
                effective + 1);
        boolean hasMore = fetched.size() > effective;
        int end = Math.min(fetched.size(), effective);
        List<ArtifactSummary> out = new ArrayList<>(end);
        for (int i = 0; i < end; i++) {
            ArtifactSummaryRow r = fetched.get(i);
            out.add(new ArtifactSummary(REF_PREFIX + r.artifactId(), r.title(),
                    r.description(), r.usageNote(),
                    r.currentRevision() == null ? 0 : r.currentRevision(),
                    r.rowCount() == null ? 0 : r.rowCount(), r.kind()));
        }
        String next = hasMore
                ? encodeListCursor(fetched.get(end - 1).updatedAt(),
                        fetched.get(end - 1).artifactId())
                : null;
        return new ArtifactListResult(out, next, hasMore);
    }

    // ------------------------------------------------------------------ rows mode

    private ArtifactRows rowsMode(String artifactRef, QuerySpec spec,
                                  List<Criterion> criteria, Table table) {
        String orderField = null;
        String orderDir = "asc";
        boolean numericOrder = false;
        if (spec.order_by() != null && !spec.order_by().isEmpty()) {
            if (spec.order_by().size() > 1) {
                throw StructureToolException.unsupportedOperation(
                        "order_by 仅支持单字段稳定排序", Map.of());
            }
            OrderClause o = spec.order_by().get(0);
            ColumnDef f = requireColumn(o.field(), table);
            if (o.direction() != null && !"asc".equalsIgnoreCase(o.direction())
                    && !"desc".equalsIgnoreCase(o.direction())) {
                throw StructureToolException.typeMismatch(o.field() + ".direction", "asc|desc");
            }
            orderField = f.name();
            orderDir = "desc".equalsIgnoreCase(o.direction()) ? "desc" : "asc";
            numericOrder = isNumeric(f);
        }

        List<String> select = validatedSelect(spec.select(), table);

        int limit = normalizedLimit(spec.limit());
        RowCursor after = decodeRowCursor(spec.cursor());
        boolean ordered = orderField != null;
        if (after != null && after.ordered() != ordered) {
            // 错页防线：keyset 语义绑定签发时的排序——换排序必须从头（否则跳页/重页）
            throw StructureToolException.unsupportedOperation(
                    "cursor 与 order_by 不匹配——请原样保留签发该 cursor 时的 order_by"
                            + "（或不带 cursor 重新发起）", Map.of());
        }
        if (numericOrder && after != null && after.sortValue() != null
                && !isCastableNumeric(after.sortValue())) {
            // 伪造 cursor 防线：sort_value 会被 mapper 包进 CAST(... AS numeric)——
            // 非法/超值域串在 Java 侧提前 typed 400，不让 PG 报 500（61号 C4）。
            // （sortValue==null 是合法 NULL 块游标，不走 CAST，放行。）
            throw badCursor();
        }

        List<ArtifactRowValue> fetched = mapper.selectArtifactRows(
                table.artifactId(), table.revision(), criteria,
                orderField, orderDir, numericOrder,
                after == null ? 0 : after.ordinal(),
                after == null ? null : after.rowKey(),
                after == null ? null : after.sortValue(),
                limit + 1);
        boolean hasMore = fetched.size() > limit;
        int end = Math.min(fetched.size(), limit);
        List<ArtifactRowView> rows = new ArrayList<>(end);
        for (int i = 0; i < end; i++) {
            rows.add(toRowView(fetched.get(i), select));
        }
        String next = null;
        if (hasMore) {
            ArtifactRowValue last = fetched.get(end - 1);
            next = encodeRowCursor(last.ordinal() == null ? 0 : last.ordinal(),
                    last.rowKey(), toSortString(last.sortValue()), ordered);
        }
        return new ArtifactRows(artifactRef, table.schemas(), rows, next, hasMore);
    }

    // ------------------------------------------------------------------ aggregate mode

    private ArtifactAggregate aggregateMode(String artifactRef, Aggregate aggregate,
                                            List<Criterion> criteria, Table table) {
        String op = aggregate.op();
        if (!AGG_OPS.contains(op)) {
            throw StructureToolException.unsupportedOperation(
                    "未知聚合操作: " + op + "；允许：" + AGG_OPS,
                    Map.of("allowed", AGG_OPS.stream().sorted().toList()));
        }
        ColumnDef field = null;
        if (!"count".equals(op)) {
            if (aggregate.field() == null) {
                throw StructureToolException.typeMismatch(
                        "aggregate.field", "字段名（count 之外必填）");
            }
            field = requireColumn(aggregate.field(), table);
            if (!isNumeric(field) && !isDate(field)) {
                throw StructureToolException.unsupportedOperation(
                        "字段 " + field.name() + " 是文本列，仅允许 count 聚合",
                        Map.of("field", field.name(), "allowed", List.of("count")));
            }
            // date：ISO 文本字典序可 min/max，但不可 sum/avg
            if (isDate(field) && ("sum".equals(op) || "avg".equals(op))) {
                throw StructureToolException.unsupportedOperation(
                        "字段 " + field.name() + " 是日期列，仅允许 count/min/max 聚合",
                        Map.of("field", field.name(), "allowed", List.of("count", "min", "max")));
            }
        }
        if (criteria.isEmpty() && table.rowCount() > UNFILTERED_AGGREGATE_ROW_GUARD) {
            throw StructureToolException.resultTooLarge(
                    "无过滤条件的全表聚合超出护栏（" + table.rowCount() + " 行）——请先加 where 过滤");
        }

        boolean textualAggregate = field != null && isDate(field);
        AggregateValueRow agg = mapper.aggregateArtifactRows(
                table.artifactId(), table.revision(), criteria, op,
                field == null ? null : field.name(), textualAggregate);
        return new ArtifactAggregate(artifactRef, new AggregateView(op,
                field == null ? null : field.name(),
                agg == null || agg.value() == null ? null : agg.value(),
                agg == null || agg.rowCount() == null ? 0L : agg.rowCount()));
    }

    // ------------------------------------------------------------------ DSL → criterion（st_ 同规则）

    private Criterion toCriterion(WhereClause w, Table table) {
        if (w.field() == null || w.op() == null) {
            throw StructureToolException.unsupportedOperation("where 子句缺少 field/op", Map.of());
        }
        ColumnDef f = requireColumn(w.field(), table);
        String op = w.op();
        if (!FILTER_OPS.contains(op)) {
            throw StructureToolException.unsupportedOperation(
                    "未知过滤操作: " + op + "；允许：" + FILTER_OPS,
                    Map.of("field", f.name(), "allowed", schemaOf(f).operations()));
        }
        boolean numeric = isNumeric(f);
        boolean date = isDate(f);
        boolean ordinal = numeric || date;
        if (NUMERIC_ONLY_OPS.contains(op) && !ordinal) {
            throw StructureToolException.unsupportedOperation(
                    "文本列不支持 " + op + "（仅数值/日期列）",
                    Map.of("field", f.name(), "allowed", schemaOf(f).operations()));
        }
        if ("contains".equals(op) && ordinal) {
            throw StructureToolException.unsupportedOperation(
                    "数值/日期列不支持 contains（文本匹配）",
                    Map.of("field", f.name(), "allowed", schemaOf(f).operations()));
        }
        if (date && !"is_null".equals(op) && !isoDateValues(w.field(), op, w.value())) {
            throw StructureToolException.typeMismatch(w.field(), "日期（YYYY-MM-DD）");
        }
        if ("is_null".equals(op)) {
            return new Criterion(f.name(), "is_null", null, null, numeric);
        }
        if ("in".equals(op)) {
            if (w.value() == null || !w.value().isArray() || w.value().isEmpty()) {
                throw StructureToolException.typeMismatch(w.field() + " in", "非空数组");
            }
            if (w.value().size() > MAX_IN_VALUES) {
                throw StructureToolException.resultTooLarge(
                        "in 数组超上限 " + MAX_IN_VALUES + "（field=" + w.field() + "）");
            }
            List<String> values = new ArrayList<>();
            for (JsonNode item : w.value()) {
                values.add(coerce(w.field(), item, numeric));
            }
            return new Criterion(f.name(), "in", null, values, numeric);
        }
        if (w.value() == null || w.value().isNull()) {
            throw StructureToolException.typeMismatch(w.field(), numeric ? "数值" : "字符串");
        }
        String coerced = coerce(w.field(), w.value(), numeric);
        return new Criterion(f.name(), op,
                numeric ? Double.parseDouble(coerced) : coerced, null, numeric);
    }

    /** 数值列：number 或可解析数字串 → 规范数字串（去千分位）；其余列：必须字符串。 */
    private static String coerce(String field, JsonNode value, boolean numeric) {
        if (numeric) {
            if (value.isNumber()) {
                return value.decimalValue().toPlainString();
            }
            if (value.isTextual() && StructuredQueryService.isNumber(value.asText())) {
                return value.asText().replace(",", "").trim();
            }
            throw StructureToolException.typeMismatch(field, "number");
        }
        if (value.isTextual()) {
            return value.asText();
        }
        throw StructureToolException.typeMismatch(field, "string");
    }

    /** date 过滤值必须真实日历 ISO（YYYY-MM-DD）——形状/日历非法都拒绝，不静默文本包含。 */
    private static boolean isoDateValues(String field, String op, JsonNode value) {
        if ("in".equals(op)) {
            for (JsonNode item : value == null ? List.<JsonNode>of() : value) {
                if (item == null || !isIsoDate(item.asText())) {
                    return false;
                }
            }
            return true;
        }
        return value != null && !value.isNull() && isIsoDate(value.asText());
    }

    private static boolean isIsoDate(String v) {
        if (v == null) {
            return false;
        }
        try {
            LocalDate.parse(v);
            return true;
        } catch (DateTimeParseException e) {
            return false;
        }
    }

    private static List<String> validatedSelect(List<String> select, Table table) {
        if (select == null || select.isEmpty()) {
            return table.columns().stream().map(ColumnDef::name).toList(); // 缺省=全部列
        }
        List<String> out = new ArrayList<>();
        for (String s : select) {
            String resolved = requireColumn(s, table).name();
            if (!out.contains(resolved)) {
                out.add(resolved);
            }
        }
        return out;
    }

    // ------------------------------------------------------------------ 列解析 / alias / 白名单

    private static List<ColumnDef> parseColumns(String schemaJson) {
        if (schemaJson == null || schemaJson.isBlank()) {
            return List.of();
        }
        List<ColumnDef> out = new ArrayList<>();
        try {
            for (JsonNode c : JsonUtils.mapper().readTree(schemaJson)) {
                String name = c.path("column_name").asText(null);
                if (name == null || name.isBlank()) {
                    continue;
                }
                List<String> aliases = new ArrayList<>();
                JsonNode alias = c.path("alias");
                if (alias.isArray()) {
                    alias.forEach(a -> {
                        if (a.isTextual() && !a.asText().isBlank()) {
                            aliases.add(a.asText());
                        }
                    });
                }
                out.add(new ColumnDef(name, c.path("column_type").asText("text"), aliases));
            }
        } catch (Exception e) {
            // schema 快照由发布事务从 kb_table_columns 定格写入——解析失败是完整性问题
            throw new IllegalStateException("artifact_schema_invalid", e);
        }
        return out;
    }

    /** 列能力（与 st_ FieldSchema 同口径，按 kb_table_columns 类型映射）。 */
    private static ColumnSchema schemaOf(ColumnDef c) {
        boolean numeric = isNumeric(c);
        boolean date = isDate(c);
        List<String> operations = numeric
                ? List.of("eq", "ne", "lt", "lte", "gt", "gte", "in", "is_null",
                          "count", "sum", "min", "max", "avg")
                : date
                        ? List.of("eq", "ne", "lt", "lte", "gt", "gte", "in", "is_null",
                                  "count", "min", "max")
                        : List.of("eq", "ne", "in", "contains", "is_null", "count");
        return new ColumnSchema(c.name(), c.type(), true, numeric || date, operations);
    }

    private static boolean isNumeric(ColumnDef c) {
        return NUMERIC_TYPES.contains(c.type());
    }

    private static boolean isDate(ColumnDef c) {
        return "date".equals(c.type());
    }

    /**
     * 字段解析：当前列名直取；否则按 schema_json 的 alias 数组映射（用例⑨：旧列名
     * 兼容读）；两者皆 miss → unknown_field（typed 400，allowed_fields 带当前清单）。
     */
    private static ColumnDef requireColumn(String field, Table table) {
        ColumnDef direct = table.byName().get(field);
        if (direct != null) {
            return direct;
        }
        String mapped = table.aliasToName().get(field);
        if (mapped != null) {
            return table.byName().get(mapped);
        }
        throw StructureToolException.unknownField(field,
                table.columns().stream().map(ColumnDef::name).toList());
    }

    // ------------------------------------------------------------------ row view

    private static ArtifactRowView toRowView(ArtifactRowValue row, List<String> select) {
        Map<String, Object> data = row.dataJson() == null
                ? Map.of() : JsonUtils.safeJsonParse(row.dataJson());
        Map<String, Object> projected = new LinkedHashMap<>();
        for (String col : select) {
            projected.put(col, data.get(col)); // 保留 jsonb 原生值类型
        }
        return new ArtifactRowView(row.rowKey(), projected, parseJsonArray(row.provenanceJson()));
    }

    private static List<Object> parseJsonArray(String json) {
        if (json == null || json.isBlank()) {
            return List.of();
        }
        try {
            return JsonUtils.mapper().readValue(json,
                    JsonUtils.mapper().getTypeFactory().constructCollectionType(List.class, Object.class));
        } catch (Exception e) {
            return List.of();
        }
    }

    /** cursor 里的排序值统一字符串形态（numeric → 可回 cast 的规范串）。 */
    private static String toSortString(Object sortValue) {
        if (sortValue == null) {
            return null;
        }
        if (sortValue instanceof Number n) {
            return new BigDecimal(n.toString()).toPlainString();
        }
        return String.valueOf(sortValue);
    }

    // ------------------------------------------------------------------ cursor codec

    /** PG numeric 整数部分位数上限（超出即 overflow，同样是 500 面）；cursor 预校验用。 */
    private static final int MAX_NUMERIC_INTEGER_DIGITS = 131_072;

    /**
     * cursor 排序值可否安全进 mapper 的 {@code CAST(... AS numeric)}：合法 cursor 恒为
     * 本服务签发的 BigDecimal 平文（无千分位/空白）；严格十进制文法 + 整数位数在
     * PG numeric 值域内才放行——其余视为伪造，提前 typed 400（C4，防 PG 报 500）。
     */
    private static boolean isCastableNumeric(String value) {
        if (value == null || value.isBlank()) {
            return false;
        }
        try {
            BigDecimal v = new BigDecimal(value.trim());
            return v.precision() - v.scale() <= MAX_NUMERIC_INTEGER_DIGITS;
        } catch (NumberFormatException e) {
            return false;
        }
    }

    static String encodeRowCursor(int ordinal, String rowKey, String sortValue, boolean ordered) {
        ObjectNode n = JsonUtils.mapper().createObjectNode();
        n.put("o", ordinal);
        n.put("k", rowKey == null ? "" : rowKey);
        if (sortValue == null) {
            n.putNull("s");
        } else {
            n.put("s", sortValue);
        }
        n.put("q", ordered);
        return encode(n.toString());
    }

    static RowCursor decodeRowCursor(String cursor) {
        if (cursor == null || cursor.isBlank()) {
            return null;
        }
        JsonNode n = decodeCursorJson(cursor);
        if (!n.has("o") || !n.get("o").isInt() || !n.has("k") || !n.get("k").isTextual()
                || !n.has("q") || !n.get("q").isBoolean()) {
            throw badCursor();
        }
        JsonNode s = n.get("s");
        if (s != null && !s.isNull() && !s.isTextual()) {
            throw badCursor();
        }
        return new RowCursor(n.get("o").asInt(), n.get("k").asText(),
                s == null || s.isNull() ? null : s.asText(), n.get("q").asBoolean());
    }

    static String encodeListCursor(OffsetDateTime updatedAt, String artifactId) {
        ObjectNode n = JsonUtils.mapper().createObjectNode();
        n.put("t", updatedAt == null ? "" : updatedAt.toString());
        n.put("i", artifactId == null ? "" : artifactId);
        return encode(n.toString());
    }

    static ListCursor decodeListCursor(String cursor) {
        if (cursor == null || cursor.isBlank()) {
            return null;
        }
        JsonNode n = decodeCursorJson(cursor);
        if (!n.has("t") || !n.get("t").isTextual() || !n.has("i") || !n.get("i").isTextual()) {
            throw badCursor();
        }
        OffsetDateTime ts;
        try {
            ts = OffsetDateTime.parse(n.get("t").asText());
        } catch (DateTimeParseException e) {
            throw badCursor();
        }
        return new ListCursor(ts, n.get("i").asText());
    }

    private static String encode(String raw) {
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(raw.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static JsonNode decodeCursorJson(String cursor) {
        try {
            String raw = new String(Base64.getUrlDecoder().decode(cursor),
                    java.nio.charset.StandardCharsets.UTF_8);
            JsonNode n = JsonUtils.mapper().readTree(raw);
            if (!n.isObject()) {
                throw badCursor();
            }
            return n;
        } catch (IllegalArgumentException | java.io.IOException e) {
            throw badCursor();
        }
    }

    private static StructureToolException badCursor() {
        return StructureToolException.unsupportedOperation(
                "cursor 非法——请原样传回上次响应的 cursor，或不传从头开始", Map.of());
    }

    // ------------------------------------------------------------------ misc

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

    private static void requireDomain(String domain) {
        if (domain == null || domain.isBlank()) {
            throw StructureToolException.unsupportedOperation(
                    "缺少 domain（制品按域路由）", Map.of());
        }
    }

    private static int normalizedLimit(Integer limit) {
        int effective = limit == null ? DEFAULT_LIMIT : limit;
        if (effective <= 0) {
            throw StructureToolException.resultTooLarge(
                    "limit 必须为 1-" + MAX_LIMIT + " 的正整数（当前 " + effective + "）");
        }
        if (effective > MAX_LIMIT) {
            throw StructureToolException.resultTooLarge(
                    "limit 超上限 " + MAX_LIMIT + "——请用 cursor 分页或收窄过滤条件");
        }
        return effective;
    }

    /** 单制品解析上下文（列定义 + 索引 + revision/rowCount 事实）。 */
    private record Table(String artifactId, int revision, List<ColumnDef> columns,
                         List<ColumnSchema> schemas, int rowCount,
                         Map<String, ColumnDef> byName, Map<String, String> aliasToName) {
        Table(String artifactId, int revision, List<ColumnDef> columns,
              List<ColumnSchema> schemas, int rowCount) {
            this(artifactId, revision, columns, schemas, rowCount,
                    indexByName(columns), indexAliases(columns));
        }

        private static Map<String, ColumnDef> indexByName(List<ColumnDef> columns) {
            Map<String, ColumnDef> byName = new LinkedHashMap<>();
            columns.forEach(c -> byName.put(c.name(), c));
            return byName;
        }

        /** alias → 当前列名（多列声明同一 alias 时按 schema 序首个命中）。 */
        private static Map<String, String> indexAliases(List<ColumnDef> columns) {
            Map<String, String> aliasToName = new LinkedHashMap<>();
            for (ColumnDef c : columns) {
                for (String a : c.aliases()) {
                    aliasToName.putIfAbsent(a, c.name());
                }
            }
            return aliasToName;
        }
    }
}
