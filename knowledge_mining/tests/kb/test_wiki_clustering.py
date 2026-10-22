"""Wiki 编译聚类器纯函数测试 —— 无 PG，本地红绿（63号批次3 Task 2）。

合成 4 维向量测逻辑：入簇/新种子/阈值边界（恰等于阈值）/质心=归一化均值/
<min_promote 解散/未聚类目录兜底（含空目录总杂项簇）/>max_cluster 递归拆/
>merge_threshold 合并（含迭代至稳定）/确定性（同输入两次 ClusterResult
相等 + cluster_id 成员集哈希稳定）/build_cluster_context 截断与标头/
top_terms 计数与平局/无 embedding 全旁路。

float32 注意：embedding 走 array('f')（生产口径），分量经 f32 舍入——
精确边界用 dot 恰为 0.0/1.0 的正交/重复向量，其余用例留足浮点余量。
"""
from __future__ import annotations

import hashlib
import math
from array import array

import pytest

from knowledge_mining.mining.kb.wiki_clustering import (
    DIGEST_MEMBERS_CAP,
    Cluster,
    build_cluster_context,
    cluster_units,
    top_terms,
)


def unit(*comps: float) -> tuple[float, ...]:
    """合成归一化向量（测试侧与实现同式独立计算——质心断言口径）。"""
    n = math.sqrt(sum(x * x for x in comps))
    return tuple(x / n for x in comps)


def it(rid: str, vec: tuple[float, ...] | None = None, *,
       content: str = "", directory: str = "d0") -> dict:
    """聚类输入行（embedding=array('f')|None，对齐 collect_units 产出。"""
    return {
        "representation_id": rid,
        "content_text": content or f"{rid}内容文本",
        "directory_path": directory,
        "embedding": array("f", vec) if vec is not None else None,
    }


def cid(*rids: str) -> str:
    """cluster_id 期望值：成员排序后 sha256[:12]（与实现同式独立计算）。"""
    return hashlib.sha256("\n".join(sorted(rids)).encode("utf-8")).hexdigest()[:12]


def by_members(result) -> dict[frozenset, Cluster]:
    return {frozenset(c.member_ids): c for c in result.clusters}


# ------------------------------------------------------- 入簇 / 新种子 / 边界

def test_assign_to_seed_vs_new_seed() -> None:
    # v3 与种子相似 0.6 < 0.62 → 新种子；v4 正交 → 新种子；重复向量入簇
    items = [
        it("r1", (1, 0, 0, 0)),
        it("r2", (1, 0, 0, 0)),
        it("r3", unit(3, 4, 0, 0)),   # cos(r1,r3)=0.6 < 0.62
        it("r4", (0, 0, 1, 0)),
    ]
    result = cluster_units(items, min_promote=1)
    groups = by_members(result)
    assert frozenset({"r1", "r2"}) in groups
    assert frozenset({"r3"}) in groups
    assert frozenset({"r4"}) in groups
    assert len(result.clusters) == 3
    assert result.unclustered == ()
    assert result.stats["cluster_count"] == 3


def test_threshold_boundary_exact_and_near() -> None:
    # 恰等于阈值：重复向量点积恰为 1.0（f32 精确），阈值=1.0 → ≥ 成立入簇
    dup = cluster_units(
        [it("a", (1, 0, 0, 0)), it("b", (1, 0, 0, 0))],
        sim_threshold=1.0, min_promote=1)
    assert [tuple(c.member_ids) for c in dup.clusters] == [("a", "b")]

    # 近边界两侧（0.6 相似度 ± 浮点余量）：0.5999 入簇 / 0.6001 新种子
    near = cluster_units(
        [it("a", (1, 0, 0, 0)), it("b", unit(3, 4, 0, 0))],
        sim_threshold=0.5999, min_promote=1)
    assert [tuple(c.member_ids) for c in near.clusters] == [("a", "b")]
    apart = cluster_units(
        [it("a", (1, 0, 0, 0)), it("b", unit(3, 4, 0, 0))],
        sim_threshold=0.6001, min_promote=1)
    assert sorted(tuple(c.member_ids) for c in apart.clusters) == [("a",), ("b",)]


def test_centroid_is_normalized_mean_of_members() -> None:
    va, vb = (1, 0, 0, 0), unit(1, 1, 0, 0)  # cos≈0.707 ≥ 0.62 同簇
    result = cluster_units(
        [it("a", va), it("b", vb)], min_promote=2)
    cluster = by_members(result)[frozenset({"a", "b"})]
    # 质心 = 成员归一化向量的均值再归一化（测试侧独立算）
    s = tuple(x + y for x, y in zip(va, vb))
    n = math.sqrt(sum(x * x for x in s))
    expected = tuple(x / n for x in s)
    assert len(cluster.centroid) == 4
    for got, exp in zip(cluster.centroid, expected):
        assert got == pytest.approx(exp)
    # 质心恒单位向量
    assert math.sqrt(sum(x * x for x in cluster.centroid)) == pytest.approx(1.0)


# ------------------------------------------------- 解散 / 未聚类目录兜底

def test_dissolve_below_min_promote_into_unclustered() -> None:
    # 2 成员簇 < min_promote=3 → 解散进未聚类池；再按目录兜底成杂项簇
    result = cluster_units([
        it("a", (1, 0, 0, 0), directory="d1"),
        it("b", (1, 0, 0, 0), directory="d1"),
    ])
    assert result.stats["dissolved_count"] == 1
    assert result.unclustered == ("a", "b")
    assert result.stats["cluster_count"] == 0
    (misc,) = result.clusters
    assert misc.is_misc and tuple(misc.member_ids) == ("a", "b")
    assert misc.centroid == ()


def test_misc_fallback_groups_by_directory() -> None:
    # 无向量成员按 directory_path 分组兜底；空目录成员同落总杂项簇
    result = cluster_units([
        it("r1", None, directory="d1"),
        it("r2", None, directory="d2"),
        it("r3", None, directory=""),
        it("r4", None, directory="d1"),
    ])
    assert result.unclustered == ("r1", "r2", "r3", "r4")  # 输入序
    assert result.stats["embedded_count"] == 0
    groups = by_members(result)
    assert tuple(groups[frozenset({"r1", "r4"})].member_ids) == ("r1", "r4")
    assert tuple(groups[frozenset({"r2"})].member_ids) == ("r2",)
    assert tuple(groups[frozenset({"r3"})].member_ids) == ("r3",)
    assert all(c.is_misc and c.centroid == () for c in result.clusters)
    assert result.stats["misc_cluster_count"] == 3


def test_unclustered_pool_keeps_input_order() -> None:
    # 解散成员（输入序在前）与无向量成员（输入序在后）合并后仍按输入序
    result = cluster_units([
        it("a", (1, 0, 0, 0), directory="d1"),
        it("b", (1, 0, 0, 0), directory="d1"),   # 与 a 成 2 成员簇 → 解散
        it("c", None, directory="d2"),            # 无向量旁路（输入序在后）
    ])
    assert result.unclustered == ("a", "b", "c")


def test_every_member_has_home() -> None:
    # 权属完整性铁律：语义簇 ∪ 杂项簇成员 = 全体输入 id
    items = [
        it("e1", (1, 0, 0, 0), directory="d1"),
        it("e2", (1, 0, 0, 0), directory="d1"),
        it("e3", unit(1, 1, 0, 0), directory="d1"),
        it("x1", (0, 1, 0, 0), directory="d1"),   # 孤立 → 解散
        it("x2", None, directory="d2"),
    ]
    result = cluster_units(items)
    covered = [rid for c in result.clusters for rid in c.member_ids]
    assert sorted(covered) == sorted(i["representation_id"] for i in items)


# ------------------------------------------------------- 拆分 / 合并

def test_split_oversize_farthest_pair_seeds() -> None:
    # 3 成员超 max_cluster=2：最远对 (a,c) 为双种子；b 与 a 相似 0.894 >
    # 与 c 的 0.8 → 归 a。子簇不回灌转正（{c} 单成员保留——口径见模块
    # docstring）
    a, b, c = (1, 0, 0, 0), unit(1, 0.5, 0, 0), unit(0.5, 1, 0, 0)
    result = cluster_units(
        [it("a", a), it("b", b), it("c", c)], max_cluster=2)
    groups = by_members(result)
    assert frozenset({"a", "b"}) in groups
    assert frozenset({"c"}) in groups
    assert result.stats["split_count"] == 1
    assert all(len(c.member_ids) <= 2 for c in result.clusters)


def test_split_recursive_until_all_fit() -> None:
    # 5 个近同向向量、max_cluster=2 → 递归拆至全部 ≤2（不钉具体分组——
    # 拆分归属受浮点影响，断言结构性质+确定性）
    items = [it(f"v{i}", unit(1, 0.001 * i, 0, 0)) for i in range(5)]
    r1 = cluster_units(items, max_cluster=2)
    r2 = cluster_units(items, max_cluster=2)
    assert r1 == r2
    assert all(len(c.member_ids) <= 2 for c in r1.clusters)
    assert result_cover(r1) == {f"v{i}" for i in range(5)}
    assert r1.stats["split_count"] >= 2


def result_cover(result) -> set:
    return {rid for c in result.clusters for rid in c.member_ids}


def test_merge_above_threshold() -> None:
    # 两簇质心相似 ≈0.707 > merge_threshold=0.6（且 < sim_threshold=0.75，
    # 指派期不互吞）→ 合一
    items = [
        it("a1", (1, 0, 0, 0)),
        it("b1", unit(1, 1, 0, 0)),     # cos(a1,b1)=0.707 < 0.75 → 新种子
        it("a2", unit(1, 0.01, 0, 0)),  # 与 a1 近同向 → 入 A
        it("b2", unit(1, 1.02, 0, 0)),  # 与 b1 近同向 → 入 B
    ]
    result = cluster_units(
        items, sim_threshold=0.75, merge_threshold=0.6, min_promote=2)
    (only,) = result.clusters
    assert tuple(only.member_ids) == ("a1", "b1", "a2", "b2")  # 输入序
    assert not only.is_misc
    assert result.stats["merge_count"] == 1


def test_merge_iterates_to_stable() -> None:
    # 三簇 0°/10°/20°：两两质心相似 cos10°≈0.985 > 0.9——合并最大对后
    # 新质心与第三簇仍 > 0.9 → 迭代再合（merge_count=2 才稳定）
    items = [
        it("p0", (1, 0, 0, 0)),
        it("p1", unit(1, math.tan(math.radians(10)), 0, 0)),
        it("p2", unit(1, math.tan(math.radians(20)), 0, 0)),
    ]
    result = cluster_units(
        items, sim_threshold=0.99, merge_threshold=0.9, min_promote=1)
    (only,) = result.clusters
    assert set(only.member_ids) == {"p0", "p1", "p2"}
    assert result.stats["merge_count"] == 2


# ------------------------------------------------------- 确定性 / 身份

def test_determinism_same_input_equal_result() -> None:
    items = [
        it("e1", (1, 0, 0, 0), directory="d1"),
        it("e2", (1, 0, 0, 0), directory="d1"),
        it("e3", unit(1, 0.02, 0, 0), directory="d1"),  # 3 成员转正簇
        it("x1", (0, 1, 0, 0), directory="d1"),          # 孤立解散
        it("x2", (0, 1, 0, 0), directory="d2"),          # 孤立解散
        it("n1", None, directory=""),                    # 无向量
    ]
    r1, r2 = cluster_units(items), cluster_units(items)
    assert r1 == r2  # dataclass 全字段相等（验收②编译侧重跑稳定前提）
    # 输出簇列表按 cluster_id 字典序
    ids = [c.cluster_id for c in r1.clusters]
    assert ids == sorted(ids)
    assert r1.stats["embedded_ratio"] == pytest.approx(5 / 6, abs=1e-4)


def test_cluster_id_stable_hash_of_sorted_members() -> None:
    # 成员相同 → id 相同（与输入序无关：成员按排序参与哈希）；成员增减 → id 变
    two = cluster_units(
        [it("r2", (1, 0, 0, 0)), it("r1", (1, 0, 0, 0))], min_promote=2)
    cluster = two.clusters[0]
    assert tuple(cluster.member_ids) == ("r2", "r1")  # 簇内序=输入序
    assert cluster.cluster_id == cid("r1", "r2")      # id=排序哈希
    again = cluster_units(
        [it("r2", (1, 0, 0, 0)), it("r1", (1, 0, 0, 0))], min_promote=2)
    assert again.clusters[0].cluster_id == cluster.cluster_id

    three = cluster_units(
        [it("r2", (1, 0, 0, 0)), it("r1", (1, 0, 0, 0)),
         it("r3", (1, 0, 0, 0))], min_promote=3)
    assert three.clusters[0].cluster_id == cid("r1", "r2", "r3")
    assert three.clusters[0].cluster_id != cluster.cluster_id


def test_digest_members_capped_and_label_shape() -> None:
    # 15 成员簇：digest_members 截 12；heuristic_label=高频词 top3 + 首成员
    # 内容前 60 字（count 平局按字典序：乙甲 < 甲乙）
    items = [it(f"m{i:02d}", (1, 0, 0, 0), content="甲乙甲乙甲") for i in range(15)]
    result = cluster_units(items)
    (cluster,) = result.clusters
    assert len(cluster.digest_members) == DIGEST_MEMBERS_CAP
    assert tuple(cluster.digest_members) == tuple(cluster.member_ids)[:12]
    assert cluster.heuristic_label == "乙甲/甲乙｜甲乙甲乙甲"
    assert len(cluster.heuristic_label.split("｜", 1)[1]) <= 60


# ------------------------------------------------------- 取材上下文 / 高频词

def test_build_cluster_context_cap_truncation_and_header() -> None:
    cluster = Cluster(
        cluster_id=cid("m1", "m2", "m3"),
        member_ids=("m1", "m2", "m3"), centroid=(),
        heuristic_label="x", digest_members=("m1", "m2", "m3"),
    )
    lookup = {
        "m1": {"content_text": "A" * 10},
        "m2": {"content_text": "B" * 10},
        # m3 悬空：lookup 缺失 → 跳过（不编造内容）
    }
    ctx = build_cluster_context(cluster, lookup, cap=2, per_unit_chars=4)
    assert ctx == "[[src:m1]]\nAAAA\n\n[[src:m2]]\nBBBB"


def test_build_cluster_context_defaults() -> None:
    cluster = Cluster(
        cluster_id="x", member_ids=("m1",), centroid=(),
        heuristic_label="x", digest_members=("m1",),
    )
    long = "字" * 1000
    ctx = build_cluster_context(cluster, {"m1": {"content_text": long}})
    assert ctx == f"[[src:m1]]\n{'字' * 800}"


def test_top_terms_counts_ties_and_filtering() -> None:
    # 计数：甲乙=3 最高；count=2 平局按字典序（乙甲 < 甲乙）
    assert top_terms(["甲乙甲乙甲", "甲乙丙"], 3) == ["甲乙", "乙甲", "乙丙"]
    # 纯标点/空白 bigram 全滤；含标点/空白的 bigram 滤（a-b / "b " / " c"）
    assert top_terms(["！！。。", "a-b"]) == []
    assert top_terms(["ab cd"], 2) == ["ab", "cd"]
    assert top_terms([], 3) == []
    assert top_terms(["任意"], 0) == []


def test_all_without_embedding_bypass_to_misc() -> None:
    result = cluster_units([it("n1", None), it("n2", None)])
    assert result.stats["embedded_count"] == 0
    assert result.stats["cluster_count"] == 0
    assert all(c.is_misc for c in result.clusters)
    assert result_cover(result) == {"n1", "n2"}


def test_zero_vector_bypasses_like_missing_embedding() -> None:
    # 零向量无角度语义（cosine 无定义）→ 与无向量同旁路未聚类池（审查 M-3）
    result = cluster_units([
        it("z", (0, 0, 0, 0), directory="dz"),
        it("e1", (1, 0, 0, 0), directory="dz"),
        it("e2", (1, 0, 0, 0), directory="dz"),
        it("e3", unit(1, 0.02, 0, 0), directory="dz"),
    ])
    assert "z" in result.unclustered
    assert result.stats["embedded_count"] == 3  # 零向量不计 embedded
    assert result_cover(result) == {"z", "e1", "e2", "e3"}
    misc = [c for c in result.clusters if c.is_misc]
    assert tuple(misc[0].member_ids) == ("z",)  # 目录兜底仍有权属


# ------------------------------------------------- 审查修复轮（I-1/I-2）

def test_seed_admission_forces_assignment_at_cap() -> None:
    # 种子准入（审查 I-1）：max_seeds=3 且 5 个两两 <0.62 的方向——前 3 个
    # 各成种子；d/e 到达时种子已满 → 强制入最佳既有簇（d 与前三相似全 0，
    # 平局取先建簇 a；e 相似全 0.5 平局同落 a）
    items = [
        it("a", (1, 0, 0, 0)),
        it("b", (0, 1, 0, 0)),
        it("c", (0, 0, 1, 0)),
        it("d", (0, 0, 0, 1)),
        it("e", unit(1, 1, 1, 1)),
    ]
    capped = cluster_units(items, max_seeds=3, min_promote=1)
    groups = by_members(capped)
    assert frozenset({"a", "d", "e"}) in groups  # d/e 被强制并入
    assert frozenset({"b"}) in groups and frozenset({"c"}) in groups
    assert len(capped.clusters) == 3
    assert capped.stats["cluster_count"] == 3

    # 对照：默认 max_seeds=500 下同样输入 → 5 个种子簇（无强制指派）
    uncapped = cluster_units(items, min_promote=1)
    assert len(uncapped.clusters) == 5


def test_member_vec_normalized_as_float32_array() -> None:
    # 内存形态断言（审查 I-2）：归一化产物为 array('f')（~4KB/条，对齐
    # 取材侧；1024 维 tuple[float,...] ~32.8KB/条已退役）；f32 舍入值近似
    from knowledge_mining.mining.kb.wiki_clustering import _normalize
    vec = _normalize((3, 4, 0, 0))
    assert isinstance(vec, array) and vec.typecode == "f"
    assert vec[0] == pytest.approx(0.6, rel=1e-6)
    assert vec[1] == pytest.approx(0.8, rel=1e-6)
    assert _normalize((0, 0, 0, 0)) is None  # 零向量门控不变


def test_empty_items_and_param_validation() -> None:
    result = cluster_units([])
    assert result.clusters == () and result.unclustered == ()
    assert result.stats["unit_count"] == 0
    assert result.stats["embedded_ratio"] == 0.0
    with pytest.raises(ValueError):
        cluster_units([it("a", (1, 0, 0, 0))], max_cluster=0)
    with pytest.raises(ValueError):
        cluster_units([it("a", (1, 0, 0, 0))], min_promote=0)
    with pytest.raises(ValueError):
        cluster_units([it("a", (1, 0, 0, 0))], max_seeds=0)
