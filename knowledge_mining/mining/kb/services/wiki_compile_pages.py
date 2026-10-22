"""Wiki 编译管线确定性加工函数（63号批次3 Task 3）—— 目录形状/页展开/
provenance 分组/引用校验 flags/专页模板生成/发布校验。纯代码无 LLM（§4.4
铁律：LLM 只复核，存在性与密度是确定性检查）；服务主文件 800 行铁律拆出。

`validate_pages_for_publish` 是批次3 决策7 的发布校验导出（wiki_compile_
service 转出、WikiLifecycle.publish 复用——仅 kind=wiki 走该路径）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from knowledge_mining.mining.kb.db_wiki import WikiError, normalize_page_key

logger = logging.getLogger(__name__)

#: 密度告警阈值（63号 §4.4：连续 ≥3 事实句无引用）。
DENSITY_STREAK = 3

#: 专页数量上限（术语 30 / 待解 20——超出截断记 stats）。
GLOSSARY_CAP = 30
OPEN_QUESTION_CAP = 20

#: 行内引用标记 ``[[src:representation_id]]``（与 db_wiki._WIKI_LINK_RE 的
#: src 跳过口径互补——本模块专抽 src 形态做存在性校验）。
_SRC_REF_RE = re.compile(r"\[\[src:([^\[\]]+)\]\]")


def extract_src_refs(body_md: str) -> list[str]:
    """抽正文行内引用 ``[[src:id]]`` → 去重保序 id 列表（存在性校验素材）。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _SRC_REF_RE.finditer(body_md or ""):
        rid = m.group(1).strip()
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def density_streak(body_md: str) -> bool:
    """密度 lint（§4.4）：连续 ≥DENSITY_STREAK 个 ``- `` 事实行无 ``[[src:`` →
    True。非列表非空行（标题等）打断连续段；空行不打断（markdown 同列表）。"""
    streak = 0
    for line in (body_md or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            if "[[src:" in stripped:
                streak = 0
            else:
                streak += 1
                if streak >= DENSITY_STREAK:
                    return True
        elif stripped:
            streak = 0
    return False


def normalize_toc(obj: Any) -> dict[str, Any] | None:
    """LLM 输出 / 用户编辑版 toc 的形状归一：``{"toc": [{section_title,
    page_hints: [{title, cluster_ids, page_type}]}], "glossary": [...],
    "open_questions": [...]}``；任一刚性形状不符 → None（调用方走重试/422）。
    page_type 恒收窄 topic（目录页提示不支持专页类型——专页由质量步生成）。"""
    if not isinstance(obj, dict):
        return None
    toc = obj.get("toc")
    if not isinstance(toc, list) or not toc:
        return None
    sections: list[dict[str, Any]] = []
    for sec in toc:
        if not isinstance(sec, dict):
            return None
        title = str(sec.get("section_title") or "").strip()
        hints = sec.get("page_hints")
        if not title or not isinstance(hints, list):
            return None
        pages: list[dict[str, Any]] = []
        for hint in hints:
            if not isinstance(hint, dict):
                return None
            page_title = str(hint.get("title") or "").strip()
            if not page_title:
                return None
            cids = hint.get("cluster_ids")
            if not isinstance(cids, list):
                cids = []
            pages.append({
                "title": page_title,
                "cluster_ids": [str(c) for c in cids],
                "page_type": "topic",
            })
        sections.append({"section_title": title, "page_hints": pages})

    def _entries(raw: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                entry = {k: str(item.get(k) or "").strip() for k in keys}
                if any(entry.values()):
                    out.append(entry)
        return out

    return {
        "toc": sections,
        "glossary": _entries(obj.get("glossary"),
                             ("term", "definition_hint")),
        "open_questions": _entries(obj.get("open_questions"),
                                   ("question", "why")),
    }


def toc_page_count(toc: list[Any]) -> int:
    """目录展开页数（Σ各节 page_hints）。"""
    return sum(len(s.get("page_hints") or []) for s in toc
               if isinstance(s, dict))


def expand_toc_pages(toc: list[Any]) -> list[dict[str, Any]]:
    """目录 → 待写页清单：{title, page_type, cluster_ids, ordinal=节序*100+
    页序}（文档序）。"""
    pages: list[dict[str, Any]] = []
    for si, sec in enumerate(toc):
        if not isinstance(sec, dict):
            continue
        for pi, hint in enumerate(sec.get("page_hints") or []):
            if not isinstance(hint, dict):
                continue
            pages.append({
                "title": str(hint.get("title") or "").strip(),
                "page_type": str(hint.get("page_type") or "topic"),
                "cluster_ids": [str(c) for c in (hint.get("cluster_ids")
                                                 or [])],
                "ordinal": (si + 1) * 100 + (pi + 1),
            })
    return pages


def toc_digest_text(toc: list[Any]) -> str:
    """全局目录文本（页写作 system 上下文——每页带全书目录，LLM-Wiki 口径）。"""
    lines: list[str] = []
    for si, sec in enumerate(toc, 1):
        if not isinstance(sec, dict):
            continue
        lines.append(f"{si}. {sec.get('section_title', '')}")
        for pi, hint in enumerate(sec.get("page_hints") or [], 1):
            title = str((hint or {}).get("title") or "")
            try:
                key = normalize_page_key(title)
            except WikiError:
                key = ""
            lines.append(f"   {si}.{pi} {title}（key: {key}）")
    return "\n".join(lines)


def blockquote_of(body_md: str) -> str:
    """正文首个 blockquote 行 → 摘要句（质量 LLM 输入；缺省空串）。"""
    for line in (body_md or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            return stripped.lstrip(">").strip()
        if stripped and not stripped.startswith("#"):
            break
    return ""


def group_provenance(
    cited_ids: list[str], member_units: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """cited_ids 按 (kb_id, snapshot_id) 分组（决策6 多源形状：簇可横跨多
    KB，representation_id 全局 PK 归属无歧义）。组内 unit_ids 保序去重；
    条目按 (kb_id, snapshot_id) 排序稳定。member_units: rid → {kb_id,
    snapshot_id}。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for rid in cited_ids:
        unit = member_units.get(rid)
        if unit is None:
            continue
        key = (str(unit.get("kb_id") or ""),
               str(unit.get("snapshot_id") or ""))
        bucket = groups.setdefault(key, [])
        if rid not in bucket:
            bucket.append(rid)
    return [
        {"kb_id": kb, "snapshot_id": snap, "unit_ids": ids}
        for (kb, snap), ids in sorted(groups.items())
    ]


def merge_provenance(entries: list[Any]) -> list[dict[str, Any]]:
    """多条 provenance 并集（矛盾页=双方来源页 provenance 合并；同 (kb,snap)
    合并 unit_ids 保序去重）。"""
    collected: dict[tuple[str, str], list[str]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = (str(entry.get("kb_id") or ""),
               str(entry.get("snapshot_id") or ""))
        bucket = collected.setdefault(key, [])
        for uid in entry.get("unit_ids") or []:
            uid = str(uid)
            if uid not in bucket:
                bucket.append(uid)
    return [
        {"kb_id": kb, "snapshot_id": snap, "unit_ids": ids}
        for (kb, snap), ids in sorted(collected.items())
    ]


def page_flags(
    page_type: str, provenance: list[Any], body_md: str,
    valid_unit_ids: set[str],
) -> dict[str, Any]:
    """页级引用校验三件（§4.4，确定性代码非 LLM）→ flags：
    provenance 空（仅 topic 计）→ provenance_missing；行内 src ∉ 取材全集 →
    dangling_refs=[...]；连续 ≥3 事实句无引用 → density_warning。"""
    flags: dict[str, Any] = {}
    if page_type == "topic" and not provenance:
        flags["provenance_missing"] = True
    dangling = [r for r in extract_src_refs(body_md)
                if r not in valid_unit_ids]
    if dangling:
        flags["dangling_refs"] = dangling
    if density_streak(body_md):
        flags["density_warning"] = True
    return flags


def constraint_text(broken_edges: list[dict[str, Any]]) -> str:
    """批间约束文本（前批断链清单注入后批——LLM-Wiki 五段生命周期 MVP 版）。"""
    if not broken_edges:
        return ""
    lines = [
        f"- [[{e.get('to_page')}]]（来自页 {e.get('from_page')}）当前是断链，"
        "如需相关页面引用请改用页键清单中的键"
        for e in broken_edges[:50]
    ]
    return "\n".join(lines)


def tags_from_label(label: str, fallback: str) -> list[str]:
    """heuristic_label（'词1/词2/词3｜摘要'）→ tags（前 3 词；无词回退标题）。"""
    head = str(label or "").split("｜")[0]
    tags = [t for t in head.split("/") if t.strip()]
    if not tags:
        tags = [fallback[:20]]
    return tags[:3]


# ------------------------------------------------- 决策7：发布校验（导出）

async def validate_pages_for_publish(
    wdb: Any, artifact_id: str,
) -> tuple[list[str], list[str]]:
    """发布前对 rev0 全页的确定性校验（决策7；WikiLifecycle.publish 复用）。

    返回 (blocking, warnings)：
    - 任何页行内 ``[[src:]]`` 悬空（∉ 该页 provenance unit_ids ∪ 全 rev0
      provenance 并集）→ blocking（§4.4 铁律：LLM 只复核不判存在性）；
    - machine **topic** 页 provenance 空 → blocking（机器可查来源账硬性）；
    - manual 页 provenance 空 → warning（记录偏差：设计「硬性」降警告——批次2
      编辑器无 provenance 录入 UI，硬阻断封死人工页发布路径；补录入 UI 后可
      收紧）；
    - 密度（连续 ≥3 事实句无引用）→ warning（告警不阻断）；
    - 专页宽免：contradiction/open_question/glossary 的 machine 页无直接
      取材单元（来源账在 frontmatter source_a/source_b/definition_source；
      contradiction 页自身带双方来源页 provenance 并集，天然非空）。
    """
    blocking: list[str] = []
    warnings: list[str] = []
    listing = await wdb.list_pages(artifact_id, revision=0)
    pages: list[dict[str, Any]] = []
    for meta in listing:
        page = await wdb.get_page(artifact_id, 0, str(meta["page_key"]))
        if page is not None:
            pages.append(page)
    union_ids: set[str] = set()
    for page in pages:
        for entry in page.get("provenance") or []:
            if isinstance(entry, dict):
                union_ids.update(
                    str(u) for u in (entry.get("unit_ids") or []))
    for page in pages:
        key = str(page.get("page_key") or "")
        origin = str(page.get("origin") or "")
        page_type = str(page.get("page_type") or "topic")
        provenance = page.get("provenance") or []
        body = str(page.get("body_md") or "")
        dangling = [r for r in extract_src_refs(body) if r not in union_ids]
        if dangling:
            blocking.append(f"{key}: 悬空引用 {dangling[:5]}")
        if page_type == "topic":
            if origin == "machine" and not provenance:
                blocking.append(f"{key}: machine 页 provenance 缺失")
            elif origin == "manual" and not provenance:
                warnings.append(f"{key}: 人工页未登记 provenance")
        if density_streak(body):
            warnings.append(f"{key}: 密度告警（连续≥3 事实句无引用）")
    return blocking, warnings


# ------------------------------------------------- 页写与专页生成（模板）

#: 专页 ordinal 基段（主题页节序*100+页序远小于该值——专页排全书尾部）。
_ORDINAL_CONTRADICTION = 910000
_ORDINAL_OPEN_QUESTION = 920000
_ORDINAL_GLOSSARY = 930000


async def write_machine_page(
    wdb: Any, artifact_id: str, *, page_key: str, title: str,
    page_type: str, frontmatter: dict[str, Any], body_md: str,
    provenance: list[Any], flags: dict[str, Any], ordinal: int, user: str,
) -> str:
    """origin 保护写（§3.2）：rev0 同键 manual → 'conflict'（不碰）；否则
    machine UPSERT（覆盖旧 machine 页=重编译语义）。expected_updated_at 恒
    None（跳比对仍 touch——批次2 已核实的编译口径）。"""
    existing = await wdb.get_page(artifact_id, 0, page_key)
    if existing is not None and str(existing.get("origin") or "") == "manual":
        return "conflict"
    await wdb.upsert_page(
        artifact_id, page_key=page_key, title=title,
        page_type=page_type, frontmatter=frontmatter,
        body_md=body_md, provenance=provenance, flags=flags,
        ordinal=ordinal, origin="machine", updated_by=user,
        expected_updated_at=None,
    )
    return "written"


async def write_derived_pages(
    wdb: Any, artifact_id: str, user: str, quality: dict[str, Any],
    glossary: list[dict[str, Any]],
    open_questions: list[dict[str, Any]],
    provenance_by_key: dict[str, list[Any]],
    written: list[dict[str, Any]],
) -> dict[str, Any]:
    """专页生成（代码模板非逐页 LLM）：contradiction（质量步）/open_question
    （质量缺口+prepare 问题合并，cap 20）/glossary（prepare 术语表，cap 30）。
    全部过 origin 保护；返回 stats 增量（专页计数与截断账）。

    contradiction 的 claim 页键必须取自已写页（质量输出悬空条目跳过）；
    open_question 的 question 强制 ？结尾（§4.3 frontmatter 严格口径）。
    """
    written_keys = {w["page_key"] for w in written}
    contradictions = 0
    open_question_pages = 0
    glossary_pages = 0
    conflicts_note: list[str] = []   # 专页撞 manual 键跳过（计数不阻断）

    for entry in quality.get("contradictions") or []:
        if not isinstance(entry, dict):
            continue
        key_a = str(entry.get("page_a") or "")
        key_b = str(entry.get("page_b") or "")
        claim_a = str(entry.get("claim_a") or "").strip()
        claim_b = str(entry.get("claim_b") or "").strip()
        if key_a not in written_keys or key_b not in written_keys:
            continue
        if not claim_a or not claim_b:
            continue
        severity = str(entry.get("severity") or "medium")
        if severity not in ("low", "medium", "high"):
            severity = "medium"
        title = f"矛盾-{key_a}-与-{key_b}"
        body = (
            f"# {title}\n\n> 跨页矛盾记录：{key_a} 与 {key_b} 对同一主题"
            "给出不一致主张。\n\n## 双方主张\n"
            f"- {key_a} 主张：{claim_a}\n- {key_b} 主张：{claim_b}\n\n"
            "## 分析\n（待人工分析）\n\n## 裁决\n（待人工裁决）"
        )
        outcome = await write_machine_page(
            wdb, artifact_id, page_key=normalize_page_key(title),
            title=title, page_type="contradiction",
            frontmatter={
                "type": "contradiction", "aliases": [title],
                "claim_a": claim_a, "claim_b": claim_b,
                "source_a": key_a, "source_b": key_b,
                "severity": severity,
            },
            body_md=body,
            provenance=merge_provenance(
                list(provenance_by_key.get(key_a) or [])
                + list(provenance_by_key.get(key_b) or [])),
            flags={}, ordinal=_ORDINAL_CONTRADICTION + contradictions,
            user=user,
        )
        if outcome == "written":
            contradictions += 1
        else:
            conflicts_note.append(title)

    # 待解页：质量缺口在前 + prepare 开放问题在后，cap 合计 20
    merged_questions: list[dict[str, str]] = []
    for gap in quality.get("gaps") or []:
        if isinstance(gap, dict) and str(gap.get("question") or "").strip():
            merged_questions.append({
                "question": str(gap.get("question")).strip(),
                "context": str(gap.get("context") or "").strip(),
            })
    for item in open_questions:
        question = str(item.get("question") or "").strip()
        if question:
            merged_questions.append({
                "question": question,
                "context": str(item.get("why") or "").strip(),
            })
    questions_dropped = max(0, len(merged_questions) - OPEN_QUESTION_CAP)
    for item in merged_questions[:OPEN_QUESTION_CAP]:
        question = item["question"]
        if not question.rstrip().endswith(("?", "？")):
            question = question.rstrip() + "？"
        title = f"待解-{question}"
        body = (
            f"# {title}\n\n> 开放问题：{question}\n\n## 问题\n{question}\n"
            f"\n## 上下文\n{item['context'] or '（编译管线提出）'}\n\n"
            "## 建议补材\n（待人工补充建议来源）"
        )
        outcome = await write_machine_page(
            wdb, artifact_id, page_key=normalize_page_key(title),
            title=title, page_type="open_question",
            frontmatter={
                "type": "open_question", "aliases": [title],
                "question": question, "priority": "medium",
                "suggested_sources": [],
            },
            body_md=body, provenance=[], flags={},
            ordinal=_ORDINAL_OPEN_QUESTION + open_question_pages, user=user,
        )
        if outcome == "written":
            open_question_pages += 1
        else:
            conflicts_note.append(title)

    # 术语页：prepare 术语表逐条，cap 30
    glossary_dropped = max(0, len(glossary) - GLOSSARY_CAP)
    for item in glossary[:GLOSSARY_CAP]:
        term = str(item.get("term") or "").strip()
        if not term:
            glossary_dropped += 1
            continue
        hint = str(item.get("definition_hint") or "").strip()
        title = f"术语-{term}"
        body = (
            f"# {title}\n\n> 术语定义：{term}。\n\n## 定义\n"
            f"{hint or '（待人工补充定义）'}\n\n## 用法\n（待人工补充）"
            "\n\n## 来源\n本词条由编译管线生成（definition_source=compile）。"
        )
        outcome = await write_machine_page(
            wdb, artifact_id, page_key=normalize_page_key(title),
            title=title, page_type="glossary",
            frontmatter={
                "type": "glossary", "aliases": [title, term],
                "term": term, "definition_source": "compile",
            },
            body_md=body, provenance=[], flags={},
            ordinal=_ORDINAL_GLOSSARY + glossary_pages, user=user,
        )
        if outcome == "written":
            glossary_pages += 1
        else:
            conflicts_note.append(title)

    if conflicts_note:
        logger.info("wiki 专页撞 manual 键跳过 artifact=%s pages=%s",
                    artifact_id, conflicts_note)
    return {
        "contradiction_pages": contradictions,
        "open_question_pages": open_question_pages,
        "open_questions_dropped": questions_dropped,
        "glossary_pages": glossary_pages,
        "glossary_dropped": glossary_dropped,
    }
