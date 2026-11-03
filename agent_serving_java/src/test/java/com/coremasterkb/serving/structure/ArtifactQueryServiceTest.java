package com.coremasterkb.serving.structure;

import com.coremasterkb.serving.operator.mapper.ArtifactToolMapper;
import com.coremasterkb.serving.operator.mapper.StructureToolMapper.Criterion;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 61号批次3（前半）art_ 制品查询通道契约：
 * <ul>
 *   <li>权限谓词：is_admin 越权面放行（跨域 admin 同放行——对齐 KbAccessService
 *       站点 admin 全通但 domain/status 条件仍生效的口径）；非成员/deprecated/
 *       跨域/不存在 → 同一 artifact_not_found（404 反探测， mapper 单 SQL 折叠）；</li>
 *   <li>三视图形态：artifact_card / artifact_rows（provenance 随行——用例②）/
 *       artifact_aggregate；</li>
 *   <li>DSL 校验复用 StructureQueryDsl 口径：未知列 unknown_field（allowed_fields
 *       带清单）、操作能力、类型收窄；</li>
 *   <li>alias 旧列名 → 当前列名（用例⑨：rollback/改名后兼容读）；</li>
 *   <li>rollback 后 status 仍 published（谓词放行、读 current_revision），
 *       deprecated 被滤除（用例⑧）；</li>
 *   <li>cursor 编解码往返 + 与 order_by 的一致性校验。</li>
 * </ul>
 */
@DisplayName("ArtifactQueryService")
class ArtifactQueryServiceTest {

    private static final String ID = "3f2a1b8c".repeat(4);
    private static final String REF = "art_" + ID;
    private static final String DOMAIN = "odn";

    /** 与 Python publish 落库形状一致：column_name/column_type/ordinal/alias。 */
    private static final String SCHEMA = """
            [
              {"column_id":"c1","column_name":"型号","column_type":"text",
               "enum_values":null,"is_primary_key":true,"is_indexed":false,
               "ordinal":1,"alias":["旧型号"]},
              {"column_id":"c2","column_name":"最大功耗","column_type":"float",
               "enum_values":null,"is_primary_key":false,"is_indexed":false,
               "ordinal":2,"alias":[]},
              {"column_id":"c3","column_name":"投产日期","column_type":"date",
               "enum_values":null,"is_primary_key":false,"is_indexed":false,
               "ordinal":3,"alias":[]}
            ]""";

    private ArtifactToolMapper mapper;
    private ArtifactQueryService service;

    @BeforeEach
    void setUp() {
        mapper = mock(ArtifactToolMapper.class);
        service = new ArtifactQueryService(mapper);
        // 默认：本域已发布、rev3、成员 alice 可见
        when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                .thenReturn(visible(3));
        when(mapper.selectRevision(ID, 3))
                .thenReturn(new ArtifactToolMapper.RevisionRow(SCHEMA, 120));
    }

    private static ArtifactToolMapper.ArtifactVisibleRow visible(int revision) {
        return new ArtifactToolMapper.ArtifactVisibleRow(
                ID, DOMAIN, "table", revision, "站点设备表", "全网设备台账", "按型号过滤查询功耗");
    }

    private static JsonNode query(String json) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readTree(json);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ---- 权限谓词 ------------------------------------------------------------

    @Test
    @DisplayName("is_admin=true 越权面放行（跨域 admin 同放行）——谓词参数 isAdmin 下沉 SQL")
    void adminBypassesMembership() {
        when(mapper.selectVisibleArtifact(ID, "other", "carol", true, null))
                .thenReturn(new ArtifactToolMapper.ArtifactVisibleRow(
                        ID, "other", "table", 3, "T", "D", "U"));
        when(mapper.selectRevision(ID, 3))
                .thenReturn(new ArtifactToolMapper.RevisionRow(SCHEMA, 5));

        Object out = service.query(REF, null, "other", "carol", true);

        assertThat(out).isInstanceOf(ArtifactQueryService.ArtifactCard.class);
        verify(mapper).selectVisibleArtifact(ID, "other", "carol", true, null);
    }

    @Test
    @DisplayName("非成员（mapper 谓词空手）→ artifact_not_found（404 反探测口径）")
    void nonMemberFoldsToArtifactNotFound() {
        assertThatThrownBy(() -> service.query(REF, null, DOMAIN, "mallory", false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");
    }

    @Test
    @DisplayName("deprecated 已停用被谓词滤除（status<>'published' 同折叠）→ artifact_not_found")
    void deprecatedFilteredOut() {
        when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null)).thenReturn(null);

        assertThatThrownBy(() -> service.query(REF, null, DOMAIN, "alice", false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");
    }

    @Test
    @DisplayName("跨域非成员 → artifact_not_found；域以请求参数原样下沉（a.domain_id=#{domain}）")
    void crossDomainFoldsToArtifactNotFound() {
        assertThatThrownBy(() -> service.query(REF, null, "other", "bob", false))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("artifact_not_found");
        verify(mapper).selectVisibleArtifact(ID, "other", "bob", false, null);
    }

    @Test
    @DisplayName("C1 回归：username≠user_id（uuid hex）——登录名原样下发，身份解析只发生在 SQL JOIN")
    void usernameForwardedVerbatimNeverUsedAsUserId() {
        // 登录名恰好形同他人的 user_id（uuid 形态）——旧口径 m.user_id=#{username} /
        // a.owner_user_id=#{username} 直比会被碰撞串误放行；新口径（XML 契约已钉）
        // 先 JOIN kb_users 再对 u.id 判定，service 侧只保证登录名逐字下沉。
        String uuidShaped = "3f2a1b8c-1111-4222-8333-444455556666";
        when(mapper.selectVisibleArtifact(ID, DOMAIN, uuidShaped, false, null))
                .thenReturn(visible(3));

        service.query(REF, null, DOMAIN, uuidShaped, false);

        // 不翻译、不猜测、不拆解——mapper 收到的就是登录名本身
        verify(mapper).selectVisibleArtifact(ID, DOMAIN, uuidShaped, false, null);

        // 发现通道同口径：同一登录名原样进入 selectArtifactSummaries
        when(mapper.selectArtifactSummaries(eq(DOMAIN), eq(uuidShaped), eq(false), eq((String) null),
                any(), any(), anyInt())).thenReturn(List.of());
        service.list(DOMAIN, uuidShaped, false, null, null);
        verify(mapper).selectArtifactSummaries(eq(DOMAIN), eq(uuidShaped), eq(false), eq((String) null),
                any(), any(), anyInt());
    }

    @Test
    @DisplayName("008 钥匙通道：keyId 逐字下沉到两个可见性谓词；web 重载恒 null")
    void keyIdPassesThroughToVisibilityPredicates() {
        // MCP 通道：钥匙 id 原样进入谓词（SQL 侧折叠开放子集判定）
        when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, "k-mcp-1"))
                .thenReturn(visible(3));
        service.query(REF, null, DOMAIN, "alice", false, "k-mcp-1");
        verify(mapper).selectVisibleArtifact(ID, DOMAIN, "alice", false, "k-mcp-1");

        when(mapper.selectArtifactSummaries(eq(DOMAIN), eq("alice"), eq(false),
                eq("k-mcp-1"), any(), any(), anyInt())).thenReturn(List.of());
        service.list(DOMAIN, "alice", false, "k-mcp-1", null, null);
        verify(mapper).selectArtifactSummaries(eq(DOMAIN), eq("alice"), eq(false),
                eq("k-mcp-1"), any(), any(), anyInt());
    }

    // ---- 三视图形态 ------------------------------------------------------------

    @Test
    @DisplayName("无 query → artifact_card：元信息 + revision + row_count + 列能力")
    void cardViewShape() {
        ArtifactQueryService.ArtifactCard card =
                (ArtifactQueryService.ArtifactCard) service.query(REF, null, DOMAIN, "alice", false);

        assertThat(card.artifactRef()).isEqualTo(REF);
        assertThat(card.title()).isEqualTo("站点设备表");
        assertThat(card.description()).isEqualTo("全网设备台账");
        assertThat(card.usageNote()).isEqualTo("按型号过滤查询功耗");
        assertThat(card.revision()).isEqualTo(3);
        assertThat(card.rowCount()).isEqualTo(120);
        assertThat(card.kind()).isEqualTo("table");
        assertThat(card.columns()).extracting("name").containsExactly("型号", "最大功耗", "投产日期");

        ArtifactQueryService.ColumnSchema power = card.columns().get(1);
        assertThat(power.type()).isEqualTo("float");
        assertThat(power.sortable()).isTrue();
        assertThat(power.canAggregate()).isTrue();
        assertThat(power.operations()).contains("lte", "sum", "avg");

        ArtifactQueryService.ColumnSchema model = card.columns().get(0);
        assertThat(model.canAggregate()).isFalse();
        assertThat(model.operations()).contains("eq", "contains", "count")
                .doesNotContain("lt", "sum");

        ArtifactQueryService.ColumnSchema date = card.columns().get(2);
        assertThat(date.canAggregate()).isTrue();
        assertThat(date.operations()).contains("gte", "min", "max")
                .doesNotContain("sum", "avg", "contains");
    }

    @Test
    @DisplayName("行视图：select 投影 + row_key + provenance 随行（用例②通过标准）")
    void rowsViewWithProvenance() {
        when(mapper.selectArtifactRows(eq(ID), eq(3), anyList(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt()))
                .thenReturn(List.of(new ArtifactToolMapper.ArtifactRowValue(
                        "OLT-1", "{\"型号\":\"OLT-1\",\"最大功耗\":65,\"投产日期\":\"2026-01-05\"}",
                        "[{\"source\":\"kb-1/doc-9\",\"row\":12}]", 0, null)));

        ArtifactQueryService.ArtifactRows out = (ArtifactQueryService.ArtifactRows) service.query(
                REF, query("{\"select\":[\"型号\",\"最大功耗\"]}"), DOMAIN, "alice", false);

        assertThat(out.artifactRef()).isEqualTo(REF);
        assertThat(out.columns()).hasSize(3);
        assertThat(out.rows()).hasSize(1);
        ArtifactQueryService.ArtifactRowView row = out.rows().get(0);
        assertThat(row.rowKey()).isEqualTo("OLT-1");
        assertThat(row.data()).containsOnlyKeys("型号", "最大功耗");
        assertThat(row.data()).containsEntry("型号", "OLT-1");
        assertThat(row.provenance()).isNotNull();
        assertThat(row.provenance()).hasSize(1);
        assertThat(out.hasMore()).isFalse();
        assertThat(out.cursor()).isNull();
    }

    @Test
    @DisplayName("聚合视图：avg 数值列下推 + row_count 附带；count 无字段")
    void aggregateView() {
        when(mapper.aggregateArtifactRows(eq(ID), eq(3), anyList(), eq("avg"),
                eq("最大功耗"), eq(false)))
                .thenReturn(new ArtifactToolMapper.AggregateValueRow(2L, 82.75));

        ArtifactQueryService.ArtifactAggregate out = (ArtifactQueryService.ArtifactAggregate)
                service.query(REF, query("{\"aggregate\":{\"op\":\"avg\",\"field\":\"最大功耗\"}}"),
                        DOMAIN, "alice", false);

        assertThat(out.artifactRef()).isEqualTo(REF);
        assertThat(out.aggregate().op()).isEqualTo("avg");
        assertThat(out.aggregate().field()).isEqualTo("最大功耗");
        assertThat(out.aggregate().value()).isEqualTo(82.75);
        assertThat(out.aggregate().rowCount()).isEqualTo(2L);
    }

    // ---- DSL 校验（复用 StructureQueryDsl / st_ 口径） --------------------------

    @Test
    @DisplayName("未知列 → unknown_field（allowed_fields 带清单），SQL 零调用")
    void unknownFieldRejected() {
        assertThatThrownBy(() -> service.query(REF,
                query("{\"where\":[{\"field\":\"型号'; DROP TABLE x;--\",\"op\":\"eq\",\"value\":\"1\"}]}"),
                DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .satisfies(e -> {
                    StructureToolException ste = (StructureToolException) e;
                    assertThat(ste.code()).isEqualTo("unknown_field");
                    assertThat(ste.details()).containsKey("allowed_fields");
                    assertThat((List<?>) ste.details().get("allowed_fields"))
                            .extracting(Object::toString)
                            .containsExactly("型号", "最大功耗", "投产日期");
                });
        verify(mapper, never()).selectArtifactRows(any(), anyInt(), anyList(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt());
    }

    @Test
    @DisplayName("alias 旧列名（用例⑨）→ 展开映射到当前列名后才进绑定参数")
    void aliasMapsToCurrentName() {
        when(mapper.selectArtifactRows(eq(ID), eq(3), anyList(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt()))
                .thenReturn(List.of());

        service.query(REF,
                query("{\"where\":[{\"field\":\"旧型号\",\"op\":\"eq\",\"value\":\"OLT-1\"}]}"),
                DOMAIN, "alice", false);

        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<Criterion>> cap = ArgumentCaptor.forClass(List.class);
        verify(mapper).selectArtifactRows(eq(ID), eq(3), cap.capture(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt());
        assertThat(cap.getValue()).hasSize(1);
        assertThat(cap.getValue().get(0).getField()).isEqualTo("型号");
        assertThat(cap.getValue().get(0).getValue()).isEqualTo("OLT-1");
    }

    @Test
    @DisplayName("类型与能力：数值列字符串数字容错（去千分位）、文本列 lt 拒绝、date 值须 ISO")
    void typeAndCapabilityRules() {
        when(mapper.selectArtifactRows(eq(ID), eq(3), anyList(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt()))
                .thenReturn(List.of());

        // 字符串数字（含千分位）→ 容错为数值绑定
        service.query(REF,
                query("{\"where\":[{\"field\":\"最大功耗\",\"op\":\"gt\",\"value\":\"1,200\"}]}"),
                DOMAIN, "alice", false);
        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<Criterion>> cap = ArgumentCaptor.forClass(List.class);
        verify(mapper).selectArtifactRows(eq(ID), eq(3), cap.capture(), any(), any(),
                anyBoolean(), anyInt(), any(), any(), anyInt());
        assertThat(cap.getValue().get(0).getValue()).isEqualTo(1200.0);
        assertThat(cap.getValue().get(0).isNumeric()).isTrue();

        // 文本列 lt → unsupported_operation
        assertThatThrownBy(() -> service.query(REF,
                query("{\"where\":[{\"field\":\"型号\",\"op\":\"lt\",\"value\":\"A\"}]}"),
                DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("unsupported_operation");

        // date 非 ISO → type_mismatch
        assertThatThrownBy(() -> service.query(REF,
                query("{\"where\":[{\"field\":\"投产日期\",\"op\":\"gte\",\"value\":\"2026/6/1\"}]}"),
                DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("type_mismatch");
    }

    @Test
    @DisplayName("文本列 sum → unsupported_operation（仅 count）；date 列 min 走文本序")
    void aggregateCapability() {
        assertThatThrownBy(() -> service.query(REF,
                query("{\"aggregate\":{\"op\":\"sum\",\"field\":\"型号\"}}"),
                DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("unsupported_operation");
        verify(mapper, never()).aggregateArtifactRows(any(), anyInt(), anyList(), any(),
                any(), anyBoolean());

        when(mapper.aggregateArtifactRows(eq(ID), eq(3), anyList(), eq("min"),
                eq("投产日期"), eq(true)))
                .thenReturn(new ArtifactToolMapper.AggregateValueRow(120L, "2020-01-01"));
        ArtifactQueryService.ArtifactAggregate out = (ArtifactQueryService.ArtifactAggregate)
                service.query(REF, query("{\"aggregate\":{\"op\":\"min\",\"field\":\"投产日期\"}}"),
                        DOMAIN, "alice", false);
        assertThat(out.aggregate().value()).isEqualTo("2020-01-01");
    }

    @Test
    @DisplayName("ref 前缀非法 → invalid_ref；空 ref → invalid_ref")
    void refPrefixEnforced() {
        assertThatThrownBy(() -> service.query("st_abcdef", null, DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("invalid_ref");
        assertThatThrownBy(() -> service.query("art_", null, DOMAIN, "alice", false))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("invalid_ref");
    }

    // ---- rollback / 生命周期（用例⑧） -------------------------------------------

    @Test
    @DisplayName("rollback 后 status 仍 published：谓词放行、读拨回的 current_revision")
    void rollbackKeepsPublishedAccess() {
        when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                .thenReturn(visible(1));
        when(mapper.selectRevision(ID, 1))
                .thenReturn(new ArtifactToolMapper.RevisionRow(SCHEMA, 80));

        ArtifactQueryService.ArtifactCard card =
                (ArtifactQueryService.ArtifactCard) service.query(REF, null, DOMAIN, "alice", false);

        assertThat(card.revision()).isEqualTo(1);
        assertThat(card.rowCount()).isEqualTo(80);
        verify(mapper).selectRevision(ID, 1);
    }

    // ---- cursor -------------------------------------------------------------------

    @Nested
    @DisplayName("cursor 编解码")
    class CursorCodec {

        @Test
        @DisplayName("行 keyset cursor 往返（ordinal/rowKey/sortValue/ordered）")
        void rowCursorRoundTrip() {
            String c = ArtifactQueryService.encodeRowCursor(7, "OLT-9", "100.5", true);
            ArtifactQueryService.RowCursor dec = ArtifactQueryService.decodeRowCursor(c);
            assertThat(dec.ordinal()).isEqualTo(7);
            assertThat(dec.rowKey()).isEqualTo("OLT-9");
            assertThat(dec.sortValue()).isEqualTo("100.5");
            assertThat(dec.ordered()).isTrue();

            String nullSort = ArtifactQueryService.encodeRowCursor(0, "k", null, true);
            assertThat(ArtifactQueryService.decodeRowCursor(nullSort).sortValue()).isNull();
            assertThat(ArtifactQueryService.decodeRowCursor(nullSort).ordered()).isTrue();

            assertThat(ArtifactQueryService.decodeRowCursor(null)).isNull();
            assertThat(ArtifactQueryService.decodeRowCursor("")).isNull();
        }

        @Test
        @DisplayName("列表 keyset cursor 往返（updated_at + artifact_id）")
        void listCursorRoundTrip() {
            OffsetDateTime ts = OffsetDateTime.parse("2026-10-17T10:15:30.123456+08:00");
            String c = ArtifactQueryService.encodeListCursor(ts, "zzz");
            ArtifactQueryService.ListCursor dec = ArtifactQueryService.decodeListCursor(c);
            assertThat(dec.updatedAt()).isEqualTo(ts);
            assertThat(dec.artifactId()).isEqualTo("zzz");
            assertThat(ArtifactQueryService.decodeListCursor(null)).isNull();
        }

        @Test
        @DisplayName("非法 cursor（非 base64 / 非 JSON / 缺键）→ typed 拒绝，不静默从头")
        void garbageCursorRejected() {
            assertThatThrownBy(() -> ArtifactQueryService.decodeRowCursor("!!!not-base64!!!"))
                    .isInstanceOf(StructureToolException.class);
            assertThatThrownBy(() -> ArtifactQueryService.decodeRowCursor("aGVsbG8"))
                    .isInstanceOf(StructureToolException.class);
            assertThatThrownBy(() -> ArtifactQueryService.decodeListCursor("aGVsbG8"))
                    .isInstanceOf(StructureToolException.class);
        }

        @Test
        @DisplayName("cursor 与 order_by 不一致 → typed 拒绝（防错页跳页）")
        void cursorOrderMismatchRejected() {
            String unordered = ArtifactQueryService.encodeRowCursor(7, "OLT-9", null, false);
            assertThatThrownBy(() -> service.query(REF,
                    query("{\"order_by\":[{\"field\":\"最大功耗\",\"direction\":\"desc\"}],\"cursor\":\""
                            + unordered + "\"}"),
                    DOMAIN, "alice", false))
                    .isInstanceOf(StructureToolException.class)
                    .extracting(e -> ((StructureToolException) e).code())
                    .isEqualTo("unsupported_operation");

            String ordered = ArtifactQueryService.encodeRowCursor(7, "OLT-9", "100.5", true);
            assertThatThrownBy(() -> service.query(REF,
                    query("{\"cursor\":\"" + ordered + "\"}"),
                    DOMAIN, "alice", false))
                    .isInstanceOf(StructureToolException.class)
                    .extracting(e -> ((StructureToolException) e).code())
                    .isEqualTo("unsupported_operation");
        }

        @Test
        @DisplayName("has_more 时 cursor 取本页最后一行（limit+1 探测）")
        void cursorAdvancesByLastReturnedRow() {
            when(mapper.selectArtifactRows(eq(ID), eq(3), anyList(), any(), any(),
                    anyBoolean(), anyInt(), any(), any(), eq(2)))
                    .thenReturn(List.of(
                            new ArtifactToolMapper.ArtifactRowValue(
                                    "OLT-1", "{\"型号\":\"OLT-1\"}", "[]", 0, null),
                            new ArtifactToolMapper.ArtifactRowValue(
                                    "OLT-2", "{\"型号\":\"OLT-2\"}", "[]", 1, null)));

            ArtifactQueryService.ArtifactRows out = (ArtifactQueryService.ArtifactRows) service.query(
                    REF, query("{\"limit\":1}"), DOMAIN, "alice", false);

            assertThat(out.hasMore()).isTrue();
            assertThat(out.rows()).hasSize(1);
            assertThat(out.cursor()).isNotBlank();
            ArtifactQueryService.RowCursor dec = ArtifactQueryService.decodeRowCursor(out.cursor());
            assertThat(dec.ordinal()).isEqualTo(0); // 最后「返回」行（第 1 行），非探测行
            assertThat(dec.rowKey()).isEqualTo("OLT-1");
            assertThat(dec.ordered()).isFalse();
        }

        @Test
        @DisplayName("C4：伪造 cursor 的 sort_value 不可 cast numeric → typed 400，零 SQL 下沉")
        void forgedNumericCursorSortValueRejected() {
            // 非法文法 / 千分位 / 超值域指数——合法 cursor 恒为 BigDecimal 平文，全是伪造形态
            for (String forged : List.of("1,2abc", "1,200", "1e999999", "")) {
                String cursor = ArtifactQueryService.encodeRowCursor(0, "k", forged, true);
                assertThatThrownBy(() -> service.query(REF,
                        query("{\"order_by\":[{\"field\":\"最大功耗\",\"direction\":\"desc\"}],"
                                + "\"cursor\":\"" + cursor + "\"}"),
                        DOMAIN, "alice", false))
                        .as("sort_value=%s 须 typed 拒绝", forged)
                        .isInstanceOf(StructureToolException.class)
                        .extracting(e -> ((StructureToolException) e).code())
                        .isEqualTo("unsupported_operation");
            }
            verify(mapper, never()).selectArtifactRows(any(), anyInt(), anyList(), any(), any(),
                    anyBoolean(), anyInt(), any(), any(), anyInt());

            // 对照：合法数字串（本服务签发形态）原样通过，keyset 参数逐值下沉
            String valid = ArtifactQueryService.encodeRowCursor(3, "OLT-3", "65.5", true);
            when(mapper.selectArtifactRows(eq(ID), eq(3), anyList(), eq("最大功耗"),
                    eq("desc"), eq(true), eq(3), eq("OLT-3"), eq("65.5"), anyInt()))
                    .thenReturn(List.of());
            service.query(REF,
                    query("{\"order_by\":[{\"field\":\"最大功耗\",\"direction\":\"desc\"}],"
                            + "\"cursor\":\"" + valid + "\"}"),
                    DOMAIN, "alice", false);
            verify(mapper).selectArtifactRows(eq(ID), eq(3), anyList(), eq("最大功耗"),
                    eq("desc"), eq(true), eq(3), eq("OLT-3"), eq("65.5"), anyInt());
        }
    }

    // ---- 发现通道 ------------------------------------------------------------

    @Test
    @DisplayName("artifact-list：域内已发布可见制品 + keyset 翻页 + art_ 前缀")
    void listDiscoveryWithPagination() {
        OffsetDateTime ts1 = OffsetDateTime.parse("2026-10-17T10:00:00+08:00");
        OffsetDateTime ts2 = OffsetDateTime.parse("2026-10-16T09:00:00+08:00");
        when(mapper.selectArtifactSummaries(DOMAIN, "alice", false, null, null, null, 51))
                .thenReturn(List.of(
                        new ArtifactToolMapper.ArtifactSummaryRow(
                                "aaa", "T1", "d1", "u1", 2, "table", ts1, 10),
                        new ArtifactToolMapper.ArtifactSummaryRow(
                                "bbb", "T2", "d2", "u2", 0, "wiki", ts2, null)));

        ArtifactQueryService.ArtifactListResult out =
                service.list(DOMAIN, "alice", false, null, null);

        assertThat(out.artifacts()).hasSize(2);
        assertThat(out.artifacts().get(0).artifactRef()).isEqualTo("art_aaa");
        assertThat(out.artifacts().get(0).rowCount()).isEqualTo(10);
        assertThat(out.artifacts().get(1).rowCount()).isZero(); // 无 revision 快照 → 0
        assertThat(out.artifacts().get(1).kind()).isEqualTo("wiki");
        assertThat(out.hasMore()).isFalse();
        assertThat(out.cursor()).isNull();
    }

    @Test
    @DisplayName("artifact-list：limit+1 探测 has_more，cursor 取最后返回行的 (updated_at,id)")
    void listDiscoveryCursorFromLastRow() {
        OffsetDateTime ts1 = OffsetDateTime.parse("2026-10-17T10:00:00+08:00");
        when(mapper.selectArtifactSummaries(DOMAIN, "alice", false, null, null, null, 2))
                .thenReturn(List.of(
                        new ArtifactToolMapper.ArtifactSummaryRow(
                                "aaa", "T1", "d1", "u1", 2, "table", ts1, 10),
                        new ArtifactToolMapper.ArtifactSummaryRow(
                                "bbb", "T2", "d2", "u2", 1, "table",
                                ts1.minusMinutes(1), 5)));

        ArtifactQueryService.ArtifactListResult out =
                service.list(DOMAIN, "alice", false, null, 1);

        assertThat(out.artifacts()).hasSize(1);
        assertThat(out.hasMore()).isTrue();
        ArtifactQueryService.ListCursor dec = ArtifactQueryService.decodeListCursor(out.cursor());
        assertThat(dec.artifactId()).isEqualTo("aaa");
        assertThat(dec.updatedAt()).isEqualTo(ts1);

        // 下一页以解码出的 keyset 参数下沉
        service.list(DOMAIN, "alice", false, out.cursor(), 1);
        verify(mapper).selectArtifactSummaries(DOMAIN, "alice", false, null, dec.updatedAt(),
                dec.artifactId(), 2);
    }

    @Test
    @DisplayName("artifact-list：limit 校验同 st_ 口径（<=0 / >200 → result_too_large）")
    void listLimitGuard() {
        assertThatThrownBy(() -> service.list(DOMAIN, "alice", false, null, 0))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("result_too_large");
        assertThatThrownBy(() -> service.list(DOMAIN, "alice", false, null, 10000))
                .isInstanceOf(StructureToolException.class)
                .extracting(e -> ((StructureToolException) e).code())
                .isEqualTo("result_too_large");
    }

    // ---- wiki 分流（63号批次4——query dict 协议路由；表格路径回归由上方既有用例守护） ----

    @Nested
    @DisplayName("wiki 分流")
    class WikiDispatch {

        private ArtifactWikiSearchService wikiSvc;

        @BeforeEach
        void setUpWiki() {
            wikiSvc = mock(ArtifactWikiSearchService.class);
            service = new ArtifactQueryService(mapper, wikiSvc);
            // 本域已发布 wiki、rev2、成员 alice 可见（table 用例的 kind="table" 桩被覆写）
            when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                    .thenReturn(new ArtifactToolMapper.ArtifactVisibleRow(
                            ID, DOMAIN, "wiki", 2, "运维知识 wiki", null, null));
        }

        @Test
        @DisplayName("query 缺省 → wiki 卡片（selectRevision 零调用——wiki 自己收口）")
        void nullQueryRoutesToCard() {
            ArtifactWikiSearchService.WikiArtifactCard card =
                    new ArtifactWikiSearchService.WikiArtifactCard(
                            "wiki", REF, "运维知识 wiki", "published", 2, 12,
                            new ArtifactWikiSearchService.WikiEmbeddingProgress(30, 28, 1, 1));
            when(wikiSvc.card(REF, DOMAIN, "alice", false, null)).thenReturn(card);

            Object out = service.query(REF, null, DOMAIN, "alice", false);

            assertThat(out).isSameAs(card);
            // wiki 分流在 selectRevision 之前——快照读取由 wiki 服务自理
            verify(mapper, never()).selectRevision(any(), anyInt());
        }

        @Test
        @DisplayName("query={\"q\",mode,top_k} → search 参数透传（mode/top_k 缺省 null）")
        void qKeyRoutesToSearch() {
            when(wikiSvc.search(eq(REF), eq("接口报错"), isNull(), isNull(),
                    eq(DOMAIN), eq("alice"), eq(false), isNull()))
                    .thenReturn(new ArtifactWikiSearchService.WikiSearchView(
                            REF, 2, false, List.of()));

            Object out = service.query(REF, query("{\"q\":\"接口报错\"}"),
                    DOMAIN, "alice", false);

            assertThat(out).isInstanceOf(ArtifactWikiSearchService.WikiSearchView.class);
            verify(wikiSvc).search(REF, "接口报错", null, null, DOMAIN, "alice", false, null);

            // mode/top_k 逐参透传
            service.query(REF, query("{\"q\":\"x\",\"mode\":\"dense\",\"top_k\":7}"),
                    DOMAIN, "alice", false);
            verify(wikiSvc).search(REF, "x", "dense", 7, DOMAIN, "alice", false, null);
        }

        @Test
        @DisplayName("query={\"page\"} → readPage；query={\"links\",direction} → links")
        void pageAndLinksKeysRoute() {
            ArtifactWikiSearchService.WikiPageView page =
                    new ArtifactWikiSearchService.WikiPageView(REF, 2, null, null);
            when(wikiSvc.readPage(REF, "home", DOMAIN, "alice", false, null)).thenReturn(page);
            assertThat(service.query(REF, query("{\"page\":\"home\"}"), DOMAIN, "alice", false))
                    .isSameAs(page);

            ArtifactWikiSearchService.WikiLinksView links =
                    new ArtifactWikiSearchService.WikiLinksView(REF, 2, "home", "in", List.of());
            when(wikiSvc.links(REF, "home", "in", DOMAIN, "alice", false, null)).thenReturn(links);
            assertThat(service.query(REF, query("{\"links\":\"home\",\"direction\":\"in\"}"),
                    DOMAIN, "alice", false)).isSameAs(links);
            // direction 缺省 null（服务内归一为 out）
            service.query(REF, query("{\"links\":\"home\"}"), DOMAIN, "alice", false);
            verify(wikiSvc).links(REF, "home", null, DOMAIN, "alice", false, null);
        }

        @Test
        @DisplayName("未知键（含表格 DSL 键）→ unsupported_operation（中文说明 wiki 协议 + allowed）")
        void unknownKeysRejectedWithProtocolHint() {
            assertThatThrownBy(() -> service.query(REF,
                    query("{\"where\":[{\"field\":\"型号\",\"op\":\"eq\",\"value\":\"1\"}]}"),
                    DOMAIN, "alice", false))
                    .isInstanceOf(StructureToolException.class)
                    .hasMessageContaining("wiki 制品 query")
                    .satisfies(e -> {
                        StructureToolException ste = (StructureToolException) e;
                        assertThat(ste.code()).isEqualTo("unsupported_operation");
                        assertThat(ste.details()).containsKey("allowed");
                        assertThat((List<?>) ste.details().get("allowed"))
                                .extracting(Object::toString)
                                .contains("q", "page", "links", "direction");
                    });
            assertThatThrownBy(() -> service.query(REF, query("{\"foo\":\"bar\"}"),
                    DOMAIN, "alice", false))
                    .isInstanceOf(StructureToolException.class)
                    .hasMessageContaining("foo");
        }

        @Test
        @DisplayName("表格回归：kind=table 时 wiki 分支不可达——DSL 照常（聚合正常返回）")
        void tableKindStillUsesDsl() {
            when(mapper.selectVisibleArtifact(ID, DOMAIN, "alice", false, null))
                    .thenReturn(visible(3));
            when(mapper.aggregateArtifactRows(eq(ID), eq(3), anyList(), eq("count"),
                    isNull(), eq(false)))
                    .thenReturn(new ArtifactToolMapper.AggregateValueRow(120L, 120L));

            Object out = service.query(REF, query("{\"aggregate\":{\"op\":\"count\"}}"),
                    DOMAIN, "alice", false);

            assertThat(out).isInstanceOf(ArtifactQueryService.ArtifactAggregate.class);
            verify(wikiSvc, org.mockito.Mockito.never())
                    .search(any(), any(), any(), any(), any(), any(), anyBoolean(), any());
        }
    }

    // ---- mapper XML 文本契约（对齐 StructureToolMapperXmlTest 口径） ------------------

    @Nested
    @DisplayName("ArtifactToolMapper SQL contract")
    class XmlContract {

        private String mapperXml() throws Exception {
            try (InputStream in = getClass().getClassLoader()
                    .getResourceAsStream("mapper/ArtifactToolMapper.xml")) {
                assertThat(in).as("mapper XML must be on classpath").isNotNull();
                // 归一换行：Windows 检出（core.autocrlf）资源文件是 CRLF，而多行
                // contains 断言用 \n 字面量——不归一则在 Windows 检出下必红。
                return new String(in.readAllBytes(), StandardCharsets.UTF_8)
                        .replace("\r\n", "\n");
            }
        }

        @Test
        @DisplayName("XML 良构（可被解析）且无 ${} 字符串插值——注入面收敛在 #{} 绑定")
        void wellFormedAndNoInterpolation() throws Exception {
            String xml = mapperXml();
            assertThat(xml).doesNotContain("${");
            javax.xml.parsers.DocumentBuilderFactory.newInstance()
                    .newDocumentBuilder().parse(new java.io.ByteArrayInputStream(
                            xml.getBytes(StandardCharsets.UTF_8)));
        }

        @Test
        @DisplayName("字段名只作绑定参数（data->>#{...}），权限谓词含 status/domain/admin 三件")
        void fieldNamesBoundAndPredicateFolded() throws Exception {
            String xml = mapperXml();
            assertThat(xml).contains("t.data ->> #{c.field}");
            assertThat(xml).contains("r.data ->> #{orderField}");
            assertThat(xml).contains("t.data ->> #{aggField}");
            assertThat(xml).doesNotContain("->> '");
            assertThat(xml).doesNotContain("->> \"");
            assertThat(xml).contains("a.status = 'published'");
            assertThat(xml).contains("a.domain_id = #{domain}");
            assertThat(xml).contains("#{isAdmin}");
            assertThat(xml).contains("m.role IS NOT NULL");
            // C1：username 先解析成 kb_users.id（active+未清退），成员/owner 全对 u.id
            // 判定——不再拿登录名直比 user_id 列（uuid hex；防恒空手与 username 碰撞提权）
            assertThat(xml).contains("u.username = #{username}");
            assertThat(xml).contains("u.status = 'active'");
            assertThat(xml).contains("u.deleted_at IS NULL");
            assertThat(xml).contains("m.user_id = u.id");
            assertThat(xml).contains("a.owner_user_id = u.id");
            assertThat(xml).doesNotContain("m.user_id = #{username}");
            assertThat(xml).doesNotContain("a.owner_user_id = #{username}");
        }

        @Test
        @DisplayName("操作符/方向/聚合是 <choose> 固定分支；数值比较去千分位后 ::numeric")
        void fixedBranchesAndNumericCoercion() throws Exception {
            String xml = mapperXml();
            assertThat(xml).contains("test=\"c.op == 'eq'\"");
            assertThat(xml).contains("test=\"c.op == 'contains'");
            assertThat(xml).contains("test=\"orderDir == 'desc'");
            assertThat(xml).contains("test=\"aggOp == 'count'");
            assertThat(xml).doesNotContain("test=\"aggOp == 'avg'\""); // otherwise 兜底
            assertThat(xml).contains(
                    "NULLIF(REPLACE(t.data ->> #{aggField}, ',', ''), '')::numeric");
            // keyset：行翻页 (ordinal,row_key) 组合；发现翻页 updated_at+id
            assertThat(xml).contains("(t.ordinal, t.row_key) &gt; (#{afterOrdinal}, #{afterRowKey})");
            assertThat(xml).contains("a.updated_at &lt; CAST(#{afterUpdatedAt} AS timestamptz)");
            assertThat(xml).contains("LIMIT #{limit}");
        }

        @Test
        @DisplayName("C2：聚合查询包同形子查询（别名 t）——aggField/criterion 引用统一走 t.data")
        void aggregateWrappedInSubqueryAliasedT() throws Exception {
            String xml = mapperXml();
            // 行查询 + 聚合子查询两处直查 kb_table_rows r；聚合外层只见别名 t
            assertThat(xml.split("FROM kb_table_rows r", -1)).hasSize(3);
            assertThat(xml).contains("t.data ->> #{aggField}");
            // 聚合直查 r 时 criterion 片段的 t.data 引用不存在的别名 → 42P01（修复前形态）
            assertThat(xml).doesNotContain("r.data ->> #{aggField}");
            // artifact/revision 收窄移入子查询（聚合过滤命中面不变）
            assertThat(xml).contains(
                    "SELECT r.data\n            FROM kb_table_rows r\n"
                            + "            WHERE r.artifact_id = #{artifactId}"
                            + " AND r.revision = #{revision}\n        ) t");
        }

        @Test
        @DisplayName("C3：DESC null 游标分支放行非 NULL 块（NULLS FIRST 头块耗尽不丢页）")
        void descNullCursorBranchExitsToNonNullBlock() throws Exception {
            String xml = mapperXml();
            // DESC 分支出路：块内 (ordinal,row_key) 推进 OR 放行非 NULL 块
            assertThat(xml).contains("t.sort_value IS NOT NULL");
            // ASC 分支保持原语义：NULL 块即终点（NULLS LAST）
            assertThat(xml).contains("AND t.sort_value IS NULL");
        }
    }
}
