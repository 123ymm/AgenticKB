package com.coremasterkb.serving.operator.operators.retrieve;

import com.coremasterkb.serving.domain.ActiveScope;
import com.coremasterkb.serving.mapper.result.UnitV2Row;
import com.coremasterkb.serving.operator.core.ExecContext;
import com.coremasterkb.serving.operator.core.Params;
import com.coremasterkb.serving.operator.core.SlotValues;
import com.coremasterkb.serving.operator.mapper.AssetRetrievalUnitV2Mapper;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * {@code dense_vector} 算子边界（批次8 R2，25 号 §6.5）：textKind/unit_type 映射删除、
 * 维度一致下推、canonical 聚合、无向量数据的 capability diagnostic（空候选+留痕）。
 * 009（1.1.15 性能修复）：embedding 候选预算 / 表示召回预算 / topK 三层分层、
 * 候选不足受控扩召一次、SQL 失败与无命中的显式区分。
 */
@DisplayName("DenseVectorOperator")
class DenseVectorOperatorTest {

    private AssetRetrievalUnitV2Mapper mapper;
    private DenseVectorOperator op;
    private ExecContext ctx;

    @BeforeEach
    void setUp() {
        mapper = mock(AssetRetrievalUnitV2Mapper.class);
        op = new DenseVectorOperator(mapper);
        ctx = new ExecContext("r", "d", "prod", false);
    }

    private static ActiveScope scope() {
        return new ActiveScope("rel", "b1", List.of("snap-1"), Map.of());
    }

    private static UnitV2Row row(String repId, String canonical, double score) {
        UnitV2Row r = new UnitV2Row();
        r.setRepresentationId(repId);
        r.setSnapshotId("snap-1");
        r.setCanonicalEvidenceId(canonical);
        r.setRepresentationType("prose");
        r.setTargetType("segment");
        r.setTargetRef(canonical);
        r.setContentText("内容");
        r.setStructuralContext("");
        r.setChannelScore(score);
        return r;
    }

    private static List<UnitV2Row> rows(int count) {
        return java.util.stream.IntStream.range(0, count)
                .mapToObj(i -> row("rep-" + i, "ev-" + i, 0.9 - i * 0.001))
                .toList();
    }

    private SlotValues inputs(float[] vec) {
        SlotValues in = new SlotValues();
        in.put("queryEmbedding", vec);
        in.put("scope", scope());
        return in;
    }

    private static Params topK(int value) {
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("topK", value);
        return new Params(node);
    }

    /** 宽匹配共享 stub：第一次返回 first，其后返回 second（扩召路径）。 */
    private void stubDenseReturning(List<UnitV2Row> first, List<UnitV2Row> second) {
        when(mapper.searchDenseV2(anyString(), anyInt(), anyList(), anyList(), anyList(),
                anyList(), anyList(), anyBoolean(), anyString(), anyInt(), anyInt()))
                .thenReturn(first, second);
    }

    @Test
    @DisplayName("missing embedding / empty scope → empty, no DB call")
    void guardsShortCircuit() {
        assertThat(op.execute(new SlotValues(), Params.empty(), ctx)
                .getCandidates("candidates")).isEmpty();
        verifyNoInteractions(mapper);
    }

    @Test
    @DisplayName("textKind param and unit_type mapping are gone; dim = query vector length")
    void noTextKindDimensionMatched() {
        stubDenseReturning(List.of(), List.of());

        op.execute(inputs(new float[]{0.1f, 0.2f, 0.3f}), Params.empty(), ctx);

        assertThat(op.definition().paramSchemaJson()).doesNotContain("textKind");
        // 返回行数 < topK 会触发一次受控扩召（第二次调用预算 ×4）——两次入参形态一致
        verify(mapper, times(2)).searchDenseV2(
                argThat(v -> v.startsWith("[") && v.endsWith("]")),
                eq(3), eq(List.of("snap-1")),
                eq(List.of()), eq(List.of()), eq(List.of()), eq(List.of()), anyBoolean(),
                anyString(), anyInt(), anyInt());
    }

    @Test
    @DisplayName("009: 预算三层分层——candidateBudget=min(4000, recall×5) 传给候选层，recall=min(1000, topK×5)")
    void budgetLayering() {
        stubDenseReturning(rows(20), List.of());

        var out = op.execute(inputs(new float[]{0.1f}), Params.empty(), ctx)
                .getCandidates("candidates");

        // topK 默认 20：recall=100，candidateBudget=500；返回 ≥ topK 行 → 不扩召
        verify(mapper, times(1)).searchDenseV2(anyString(), eq(1), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), eq(500), eq(100));
        assertThat(out).hasSize(20);
        assertThat(ctx.attributes()).containsKey("denseSqlMicros");
        assertThat(ctx.attributes()).containsKey("denseRowCount");
        assertThat(ctx.attributes()).doesNotContainKey("denseCandidateEscalated");
    }

    @Test
    @DisplayName("009: topK=200 触顶——recall=1000/candidateBudget=4000，候选不足也不扩召（已在硬顶）")
    void budgetCapsAtHardCapNoEscalation() {
        stubDenseReturning(rows(5), rows(5));

        op.execute(inputs(new float[]{0.1f}), topK(200), ctx);

        verify(mapper, times(1)).searchDenseV2(anyString(), eq(1), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), eq(4000), eq(1000));
        assertThat(ctx.attributes()).doesNotContainKey("denseCandidateEscalated");
    }

    @Test
    @DisplayName("009: 候选不足（去重后 < topK）→ 受控扩召一次（预算 ×4），留痕不无限重试")
    void candidateEscalationOnce() {
        stubDenseReturning(rows(3), rows(20));

        var out = op.execute(inputs(new float[]{0.1f}), Params.empty(), ctx)
                .getCandidates("candidates");

        // 第一次 candidateBudget=500（返回 3 行 < topK=20）→ 第二次 2000，仅此一次
        verify(mapper, times(1)).searchDenseV2(anyString(), eq(1), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), eq(500), eq(100));
        verify(mapper, times(1)).searchDenseV2(anyString(), eq(1), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), eq(2000), eq(100));
        verify(mapper, times(2)).searchDenseV2(anyString(), anyInt(), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), anyInt(), anyInt());
        assertThat(ctx.attributes().get("denseCandidateEscalated")).isEqualTo("500->2000");
        assertThat(out).hasSize(20);
    }

    @Test
    @DisplayName("009: SQL 失败 ≠ 无命中——空 dense 通道 + degraded 留痕，不再静默吞")
    void sqlFailureSurfaced() {
        when(mapper.searchDenseV2(anyString(), anyInt(), anyList(), anyList(), anyList(),
                anyList(), anyList(), anyBoolean(), anyString(), anyInt(), anyInt()))
                .thenThrow(new RuntimeException("query timeout"));

        var out = op.execute(inputs(new float[]{0.1f}), Params.empty(), ctx)
                .getCandidates("candidates");

        assertThat(out).isEmpty();
        assertThat(ctx.attributes().get("denseVectorDegraded")).isEqualTo("dense_query_failed");
        // 失败短路：不做扩召、不做维度诊断（那是"无命中"路径的探查）
        verify(mapper, times(1)).searchDenseV2(anyString(), anyInt(), anyList(), anyList(),
                anyList(), anyList(), anyList(), anyBoolean(), anyString(), anyInt(), anyInt());
        verify(mapper, never()).selectDistinctDimensions(anyList());
    }

    @Test
    @DisplayName("no embeddings in scope → empty candidates + capability degraded trace")
    void noVectorsCapabilityDiagnostic() {
        stubDenseReturning(List.of(), List.of());
        when(mapper.selectDistinctDimensions(anyList())).thenReturn(List.of());

        var out = op.execute(inputs(new float[]{0.1f}), Params.empty(), ctx);

        assertThat(out.getCandidates("candidates")).isEmpty();
        assertThat(ctx.attributes().get("denseVectorDegraded")).isEqualTo("no_embeddings_in_scope");
        verify(mapper).selectDistinctDimensions(List.of("snap-1"));
    }

    @Test
    @DisplayName("dimension mismatch with scope profile → degraded trace (not silent empty)")
    void dimensionMismatchDiagnostic() {
        stubDenseReturning(List.of(), List.of());
        when(mapper.selectDistinctDimensions(anyList())).thenReturn(List.of(1024));

        op.execute(inputs(new float[]{0.1f, 0.2f}), Params.empty(), ctx);

        assertThat(ctx.attributes().get("denseVectorDegraded")).isEqualTo("dimension_mismatch");
    }

    @Test
    @DisplayName("normal no-hit with matching dims → empty result, no degraded trace")
    void normalEmptyNoTrace() {
        stubDenseReturning(List.of(), List.of());
        when(mapper.selectDistinctDimensions(anyList())).thenReturn(List.of(2));

        var out = op.execute(inputs(new float[]{0.1f, 0.2f}), Params.empty(), ctx);

        assertThat(out.getCandidates("candidates")).isEmpty();
        assertThat(ctx.attributes()).doesNotContainKey("denseVectorDegraded");
    }

    @Test
    @DisplayName("rows aggregate by canonical within the channel")
    void canonicalAggregation() {
        // 3 行 < topK=20 → 触发一次扩召；扩召返回同一批（库里就这么多表示）
        List<UnitV2Row> seeded = List.of(
                row("rep-a", "ev-1", 0.95),
                row("rep-b", "ev-1", 0.90),
                row("rep-c", "ev-2", 0.80));
        stubDenseReturning(seeded, seeded);

        var out = op.execute(inputs(new float[]{0.1f}), Params.empty(), ctx)
                .getCandidates("candidates");

        assertThat(out).hasSize(2);
        assertThat(out.get(0).channelId()).isEqualTo("dense");
        assertThat(out.get(0).representationRefs()).containsExactly("rep-a", "rep-b");
        assertThat(out.get(1).channelRank()).isEqualTo(2);
    }
}
