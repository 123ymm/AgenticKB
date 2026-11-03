"""KB Wiki 目录草稿（66号批次1）—— kb_wiki_tocs 仓储 + 发布解析。

独立模块防 db_wiki.py 破千行；WikiError/_json 复用 db_wiki（异常口径不
新造第二套：stale → WikiError("stale_artifact")，服务层 _WIKI_CODE_TO_ERROR
映射 StaleArtifact/409）。本模块不做 pool 管理——调用方给 conn（发布解析
必须在 publish 同事务；CRUD 由服务层在自有连接/事务内调用）。

机制权威 = docs/superpowers/plans/2026-11-03-artifacts-unified-ui.md 批次1
「目录定版设计」（表 DDL = 迁移 011 / 启动链 019 同文）：
- 草稿 toc 行（artifact_id 单行，page_key 级）写路径带乐观锁——比对**本行**
  updated_at（与 wiki 页 PUT 比对 kb_artifacts.updated_at 同口径的全精度
  比对；行不存在时首次写入跳过锁检查——编译钩子/用户首次编辑场景）。
- resolve_toc 发布解析：有 toc 行 → 剔除已不存在页键（warnings 记
  toc_dropped_page_keys）+ 未收录现存页追加尾部「其他」节（warnings 记
  toc_appended_page_keys）；无 toc 行 → 按 ordinal 派生（机器页
  ordinal//1000-1 为节索引归节 f"第{n}节"；专页 910000/920000/930000 基段
  归固定节「矛盾对照/开放问题/术语表」；manual 手工页归「手工页」节）。
  返回的 toc 永远是完整 page_key 级目录（所有现存页都出现在某节中）。
- warnings 是 list[str]（与 validate_pages_for_publish 的发布 warnings 同构
  ——服务层直接拼接透传）。
"""
from __future__ import annotations

from typing import Any

from .db_wiki import WikiError, _json

#: 专页固定节：ordinal 基段 [base, base+10000) → 节名（输出序即此序）。
_SPECIAL_BASES: tuple[tuple[int, str], ...] = (
    (910000, "矛盾对照"),
    (920000, "开放问题"),
    (930000, "术语表"),
)

#: 有 toc 行时未收录页的尾部追加节名（固定，warnings 记 appended 键）。
_TAIL_SECTION_TITLE = "其他"

#: 无 toc 行派生时 origin=manual 且不在专页段的页归此节。
_MANUAL_SECTION_TITLE = "手工页"


def validate_toc(toc: Any) -> dict[str, Any]:
    """toc 形态校验 + 归一（畸形 → ValueError，路由层映射 422 带具体细节）：
    必须是含 sections 列表的 dict；每节 {section_title: str, page_keys:
    [str, ...]}（元素无嵌套畸形）；页键全局唯一（跨节重复也是畸形——页
    归属唯一）。归一形态只保留两个契约键（多余键不落库）。"""
    if not isinstance(toc, dict):
        raise ValueError("toc must be an object with a 'sections' list")
    sections = toc.get("sections")
    if not isinstance(sections, list):
        raise ValueError("toc.sections must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            raise ValueError(f"toc.sections[{i}] must be an object")
        title = sec.get("section_title")
        if not isinstance(title, str):
            raise ValueError(
                f"toc.sections[{i}].section_title must be a string")
        keys = sec.get("page_keys")
        if not isinstance(keys, list):
            raise ValueError(
                f"toc.sections[{i}].page_keys must be a list of strings")
        for key in keys:
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"toc.sections[{i}].page_keys entries must be"
                    " non-empty strings")
            if key in seen:
                raise ValueError(f"duplicate page_key in toc: {key}")
            seen.add(key)
        normalized.append({"section_title": title, "page_keys": list(keys)})
    return {"sections": normalized}


async def get_draft_toc(conn: Any, artifact_id: str) -> dict[str, Any] | None:
    """读草稿目录行；无行（未知 artifact / 从未编辑过目录）→ None。"""
    cur = await conn.execute(
        """SELECT artifact_id, toc, updated_at, updated_by
           FROM kb_wiki_tocs WHERE artifact_id = %s""",
        (artifact_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_draft_toc(
    conn: Any, artifact_id: str, toc: Any, updated_by: str = "",
    expected_updated_at: Any = None,
) -> dict[str, Any]:
    """写草稿目录行（单事务：FOR UPDATE 锁行 → 令牌比对 → UPSERT）。

    乐观锁对象是**本行** updated_at（行不存在时首次写入跳过锁检查）；失配
    WikiError("stale_artifact")——两 editor 并发改目录不静默互相覆盖。
    返回完整行（toc 为归一形态，updated_at 是下次编辑的新令牌）。"""
    normalized = validate_toc(toc)
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT updated_at FROM kb_wiki_tocs"
            " WHERE artifact_id = %s FOR UPDATE",
            (artifact_id,),
        )
        row = await cur.fetchone()
        if row is not None and expected_updated_at is not None \
                and row["updated_at"] != expected_updated_at:
            raise WikiError("stale_artifact")
        cur = await conn.execute(
            """INSERT INTO kb_wiki_tocs (artifact_id, toc, updated_at, updated_by)
               VALUES (%s, %s::jsonb, now(), %s)
               ON CONFLICT (artifact_id) DO UPDATE SET
                   toc = EXCLUDED.toc,
                   updated_at = now(),
                   updated_by = EXCLUDED.updated_by
               RETURNING artifact_id, toc, updated_at, updated_by""",
            (artifact_id, _json(normalized), updated_by),
        )
        return dict(await cur.fetchone())  # type: ignore[arg-type]


def _derive_toc_from_manifest(page_manifest: list[dict[str, Any]]) -> dict:
    """无草稿行时按 ordinal 派生：机器页 ordinal//1000-1 为节索引（节名
    f"第{idx+1}节"；页序=manifest 序即 ordinal 升序）；专页按
    910000/920000/930000 基段归固定节；origin=manual 且不在专页段 →
    「手工页」节。节输出序：机器节升序 → 三专页固定序 → 手工页（派生
    场景无人编辑过目录，空节无信息量故不出现）。"""
    machine: dict[int, list[str]] = {}
    special: dict[str, list[str]] = {name: [] for _, name in _SPECIAL_BASES}
    manual: list[str] = []
    for page in page_manifest:
        key = str(page["page_key"])
        ordinal = int(page.get("ordinal") or 0)
        matched = None
        for base, name in _SPECIAL_BASES:
            if base <= ordinal < base + 10000:
                matched = name
                break
        if matched is not None:
            special[matched].append(key)
        elif str(page.get("origin") or "") == "manual":
            manual.append(key)
        else:
            machine.setdefault(ordinal // 1000 - 1, []).append(key)
    sections: list[dict[str, Any]] = [
        {"section_title": f"第{idx + 1}节", "page_keys": keys}
        for idx, keys in sorted(machine.items())
    ]
    sections.extend(
        {"section_title": name, "page_keys": keys}
        for name, keys in special.items() if keys
    )
    if manual:
        sections.append(
            {"section_title": _MANUAL_SECTION_TITLE, "page_keys": manual})
    return {"sections": sections}


async def resolve_toc(
    conn: Any, artifact_id: str, page_manifest: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """发布目录解析（publish 同事务调用）→ (toc, warnings)。

    有 toc 行：死键剔除（warnings 记 toc_dropped_page_keys:键1,键2）+
    未收录现存页追加尾部「其他」节（warnings 记 toc_appended_page_keys）；
    空节保留（用户显式组织的节结构不因页删除静默消失）。无 toc 行：按
    ordinal 派生（见 _derive_toc_from_manifest），无警示。两种路径都保证
    返回完整 page_key 级目录（所有现存页都出现在某节中）。"""
    existing_keys = [str(p["page_key"]) for p in page_manifest]
    existing = set(existing_keys)
    row = await get_draft_toc(conn, artifact_id)
    if row is None:
        return _derive_toc_from_manifest(page_manifest), []

    draft = row["toc"] if isinstance(row["toc"], dict) else {}
    sections_out: list[dict[str, Any]] = []
    dropped: list[str] = []
    covered: set[str] = set()
    for sec in draft.get("sections") or []:
        keys_in = [str(k) for k in (sec.get("page_keys") or [])]
        kept = [k for k in keys_in if k in existing]
        dropped.extend(k for k in keys_in if k not in existing)
        covered.update(kept)
        sections_out.append({
            "section_title": str(sec.get("section_title") or ""),
            "page_keys": kept,
        })
    appended = [k for k in existing_keys if k not in covered]
    if appended:
        sections_out.append(
            {"section_title": _TAIL_SECTION_TITLE, "page_keys": appended})
    warnings: list[str] = []
    if dropped:
        warnings.append("toc_dropped_page_keys:" + ",".join(dropped))
    if appended:
        warnings.append("toc_appended_page_keys:" + ",".join(appended))
    return {"sections": sections_out}, warnings
