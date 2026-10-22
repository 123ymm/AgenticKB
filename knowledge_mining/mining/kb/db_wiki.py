"""KB Wiki 制品（63号）—— wiki 专有三表异步仓储 + 模块级纯函数。

风格契约与 mining/kb/db_artifacts.py 一致：TEXT id、参数化 SQL、dict_row、
多语句显式 ``conn.transaction()``、每方法自开连接（一个事务）。

机制权威 = docs/下一阶段/63号 §三/§五（与表格制品逐层对照）：
- rev0=设计态工作区，revN=发布快照；页主键 (artifact_id, revision, page_key)。
- 发布事务镜像 db_artifacts.publish：FOR UPDATE→门禁→GREATEST 版本号→
  pages/links 复制定格→revisions 行（schema_json=页面清单）→清 rev0→拨指针；
  wiki 增补门禁：rev0 页数=0 → wiki_no_pages。
- rollback_to 纯指针拨回（数据不动）——units 按版本保留（批次2决策①），
  回滚后检索直接用目标版 units，无需重算向量。
- units 投影（§5.1）幂等：先 DELETE 该 (artifact_id,revision) 再 INSERT；
  embedding 走文本字面量绑定（psycopg 无 pgvector 适配器，传 list 会炸）。
- 网关铁律：URL path 里 page_key 一律 UTF-8 hex 编码传输（raw path 含 % 即
  400，照抄 db_artifacts._encode_key_parts 的 row_key 先例）。
"""
from __future__ import annotations

import binascii
import json
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

#: diff 样例截断（counts 恒全量，added/removed/changed 各只回前 N 条）。
_DIFF_SAMPLE_LIMIT = 100

#: 检索单元分块上限（63号 §5.3：≤2000 字单块；超长按 H2 分节/段落硬切）。
_UNITS_CHUNK_LIMIT = 2000

#: 链接批量 INSERT 的行数护栏（对齐 db_artifacts._ROW_INSERT_BATCH 量级）。
_LINK_INSERT_BATCH = 1000

#: diff 参与字段（origin 翻 manual 也是真实待发布变更——63号 §3.2）。
_DIFF_FIELDS = ("title", "body_md", "frontmatter", "page_type", "origin")

#: page_key 归一剔除的字符类补集：保留 \w（Python 默认含中文）+ CJK 区间
#: （显式声明意图，防未来 re.ASCII 化）+ 连字符（空白折叠产物）。
_PAGE_KEY_DROP_RE = re.compile(r"[^\w一-鿿-]")

#: wiki 双链语法 ``[[目标]]`` / ``[[目标|锚文本]]``；``[[src:...]]`` 是引用标记。
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

#: 段落边界（超长节硬切分块用）。
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


def _json(obj: Any) -> str:
    if obj is None:
        return "null"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _vec_literal(vector: Any) -> str:
    """list[float] → pgvector 文本输入形态（'[1.0,2.0]'；写入用 %s::vector）。
    口径照抄 retrieval_projection/repositories_pg.py:_vector_literal——psycopg
    未注册 pgvector 适配器（依赖冻结不引入 pgvector 包），直接传 list 会炸。"""
    return "[" + ",".join(map(str, vector)) + "]"


class WikiError(Exception):
    """wiki 仓储层不变量冲突（发布门禁 / 非法 page_key / 乐观锁失配）。
    service 负责翻译（对齐 artifact_service._translate 模式）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------ 纯函数（顶部）

def normalize_page_key(title: str) -> str:
    """标题 → page_key：NFKC → strip → 空白折叠成 ``-`` → 剔除非法字符。
    结果可含中文（中文是 page_key 存储形态，仅传输层 hex 编码，见
    encode_page_key）；全剔空 → invalid_page_key。"""
    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    text = re.sub(r"\s+", "-", text)
    text = _PAGE_KEY_DROP_RE.sub("", text)
    if not text:
        raise WikiError("invalid_page_key")
    return text


def encode_page_key(page_key: str) -> str:
    """page_key → UTF-8 hex（URL path 安全 ASCII；网关对 raw path 含 % 一律
    400，中文/``/``/``:`` 主键值必须编码传输）。"""
    return binascii.hexlify(str(page_key).encode("utf-8")).decode("ascii")


def decode_page_key(hex_str: str) -> str:
    """hex → page_key（路由收 hex 参数后解码再查库）；空串 / 非法 hex /
    非法 UTF-8 字节序列 → invalid_page_key（合法 page_key 恒非空）。"""
    if not hex_str:
        raise WikiError("invalid_page_key")
    try:
        return binascii.unhexlify(str(hex_str).encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeEncodeError, UnicodeDecodeError):
        raise WikiError("invalid_page_key") from None


def extract_wiki_links(body_md: str) -> list[tuple[str, str]]:
    """解析 ``[[目标]]`` / ``[[目标|锚文本]]`` → (to_key, anchor_text)。
    跳过 ``[[src:...]]``（引用标记非页面链接）；按 (to_key, anchor) 去重
    保序（边主键含 anchor_text——同目标不同锚文本是不同边）。"""
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _WIKI_LINK_RE.finditer(body_md or ""):
        inner = m.group(1)
        if inner.startswith("src:"):
            continue
        if "|" in inner:
            to_key, _, anchor = inner.partition("|")
        else:
            to_key, anchor = inner, ""
        to_key = to_key.strip()
        anchor = anchor.strip()
        if not to_key:
            continue
        pair = (to_key, anchor)
        if pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
    return result


def compute_page_edges(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{page_key, frontmatter, body_md}] → 边表
    [{from_page, to_page, anchor_text, status}]（63号 §2.4）。

    目标解析归一：to_key 命中某页 page_key 或其 frontmatter.aliases →
    to_page 写该页 canonical page_key、status='valid'（canonical 键优先于
    alias；两页争同一 alias 时文档序前者胜）；未命中 → to_page 原样、
    status='broken'。归一是反向链接（WHERE to_page=?）与阅读器跳转（前端按
    canonical key 找页）的正确性前提；anchor_text 保留书写原文。"""
    resolve: dict[str, str] = {}
    for page in pages:
        key = str(page.get("page_key") or "")
        if key:
            resolve[key] = key
    for page in pages:
        key = str(page.get("page_key") or "")
        frontmatter = page.get("frontmatter") or {}
        aliases = frontmatter.get("aliases") if isinstance(frontmatter, dict) else None
        for alias in aliases or []:
            alias = str(alias).strip()
            if alias and alias not in resolve:
                resolve[alias] = key
    edges: list[dict[str, Any]] = []
    for page in pages:
        from_page = str(page.get("page_key") or "")
        if not from_page:
            continue
        for to_key, anchor in extract_wiki_links(page.get("body_md") or ""):
            edges.append({
                "from_page": from_page,
                "to_page": resolve.get(to_key, to_key),
                "anchor_text": anchor,
                "status": "valid" if to_key in resolve else "broken",
            })
    return edges


def _split_sections(body_md: str) -> list[tuple[str | None, list[str]]]:
    """按 H2（``## `` 行）分节：[(标题行|None, 其余行)]（文档序）；首个 H2
    之前的导语段标题行为 None，全空白导语段丢弃（不占节号）。"""
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []
    for line in (body_md or "").split("\n"):
        if line.startswith("## "):
            if heading is not None or any(ln.strip() for ln in lines):
                sections.append((heading, lines))
            heading = line
            lines = []
        else:
            lines.append(line)
    if heading is not None or any(ln.strip() for ln in lines):
        sections.append((heading, lines))
    return sections


def _split_oversized(text: str) -> list[str]:
    """段落硬切：空白行分段贪心合并（每块 ≤ 上限）；单段超限按字符硬切。"""
    blocks: list[str] = []
    current = ""
    for para in _PARA_SPLIT_RE.split(text):
        if not para.strip():
            continue
        if len(para) > _UNITS_CHUNK_LIMIT:
            if current:
                blocks.append(current)
                current = ""
            for i in range(0, len(para), _UNITS_CHUNK_LIMIT):
                blocks.append(para[i:i + _UNITS_CHUNK_LIMIT])
            continue
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= _UNITS_CHUNK_LIMIT:
            current = f"{current}\n\n{para}"
        else:
            blocks.append(current)
            current = para
    if current:
        blocks.append(current)
    return blocks


def chunk_page_for_units(
    title: str, body_md: str, page_key: str,
) -> list[tuple[str, str]]:
    """页面 → 检索单元分块（63号 §5.3）：≤2000 字单块（unit_key=page_key）；
    超长按 H2 分节、单节仍超长按段落硬切（unit_key=page_key#sN，文档序 1 起，
    分节+硬切后的最终块连续编号）。每块内容前置 ``# {title}`` 与节标题行
    （块文本携带页标题上下文）；上限量的是正文部分（前缀不计入）。"""
    title = str(title or "")
    body = body_md or ""
    page_prefix = f"# {title}\n\n"
    if len(body) <= _UNITS_CHUNK_LIMIT:
        return [(page_key, page_prefix + body)]
    contents: list[str] = []
    for heading, lines in _split_sections(body):
        # 首尾空行是标题行剥离的残留，裁掉——分块计量与键序不受位移干扰
        section_body = "\n".join(lines).strip("\n")
        section_prefix = page_prefix + (f"{heading}\n\n" if heading else "")
        if len(section_body) <= _UNITS_CHUNK_LIMIT:
            contents.append(section_prefix + section_body)
        else:
            contents.extend(
                section_prefix + b for b in _split_oversized(section_body)
            )
    if not contents:  # 防御：body 超长但分节全空（不可能路径，保底单块）
        contents.append(page_prefix + body)
    return [(f"{page_key}#s{i}", text) for i, text in enumerate(contents, start=1)]


class WikiDB:
    """kb_wiki_pages / kb_wiki_page_links / kb_wiki_page_units 仓储
    （kb_artifact_sources 通用来源表由批次3编译管线接线，本类暂不涉）。

    构造入参为 row_factory=dict_row 的 psycopg AsyncConnectionPool。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------ 页面 CRUD

    async def count_pages(self, artifact_id: str, revision: int = 0) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT COUNT(*) AS n FROM kb_wiki_pages
                   WHERE artifact_id = %s AND revision = %s""",
                (artifact_id, revision),
            )
            return int((await cur.fetchone())["n"])  # type: ignore[index]

    async def list_pages(
        self, artifact_id: str, *, revision: int,
    ) -> list[dict[str, Any]]:
        """轻量清单（不含 body_md/frontmatter）；aliases 自 frontmatter.aliases
        派生为 list[str]（前端/阅读器链接解析兜底用）。ORDER BY ordinal,
        page_key。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT page_key, title, page_type, origin, ordinal, flags,
                          updated_at, updated_by, frontmatter
                   FROM kb_wiki_pages
                   WHERE artifact_id = %s AND revision = %s
                   ORDER BY ordinal, page_key""",
                (artifact_id, revision),
            )
            rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            frontmatter = row.pop("frontmatter", None) or {}
            aliases = frontmatter.get("aliases") \
                if isinstance(frontmatter, dict) else None
            row["aliases"] = [str(a) for a in (aliases or [])]
        return rows

    async def get_page(
        self, artifact_id: str, revision: int, page_key: str,
    ) -> dict[str, Any] | None:
        """单页全字段读取（含 body_md/frontmatter/provenance）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT page_key, title, page_type, frontmatter, body_md,
                          origin, provenance, flags, ordinal, updated_at,
                          updated_by
                   FROM kb_wiki_pages
                   WHERE artifact_id = %s AND revision = %s AND page_key = %s""",
                (artifact_id, revision, page_key),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def upsert_page(
        self, artifact_id: str, *, page_key: str, title: str,
        page_type: str = "topic", frontmatter: dict[str, Any] | None = None,
        body_md: str = "", provenance: list[Any] | None = None,
        flags: dict[str, Any] | None = None, ordinal: int = 0,
        origin: str = "manual", updated_by: str = "",
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """单事务写页（rev0=设计态工作区，revision 固定 0）：FOR UPDATE 锁 +
        expected_updated_at 全精度比对（失配 stale_artifact；None 跳过——对齐
        db_artifacts._assert_expected_updated_at 既有语义）→ UPSERT 全内容字段
        → 推进 kb_artifacts.updated_at（页写必须推进乐观锁令牌，对齐
        update_cell 的锁+touch 同事务模式）。返回完整页行。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._assert_expected_updated_at(
                    conn, artifact_id, expected_updated_at,
                )
                cur = await conn.execute(
                    """INSERT INTO kb_wiki_pages
                       (artifact_id, revision, page_key, title, page_type,
                        frontmatter, body_md, origin, provenance, flags,
                        ordinal, updated_at, updated_by)
                       VALUES (%(a)s, 0, %(k)s, %(t)s, %(pt)s, %(fm)s::jsonb,
                               %(b)s, %(o)s, %(pv)s::jsonb, %(fl)s::jsonb,
                               %(ord)s, now(), %(u)s)
                       ON CONFLICT (artifact_id, revision, page_key) DO UPDATE SET
                           title = EXCLUDED.title,
                           page_type = EXCLUDED.page_type,
                           frontmatter = EXCLUDED.frontmatter,
                           body_md = EXCLUDED.body_md,
                           origin = EXCLUDED.origin,
                           provenance = EXCLUDED.provenance,
                           flags = EXCLUDED.flags,
                           ordinal = EXCLUDED.ordinal,
                           updated_at = now(),
                           updated_by = EXCLUDED.updated_by
                       RETURNING page_key, title, page_type, frontmatter,
                                 body_md, origin, provenance, flags, ordinal,
                                 updated_at, updated_by""",
                    {
                        "a": artifact_id, "k": page_key, "t": title,
                        "pt": page_type, "fm": _json(frontmatter or {}),
                        "b": body_md, "o": origin,
                        "pv": _json(provenance or []),
                        "fl": _json(flags or {}),
                        "ord": int(ordinal), "u": updated_by,
                    },
                )
                row = dict(await cur.fetchone())  # type: ignore[arg-type]
                await self._touch_in_conn(conn, artifact_id)
                return row

    async def delete_page(
        self, artifact_id: str, page_key: str, *, expected_updated_at: Any = None,
    ) -> bool:
        """同款单事务（锁+比对+删页+touch）；页不存在返回 False。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._assert_expected_updated_at(
                    conn, artifact_id, expected_updated_at,
                )
                cur = await conn.execute(
                    """DELETE FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = 0 AND page_key = %s
                       RETURNING page_key""",
                    (artifact_id, page_key),
                )
                hit = await cur.fetchone() is not None
                if hit:
                    await self._touch_in_conn(conn, artifact_id)
                return hit

    # ------------------------------------------------------------ 链接维护

    async def replace_page_links(
        self, artifact_id: str, revision: int, from_page: str,
        edges: list[dict[str, Any]],
    ) -> None:
        """整页边替换（edges 由 compute_page_edges 算好传入；防御性只写
        from_page 匹配的边——调用方传全图边表也安全）。链接是页内容的派生
        数据，不推进乐观锁令牌（令牌随页写前进）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """DELETE FROM kb_wiki_page_links
                       WHERE artifact_id = %s AND revision = %s
                         AND from_page = %s""",
                    (artifact_id, revision, from_page),
                )
                own = [
                    e for e in edges if str(e.get("from_page")) == from_page
                ]
                for start in range(0, len(own), _LINK_INSERT_BATCH):
                    chunk = own[start:start + _LINK_INSERT_BATCH]
                    flat: list[Any] = []
                    for edge in chunk:
                        flat.extend([
                            artifact_id, revision, from_page,
                            str(edge.get("to_page")),
                            str(edge.get("anchor_text") or ""),
                            str(edge.get("status") or "broken"),
                        ])
                    await conn.execute(
                        """INSERT INTO kb_wiki_page_links
                           (artifact_id, revision, from_page, to_page,
                            anchor_text, status)
                           VALUES """
                        + ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(chunk)),
                        flat,
                    )

    async def recompute_link_statuses(
        self, artifact_id: str, revision: int,
    ) -> None:
        """该 revision 全量 links 重算 valid/broken：目标命中 page_key ∪
        aliases 集合 → valid（页删后指向它的边自动落 broken，63号 §3.1；
        目标集口径与 compute_page_edges 的解析归一同构）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """SELECT page_key, frontmatter FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = %s""",
                    (artifact_id, revision),
                )
                pages = [dict(r) for r in await cur.fetchall()]
                targets: set[str] = set()
                for page in pages:
                    targets.add(str(page["page_key"]))
                    frontmatter = page.get("frontmatter") or {}
                    if isinstance(frontmatter, dict):
                        targets.update(
                            str(a) for a in (frontmatter.get("aliases") or [])
                        )
                await conn.execute(
                    """UPDATE kb_wiki_page_links
                       SET status = CASE WHEN to_page = ANY(%(t)s)
                                         THEN 'valid' ELSE 'broken' END
                       WHERE artifact_id = %(a)s AND revision = %(r)s""",
                    {"t": sorted(targets), "a": artifact_id, "r": revision},
                )

    async def list_links(
        self, artifact_id: str, *, revision: int, page_key: str | None = None,
        direction: str = "out",
    ) -> list[dict[str, Any]]:
        """出边/入边（63号 §2.4 反向链接）：direction="out" → from_page=
        page_key；"in" → to_page=page_key；无 page_key → 该 revision 全量。
        ORDER BY from_page, to_page。"""
        if direction not in ("out", "in"):
            raise WikiError("invalid_direction")
        clause = ""
        params: list[Any] = [artifact_id, revision]
        if page_key is not None:
            clause = (" AND from_page = %s" if direction == "out"
                      else " AND to_page = %s")
            params.append(page_key)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT from_page, to_page, anchor_text, status
                   FROM kb_wiki_page_links
                   WHERE artifact_id = %s AND revision = %s""" + clause +
                " ORDER BY from_page, to_page",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]

    # ---------------------------------------------------------------- diff

    async def diff_draft_vs_current(self, artifact_id: str) -> dict[str, Any]:
        """rev0 pages vs current_revision pages 按 page_key 对齐。返回六平键
        （键名与 db_artifacts.diff_draft_vs_current 完全同名同构）：added/
        removed=page_key 列表；changed=title/body_md/frontmatter/page_type/
        origin 任一不同（origin 翻 manual 也是真实待发布变更），元素形态
        {page_key, fields:[变更字段名]}；counts 恒全量、样例各截前 100 条。
        干净态（draft_open=FALSE 且 current>0）零差异投影；从未发布
        （current=0）→ current 侧空集、全部计新增。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT current_revision, draft_open FROM kb_artifacts"
                " WHERE artifact_id = %s",
                (artifact_id,),
            )
            art = await cur.fetchone()
            if art is not None and not art["draft_open"] \
                    and int(art["current_revision"]) > 0:
                return self._empty_diff()
            current_rev = int(art["current_revision"]) if art else 0
            cur = await conn.execute(
                """SELECT page_key, title, body_md, frontmatter, page_type,
                          origin
                   FROM kb_wiki_pages
                   WHERE artifact_id = %s AND revision = 0
                   ORDER BY page_key""",
                (artifact_id,),
            )
            draft = {str(r["page_key"]): dict(r) for r in await cur.fetchall()}
            if current_rev > 0:
                cur = await conn.execute(
                    """SELECT page_key, title, body_md, frontmatter, page_type,
                              origin
                       FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = %s
                       ORDER BY page_key""",
                    (artifact_id, current_rev),
                )
                current = {
                    str(r["page_key"]): dict(r) for r in await cur.fetchall()
                }
            else:
                current = {}
        added = [k for k in draft if k not in current]
        removed = [k for k in current if k not in draft]
        changed: list[dict[str, Any]] = []
        for key in draft:
            if key not in current:
                continue
            fields = [
                f for f in _DIFF_FIELDS
                if draft[key].get(f) != current[key].get(f)
            ]
            if fields:
                changed.append({"page_key": key, "fields": fields})
        return {
            "added": added[:_DIFF_SAMPLE_LIMIT],
            "removed": removed[:_DIFF_SAMPLE_LIMIT],
            "changed": changed[:_DIFF_SAMPLE_LIMIT],
            "added_count": len(added), "removed_count": len(removed),
            "changed_count": len(changed),
        }

    @staticmethod
    def _empty_diff() -> dict[str, Any]:
        return {
            "added": [], "removed": [], "changed": [],
            "added_count": 0, "removed_count": 0, "changed_count": 0,
        }

    # ------------------------------------------------------------ units 投影

    async def project_units(self, artifact_id: str, revision: int) -> int:
        """发布后置投影（63号 §5.1，幂等）：先 DELETE 该 (artifact_id,
        revision) 既有 units 再逐页 chunk_page_for_units INSERT
        （embed_status='pending'）。返回单元数。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kb_wiki_page_units"
                    " WHERE artifact_id = %s AND revision = %s",
                    (artifact_id, revision),
                )
                cur = await conn.execute(
                    """SELECT page_key, title, body_md FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = %s
                       ORDER BY ordinal, page_key""",
                    (artifact_id, revision),
                )
                pages = [dict(r) for r in await cur.fetchall()]
                count = 0
                for page in pages:
                    chunks = chunk_page_for_units(
                        str(page["title"]), str(page["body_md"] or ""),
                        str(page["page_key"]),
                    )
                    for unit_key, content_text in chunks:
                        await conn.execute(
                            """INSERT INTO kb_wiki_page_units
                               (artifact_id, revision, unit_key, page_key,
                                content_text, embed_status)
                               VALUES (%s, %s, %s, %s, %s, 'pending')""",
                            (artifact_id, revision, unit_key,
                             str(page["page_key"]), content_text),
                        )
                        count += 1
                return count

    async def units_status(
        self, artifact_id: str, revision: int,
    ) -> dict[str, int]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT embed_status, COUNT(*) AS n FROM kb_wiki_page_units
                   WHERE artifact_id = %s AND revision = %s
                   GROUP BY embed_status""",
                (artifact_id, revision),
            )
            counts = {
                str(r["embed_status"]): int(r["n"])
                for r in await cur.fetchall()
            }
        return {
            "total": sum(counts.values()),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
        }

    async def list_pending_units(
        self, artifact_id: str, revision: int, limit: int = 32,
    ) -> list[dict[str, Any]]:
        """待嵌入单元（ORDER BY unit_key——幂等键内容哈希的稳定序）。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT unit_key, page_key, content_text
                   FROM kb_wiki_page_units
                   WHERE artifact_id = %s AND revision = %s
                     AND embed_status = 'pending'
                   ORDER BY unit_key LIMIT %s""",
                (artifact_id, revision, max(1, int(limit))),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def update_unit_embeddings(
        self, artifact_id: str, revision: int, unit_keys: list[str],
        vectors: list[list[float]],
    ) -> None:
        """写 embedding + embed_status='done'。vector 走文本字面量绑定
        （%s::vector，见 _vec_literal 注释——psycopg 无 pgvector 适配器）。"""
        if len(unit_keys) != len(vectors):
            raise WikiError("invalid_embedding_batch")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                for unit_key, vector in zip(unit_keys, vectors):
                    await conn.execute(
                        """UPDATE kb_wiki_page_units
                           SET embedding = %s::vector, embed_status = 'done'
                           WHERE artifact_id = %s AND revision = %s
                             AND unit_key = %s""",
                        (_vec_literal(vector), artifact_id, revision, unit_key),
                    )

    async def mark_units_failed(
        self, artifact_id: str, revision: int, unit_keys: list[str],
    ) -> None:
        """终态失败标记（failed 不自动重试；瞬时失败保持 pending 由下次
        发布/status 自愈重试）。"""
        if not unit_keys:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                """UPDATE kb_wiki_page_units
                   SET embed_status = 'failed'
                   WHERE artifact_id = %s AND revision = %s
                     AND unit_key = ANY(%s)""",
                (artifact_id, revision, list(unit_keys)),
            )

    # ------------------------------------------------------------ 生命周期

    async def publish(
        self, artifact_id: str, *, expected_updated_at: Any = None,
        change_summary: str = "", published_by: str = "",
    ) -> dict[str, Any]:
        """wiki 发布事务（63号 §三，镜像 db_artifacts.publish 逐步骤同构）：
        ①FOR UPDATE+乐观锁全精度比对 ②门禁（status / usage_note / draft
        干净态 / rev0 页数=0→wiki_no_pages）③new_rev=GREATEST(current, 历史
        revisions, table/wiki 专有表正版本最大值)+1（回滚拨低指针后重发布不撞
        历史版本行）④pages+links rev0→revN INSERT..SELECT 复制定格 ⑤revisions
        行（schema_json=页面清单 JSON，toc/glossary 批次3编译填充）⑥清 rev0
        ⑦拨指针。返回 {"revision", "page_count"}。units 投影是事务外后置
        （service 层 WikiLifecycle 调 project_units——投影失败不回滚发布）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise WikiError("artifact_not_found")
                if expected_updated_at is not None \
                        and art["updated_at"] != expected_updated_at:
                    raise WikiError("stale_artifact")
                if art["status"] not in ("draft", "published", "deprecated"):
                    raise WikiError("invalid_status")
                if not (art["usage_note"] or "").strip():
                    raise WikiError("usage_note_required")
                if not art["draft_open"] and int(art["current_revision"]) > 0:
                    raise WikiError("no_pending_changes")
                cur = await conn.execute(
                    """SELECT COUNT(*) AS n FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = 0""",
                    (artifact_id,),
                )
                if int((await cur.fetchone())["n"]) == 0:  # type: ignore[index]
                    raise WikiError("wiki_no_pages")
                cur = await conn.execute(
                    """SELECT GREATEST(
                           %(cur)s,
                           COALESCE((SELECT MAX(revision) FROM kb_artifact_revisions
                                     WHERE artifact_id = %(a)s), 0),
                           COALESCE((SELECT MAX(revision) FROM kb_table_rows
                                     WHERE artifact_id = %(a)s AND revision > 0), 0),
                           COALESCE((SELECT MAX(revision) FROM kb_wiki_pages
                                     WHERE artifact_id = %(a)s AND revision > 0), 0)
                       ) + 1 AS new_rev""",
                    {"cur": art["current_revision"], "a": artifact_id},
                )
                new_rev = int((await cur.fetchone())["new_rev"])  # type: ignore[index]
                cur = await conn.execute(
                    """SELECT page_key, title, page_type, ordinal, origin
                       FROM kb_wiki_pages
                       WHERE artifact_id = %s AND revision = 0
                       ORDER BY ordinal, page_key""",
                    (artifact_id,),
                )
                manifest = [dict(r) for r in await cur.fetchall()]
                cur = await conn.execute(
                    """INSERT INTO kb_wiki_pages
                       (artifact_id, revision, page_key, title, page_type,
                        frontmatter, body_md, origin, provenance, flags,
                        ordinal, updated_at, updated_by)
                       SELECT artifact_id, %(r)s, page_key, title, page_type,
                              frontmatter, body_md, origin, provenance, flags,
                              ordinal, now(), %(by)s
                       FROM kb_wiki_pages
                       WHERE artifact_id = %(a)s AND revision = 0""",
                    {"r": new_rev, "by": published_by, "a": artifact_id},
                )
                # INSERT..SELECT 无 RETURNING：rowcount 才是行数（fetchval 拿不到）
                page_count = int(cur.rowcount or 0)
                await conn.execute(
                    """INSERT INTO kb_wiki_page_links
                       (artifact_id, revision, from_page, to_page, anchor_text,
                        status)
                       SELECT artifact_id, %(r)s, from_page, to_page,
                              anchor_text, status
                       FROM kb_wiki_page_links
                       WHERE artifact_id = %(a)s AND revision = 0""",
                    {"r": new_rev, "a": artifact_id},
                )
                await conn.execute(
                    """INSERT INTO kb_artifact_revisions
                       (artifact_id, revision, schema_json, row_count,
                        change_summary, published_by)
                       VALUES (%s, %s, %s::jsonb, %s, %s, %s)""",
                    (artifact_id, new_rev,
                     _json({"pages": manifest, "toc": None, "glossary": []}),
                     page_count, change_summary, published_by),
                )
                await conn.execute(
                    "DELETE FROM kb_wiki_pages"
                    " WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_wiki_page_links"
                    " WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                await conn.execute(
                    """UPDATE kb_artifacts
                       SET current_revision = %s, status = 'published',
                           draft_open = FALSE, updated_at = now()
                       WHERE artifact_id = %s""",
                    (new_rev, artifact_id),
                )
        return {"revision": new_rev, "page_count": page_count}

    async def rollback_to(
        self, artifact_id: str, revision: int, *, expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """纯指针拨回（镜像 db_artifacts.rollback_to：数据不动）——不碰
        pages/links/units（units 按版本保留，回滚后检索直接用目标版 units）。
        rev0 清空与 checkout 只属于 discard_draft/ensure_draft。仅校验目标版本
        存在于 kb_artifact_revisions——拨任何历史版（含 > current）都安全。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise WikiError("artifact_not_found")
                if expected_updated_at is not None \
                        and art["updated_at"] != expected_updated_at:
                    raise WikiError("stale_artifact")
                revision = int(revision)
                if revision < 1:
                    raise WikiError("invalid_revision")
                cur = await conn.execute(
                    "SELECT 1 FROM kb_artifact_revisions"
                    " WHERE artifact_id = %s AND revision = %s",
                    (artifact_id, revision),
                )
                if await cur.fetchone() is None:
                    raise WikiError("invalid_revision")
                cur = await conn.execute(
                    """UPDATE kb_artifacts
                       SET current_revision = %s, status = 'published',
                           updated_at = now()
                       WHERE artifact_id = %s
                       RETURNING artifact_id, domain_id, kind, title, description,
                                 usage_note, owner_user_id, status,
                                 current_revision, draft_open, created_at,
                                 updated_at""",
                    (revision, artifact_id),
                )
                return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def discard_draft(
        self, artifact_id: str, *, expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """丢弃 draft：DELETE rev0 pages+links + draft_open=FALSE + touch
        （乱改后的逃生门；镜像 db_artifacts.discard_draft 的锁/校验/touch——
        已发布快照不动）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._assert_expected_updated_at(
                    conn, artifact_id, expected_updated_at,
                )
                await conn.execute(
                    "DELETE FROM kb_wiki_pages"
                    " WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                await conn.execute(
                    "DELETE FROM kb_wiki_page_links"
                    " WHERE artifact_id = %s AND revision = 0",
                    (artifact_id,),
                )
                cur = await conn.execute(
                    """UPDATE kb_artifacts
                       SET draft_open = FALSE, updated_at = now()
                       WHERE artifact_id = %s
                       RETURNING artifact_id, domain_id, kind, title, description,
                                 usage_note, owner_user_id, status,
                                 current_revision, draft_open, created_at,
                                 updated_at""",
                    (artifact_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise WikiError("artifact_not_found")
                return dict(row)

    async def ensure_draft(self, artifact_id: str) -> dict[str, Any]:
        """设计态编辑统一入口（镜像 db_artifacts.ensure_draft）：draft_open=
        FALSE 且 current>0 → checkout（rev0 清空后 current→rev0 复制
        pages+links）再置 TRUE；FALSE 且 current=0（从未发布）直接置 TRUE
        （rev0 即工作区）。幂等；checkout **不推进 updated_at**——rev0 是
        current 的逐字节副本，用户可见内容未变（乐观锁令牌只随真实内容变更
        前进）。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT * FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
                    (artifact_id,),
                )
                art = await cur.fetchone()
                if art is None:
                    raise WikiError("artifact_not_found")
                if not art["draft_open"]:
                    if int(art["current_revision"]) > 0:
                        # checkout 前清 rev0（防御脏残留——INSERT..SELECT 撞主键）
                        await conn.execute(
                            "DELETE FROM kb_wiki_pages"
                            " WHERE artifact_id = %s AND revision = 0",
                            (artifact_id,),
                        )
                        await conn.execute(
                            "DELETE FROM kb_wiki_page_links"
                            " WHERE artifact_id = %s AND revision = 0",
                            (artifact_id,),
                        )
                        await conn.execute(
                            """INSERT INTO kb_wiki_pages
                               (artifact_id, revision, page_key, title,
                                page_type, frontmatter, body_md, origin,
                                provenance, flags, ordinal, updated_at,
                                updated_by)
                               SELECT artifact_id, 0, page_key, title,
                                      page_type, frontmatter, body_md, origin,
                                      provenance, flags, ordinal, now(), ''
                               FROM kb_wiki_pages
                               WHERE artifact_id = %s AND revision = %s""",
                            (artifact_id, art["current_revision"]),
                        )
                        await conn.execute(
                            """INSERT INTO kb_wiki_page_links
                               (artifact_id, revision, from_page, to_page,
                                anchor_text, status)
                               SELECT artifact_id, 0, from_page, to_page,
                                      anchor_text, status
                               FROM kb_wiki_page_links
                               WHERE artifact_id = %s AND revision = %s""",
                            (artifact_id, art["current_revision"]),
                        )
                    cur = await conn.execute(
                        """UPDATE kb_artifacts
                           SET draft_open = TRUE
                           WHERE artifact_id = %s
                           RETURNING artifact_id, domain_id, kind, title,
                                     description, usage_note, owner_user_id,
                                     status, current_revision, draft_open,
                                     created_at, updated_at""",
                        (artifact_id,),
                    )
                    art = await cur.fetchone()
                return dict(art)  # type: ignore[arg-type]

    # ------------------------------------------------------------ 内部共用

    async def _assert_expected_updated_at(
        self, conn: Any, artifact_id: str, expected_updated_at: Any,
    ) -> None:
        """写路径乐观锁权威校验（口径同 db_artifacts 同名方法）：FOR UPDATE
        锁 kb_artifacts 行 → 全精度比对 updated_at（PG TIMESTAMPTZ 微秒）→
        失配 stale_artifact。expected_updated_at 为 None 跳过（校验是否前置
        由 service 决定）。"""
        if expected_updated_at is None:
            return
        cur = await conn.execute(
            "SELECT updated_at FROM kb_artifacts WHERE artifact_id = %s FOR UPDATE",
            (artifact_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise WikiError("artifact_not_found")
        if row["updated_at"] != expected_updated_at:
            raise WikiError("stale_artifact")

    @staticmethod
    async def _touch_in_conn(conn: Any, artifact_id: str) -> None:
        await conn.execute(
            "UPDATE kb_artifacts SET updated_at = now() WHERE artifact_id = %s",
            (artifact_id,),
        )
