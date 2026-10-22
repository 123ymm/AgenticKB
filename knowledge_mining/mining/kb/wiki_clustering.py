"""Wiki 制品编译聚类器（63号 §4.2 ② / 批次3 Task 2）—— 纯函数、零 IO、
纯 stdlib（依赖冻结：不引 numpy/分词库）。

输入 = collect_units 产出的单元行（embedding 为 array('f') 或 None——无向量
单元不参与聚类，旁路到未聚类池）；输出 ClusterResult。流水线：

    增量指派 → <min_promote 解散 → 簇间合并（迭代至稳定）→ 超限递归拆分
    → 未聚类按 directory_path 兜底成杂项簇（每成员必须有权属——取材完整）

设计口径（对任务书裁定的落点）：
- **增量指派**：逐项与既有簇**质心**比相似度（归一化向量点积；质心=现成员
  归一化均值，首成员即质心初始化）；≥sim_threshold 入最高相似簇，否则新
  种子簇。数值平局按簇创建序裁决（创建序由输入序派生——确定性等价）。
- **合并先于拆分**（任务书合并条「合并后若超 max_cluster 交给拆分规则」）；
  拆分产物**不回灌**合并/转正——子簇源自已转正簇，保持权属，双种子取簇内
  最远对、子簇质心间距构造上已拉开。
- **杂项簇**：目录兜底容器非语义簇——centroid 空、不参与拆/合；空目录
  （directory_path=""，根目录文档）成员同落**总杂项簇**。
- **确定性铁律**：输入序固定（调用方 ORDER BY snapshot_id, ordinal,
  representation_id 已保证）是全链根基；簇内成员序/digest_members/杂项簇
  组内序全按输入序；输出簇列表按 cluster_id 字典序——同输入两次调用输出
  逐字段相等（验收②「重跑稳定性」的编译侧前提）。
- **cluster_id** = 成员 representation_ids 排序后 sha256[:12]——成员集合
  决定身份（与成员到达序无关），成员增减必变 id。
- 零向量 embedding 无角度语义（cosine 无定义）→ 与无向量同旁路未聚类池。

设计偏差记录（批次3 决策7）：MVP 不做「凝聚度低重聚」——增量指派+拆+合
已覆盖主逻辑。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Sequence

#: digest_members / build_cluster_context 默认取材单元数上限（63号取材口径）
DIGEST_MEMBERS_CAP = 12

#: heuristic_label 首成员内容截断长度（字）
_LABEL_SNIPPET_CHARS = 60

#: heuristic_label 高频词个数
_LABEL_TERMS = 3


@dataclass(frozen=True)
class Cluster:
    """聚类输出簇（不可变——确定性比较的载体）。

    centroid 为成员归一化向量的归一化均值；杂项簇（is_misc=True）为目录
    兜底容器，centroid 恒空元组。
    """

    cluster_id: str
    member_ids: tuple[str, ...]
    centroid: tuple[float, ...]
    heuristic_label: str
    digest_members: tuple[str, ...]
    is_misc: bool = False


@dataclass(frozen=True)
class ClusterResult:
    """聚类结果：clusters 含杂项簇（按 cluster_id 字典序）；unclustered 为
    未入语义簇的 id（输入序——成员已由杂项簇兜底覆盖）；stats 为量级账
    （Task 3 details.stats 数据源）。"""

    clusters: tuple[Cluster, ...]
    unclustered: tuple[str, ...]
    stats: dict[str, Any]


class _Member(NamedTuple):
    """内部成员：输入序号（确定性键）+ 归一化向量 + 取材字段。"""

    index: int
    rid: str
    vec: tuple[float, ...]
    content: str
    directory: str


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _normalize(vec: Sequence[float]) -> tuple[float, ...] | None:
    """归一化；零向量返回 None（无角度语义，旁路未聚类池）。"""
    n = math.sqrt(sum(x * x for x in vec))
    if n <= 0.0:
        return None
    return tuple(x / n for x in vec)


def _unit_from_sum(sum_vec: Sequence[float]) -> tuple[float, ...]:
    """成员向量和（均单位向量）→ 归一化质心。和非零数学上恒成立；防御
    零和直接返回原样（不参与相似度语义，不影响确定性）。"""
    n = math.sqrt(sum(x * x for x in sum_vec))
    if n <= 0.0:
        return tuple(sum_vec)
    return tuple(x / n for x in sum_vec)


class _Forming:
    """内部可变工作簇：成员向量滚动求和（质心按需归一化——O(dim)/次比较），
    输出前冻结为 Cluster。members 恒按输入序维护（merge 时重排）。"""

    __slots__ = ("members", "sum_vec")

    def __init__(self, first: _Member) -> None:
        self.members: list[_Member] = [first]
        self.sum_vec: list[float] = list(first.vec)

    def add(self, m: _Member) -> None:
        self.members.append(m)
        for i, x in enumerate(m.vec):
            self.sum_vec[i] += x

    def centroid(self) -> tuple[float, ...]:
        return _unit_from_sum(self.sum_vec)

    def similarity(self, vec: Sequence[float]) -> float:
        return _dot(vec, self.centroid())

    @classmethod
    def merged(cls, a: "_Forming", b: "_Forming") -> "_Forming":
        c = cls.__new__(cls)
        c.members = sorted(a.members + b.members, key=lambda m: m.index)
        c.sum_vec = [x + y for x, y in zip(a.sum_vec, b.sum_vec)]
        return c


# ------------------------------------------------------------ 对外纯函数

def top_terms(texts: Sequence[str], k: int = 3) -> list[str]:
    """简易中文高频词（依赖冻结——不引分词）：字符 bigram 计数，过滤含
    标点/空白字符的 bigram（两字符均 isalnum 才计）；计数降序、bigram
    字典序平局裁决，取前 k。"""
    if k <= 0:
        return []
    counts: dict[str, int] = {}
    for text in texts:
        s = text or ""
        for a, b in zip(s, s[1:]):
            if a.isalnum() and b.isalnum():
                key = a + b
                counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ordered[:k]]


def cluster_units(
    items: Sequence[Mapping[str, Any]],
    *,
    sim_threshold: float = 0.62,
    merge_threshold: float = 0.85,
    max_cluster: int = 100,
    min_promote: int = 3,
) -> ClusterResult:
    """增量聚类主入口（口径见模块 docstring）。

    items 各行至少含 representation_id / content_text / directory_path /
    embedding(array('f')|None)；多余键（collect_units 的 snapshot_id 等）
    容忍不读。量级护栏在编译 start（Σ unit_count > 30000 → 422），本函数
    信任调用方。
    """
    if max_cluster < 1:
        raise ValueError("max_cluster 必须 ≥ 1")
    if min_promote < 1:
        raise ValueError("min_promote 必须 ≥ 1")

    members: list[_Member] = []
    # 未聚类池（无向量/零向量/解散成员）：(输入序号, id, 内容, 目录)
    pool: list[tuple[int, str, str, str]] = []
    for idx, row in enumerate(items):
        rid = str(row.get("representation_id", ""))
        content = str(row.get("content_text") or "")
        directory = str(row.get("directory_path") or "")
        raw = row.get("embedding")
        vec = _normalize(raw) if raw is not None else None
        if vec is None:
            pool.append((idx, rid, content, directory))
            continue
        members.append(_Member(idx, rid, vec, content, directory))

    forming = _assign(members, sim_threshold)

    # <min_promote 解散（转正门禁）
    promoted: list[_Forming] = []
    dissolved = 0
    for c in forming:
        if len(c.members) < min_promote:
            pool.extend((m.index, m.rid, m.content, m.directory)
                        for m in c.members)
            dissolved += 1
        else:
            promoted.append(c)

    # 簇间合并（迭代至稳定）→ 超限拆分（递归至全部 ≤ max_cluster）
    promoted, merge_count = _merge_clusters(promoted, merge_threshold)
    promoted, split_count = _split_oversize(promoted, sim_threshold, max_cluster)

    clusters = [_freeze(c) for c in promoted]
    pool.sort(key=lambda e: e[0])  # 未聚类账按输入序
    clusters.extend(_misc_fallback(pool))
    clusters.sort(key=lambda c: c.cluster_id)  # 输出确定性序

    embedded = len(members)
    total = len(items)
    stats: dict[str, Any] = {
        "unit_count": total,
        "embedded_count": embedded,
        "embedded_ratio": round(embedded / total, 4) if total else 0.0,
        "cluster_count": len(promoted),
        "misc_cluster_count": len(clusters) - len(promoted),
        "unclustered_count": len(pool),
        "dissolved_count": dissolved,
        "merge_count": merge_count,
        "split_count": split_count,
    }
    return ClusterResult(
        clusters=tuple(clusters),
        unclustered=tuple(e[1] for e in pool),
        stats=stats,
    )


def build_cluster_context(
    cluster: Cluster,
    units_lookup: Mapping[str, Mapping[str, Any]],
    *,
    cap: int = 12,
    per_unit_chars: int = 800,
) -> str:
    """簇取材上下文（簇级写作 prompt 素材，63号 §4.4 引用规范——正文须带
    ``[[src:representation_id]]``，标头行给全 id 供 LLM 抄引）。

    取 cluster.digest_members 前 cap 个，每单元 content_text 截
    per_unit_chars；lookup 缺失的 id 跳过（不编造内容——调用方用同一批
    items 建查找表，悬空只可能来自调用方缺陷，交由引用校验兜底）。
    """
    blocks: list[str] = []
    for rid in cluster.digest_members[:max(cap, 0)]:
        unit = units_lookup.get(rid)
        if unit is None:
            continue
        text = str(unit.get("content_text") or "")[:max(per_unit_chars, 0)]
        blocks.append(f"[[src:{rid}]]\n{text}")
    return "\n\n".join(blocks)


# ------------------------------------------------------------ 内部算法步

def _assign(members: Sequence[_Member],
            sim_threshold: float) -> list[_Forming]:
    """增量指派：逐项与既有簇质心比相似度，≥sim_threshold 入最高相似簇
    （平局取先建簇——创建序由输入序派生）；否则新种子簇。"""
    clusters: list[_Forming] = []
    for m in members:
        best: _Forming | None = None
        best_sim = 0.0
        for c in clusters:
            sim = c.similarity(m.vec)
            if best is None or sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= sim_threshold:
            best.add(m)
        else:
            clusters.append(_Forming(m))
    return clusters


def _merge_clusters(clusters: list[_Forming],
                    merge_threshold: float) -> tuple[list[_Forming], int]:
    """簇间合并（迭代至稳定）：每轮取质心相似度最大且 > merge_threshold
    的簇对合并；簇列表恒按首成员输入序排布（成员集互斥 → 键唯一，确定性
    扫描序）。返回 (簇列表, 合并次数)。"""
    ordered = sorted(clusters, key=lambda c: c.members[0].index)
    merged = 0
    while True:
        best_i = best_j = -1
        best_sim = merge_threshold  # 严格大于阈值才合
        for i in range(len(ordered)):
            ci = ordered[i].centroid()
            for j in range(i + 1, len(ordered)):
                sim = _dot(ci, ordered[j].centroid())
                if sim > best_sim:
                    best_sim, best_i, best_j = sim, i, j
        if best_i < 0:
            break
        combined = _Forming.merged(ordered[best_i], ordered[best_j])
        merged += 1
        ordered = [c for k, c in enumerate(ordered)
                   if k not in (best_i, best_j)]
        ordered.append(combined)
        ordered.sort(key=lambda c: c.members[0].index)
    return ordered, merged


def _split_oversize(clusters: list[_Forming], sim_threshold: float,
                    max_cluster: int) -> tuple[list[_Forming], int]:
    """超限拆分（递归）：成员数 > max_cluster 的簇取**两两最远对**（余弦
    最小，平局取扫描序）为双种子，簇内按输入序重跑增量指派；子簇仍超限
    则继续拆直至全部 ≤ max_cluster（每轮最大子簇严格变小，必然终止）。
    返回 (簇列表, 拆分次数)。"""
    done: list[_Forming] = []
    queue = sorted(clusters, key=lambda c: c.members[0].index)
    splits = 0
    while queue:
        c = queue.pop(0)
        if len(c.members) <= max_cluster:
            done.append(c)
            continue
        splits += 1
        seed_a, seed_b = _farthest_pair(c.members)
        skip = {seed_a.index, seed_b.index}
        subs = [_Forming(seed_a), _Forming(seed_b)]
        for m in c.members:  # 输入序重跑增量指派（同 _assign 规则）
            if m.index in skip:
                continue
            best: _Forming | None = None
            best_sim = 0.0
            for sc in subs:
                sim = sc.similarity(m.vec)
                if best is None or sim > best_sim:
                    best, best_sim = sc, sim
            if best is not None and best_sim >= sim_threshold:
                best.add(m)
            else:
                subs.append(_Forming(m))
        queue.extend(subs)
    done.sort(key=lambda c: c.members[0].index)
    return done, splits


def _farthest_pair(members: Sequence[_Member]) -> tuple[_Member, _Member]:
    """簇内两两余弦最小对（平局取扫描序——members 输入序）。调用前提
    len(members) ≥ 2（拆分仅在 > max_cluster ≥ 1 时触发）。"""
    best = (members[0], members[1])
    best_sim = _dot(members[0].vec, members[1].vec)
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            sim = _dot(members[i].vec, members[j].vec)
            if sim < best_sim:
                best_sim = sim
                best = (members[i], members[j])
    return best


def _cluster_hash(member_ids: Sequence[str]) -> str:
    """cluster_id = 成员排序后 sha256[:12]（成员集合决定身份）。"""
    payload = "\n".join(sorted(member_ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _make_label(texts: Sequence[str]) -> str:
    """heuristic_label = 高频词 top3（/ 连接）+ 首成员内容前 60 字（｜
    分隔）；无高频词（纯标点/空内容）时仅摘要段。"""
    terms = top_terms(texts, _LABEL_TERMS)
    head = (texts[0] or "")[:_LABEL_SNIPPET_CHARS]
    return f"{'/'.join(terms)}｜{head}" if terms else head


def _freeze(c: _Forming) -> Cluster:
    """工作簇 → 语义簇（成员序=输入序；质心/标签/摘要一次定形）。"""
    member_ids = tuple(m.rid for m in c.members)
    return Cluster(
        cluster_id=_cluster_hash(member_ids),
        member_ids=member_ids,
        centroid=c.centroid(),
        heuristic_label=_make_label([m.content for m in c.members]),
        digest_members=member_ids[:DIGEST_MEMBERS_CAP],
    )


def _misc_fallback(
    pool: Sequence[tuple[int, str, str, str]],
) -> list[Cluster]:
    """未聚类兜底：按 directory_path 分组（None/空串归 ""——**总杂项簇**），
    每组一簇（cluster_id 同哈希规则；组内序=输入序——pool 已按输入序）。
    杂项簇 centroid 空、不参与拆/合（兜底权属容器非语义簇）。"""
    groups: dict[str, list[tuple[int, str, str, str]]] = {}
    for entry in pool:
        groups.setdefault(entry[3] or "", []).append(entry)
    out: list[Cluster] = []
    for entries in groups.values():
        member_ids = tuple(e[1] for e in entries)
        out.append(Cluster(
            cluster_id=_cluster_hash(member_ids),
            member_ids=member_ids,
            centroid=(),
            heuristic_label=_make_label([e[2] for e in entries]),
            digest_members=member_ids[:DIGEST_MEMBERS_CAP],
            is_misc=True,
        ))
    return out
