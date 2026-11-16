# -*- coding: utf-8 -*-
"""KB 知识制品工作台来源适配器（67号 MVP Task 5）—— kb_dir 文档底表 /
Excel 冻结解析 / materialize 统一取数。

三个入口（前两个由 service 层在「接入来源」时调用，产物冻结进
kb_workspace_sources.input_manifest；第三个由执行器取数）：

- ``read_kb_dir(conn, *, kb_id, directories, user)`` → ``(dataset, manifest)``：
  KB 目录 → 文档底表（cols=``[知识库,目录,文件,文档内容_JSON]``，一行一文档）。
- ``parse_excel(content, *, file_name, sheet_name)`` → ``(dataset, manifest)``：
  Excel 表 → 数据集（复用 artifact_import_service 四件套私有函数，零复制）。
- ``materialize_source(source_row)`` → dataset：两类连接器同构——都从
  ``input_manifest["dataset"]`` 取冻结数据。

关键决策（docstring 即决策记录）：

1. **权限**：无权 → :class:`WorkspacePermissionError`（工作台族新异常，
   code=``workspace_permission``；不复用 kb_service.Forbidden——那是 kb 服务
   层 HTTP 翻译族，工作台错误族统一走 WorkspaceError 供 Task 8 映射）。
   谓词本身零重写：经 :class:`_SingleConnPool` 适配器真调
   ``KbDB.is_visible``（谓词唯一真相源 = kb/db.py——admin 全通/域绑定+域内
   public/owner/成员分支一个不少）。kb 不存在（或非 active）→
   WorkspaceNotFoundError。
2. **解析结构读取路径**：镜像 ``ParseResultReadService`` 的取数链（快照
   仓储 ``latest_for_document`` 的 SQL 口径）——文档「当前内容对象+修订」锚
   定的最近 READY 且有指纹快照（asset_document_snapshot_links ⋈
   asset_document_snapshots），无此快照 = 无解析结果 → manifest 的
   ``unparsed_docs`` 显式记录（不产出行、不静默跳过）。L1/L2 章节结构读
   ``asset_raw_segments``（Parse IR 的 DB 投影：``section_path`` 即编译器的
   heading_chain，heading 段含自身）并**按链前缀重建**（带正文的标题不产
   独立 heading 段——见 _build_sections）；L3+ 深标题不单列——其文本折叠
   进最近 L2 节（不丢内容）；首个 L1/L2 前的前导段落归入 ``title=""`` 的
   L1 伪节。
3. **冻结语义（对 67号 §7.1 前半句的显式偏离）**：MVP = 接入即冻数据——
   ``read_kb_dir`` 返回的 dataset 由 manifest["dataset"] 内嵌随 sources 行
   落库，**执行不重读 KB**（撤权后已接入的执行不会失败）；撤权检查移至
   导出门禁（Task 9：含 kb_dir 血缘的导出 item 重验可见性）。「用新资料
   重跑」= 重新 PUT source 触发重读（WB-07 批次推进）。
4. **文档内容_JSON 存 canonical JSON 字符串**（ensure_ascii=False 紧凑
   分隔符）：dict 不是 Task 4 六列型的合法列值；字符串可被下游
   select/llm_extract/导出按 text 列消费，``json.loads`` 还原。
5. **Excel cast 失败留原值**（datetime/date 归一 ISO，其余原样）：不记
   行列级 quality 账（MVP 简化，docstring 记录）；类型推断对全量数据行
   采样（artifact_import 预览只抽前 200 行，冻结解析以全量为准——同一
   列在两个入口可能得出不同推断，属预期）。

护栏：文档上限 ``MAX_DOCS``=200（超限 WorkspaceValidationError 带实际
数量）；数据集经 workspace_dataset.validate_dataset（5 万行）+ manifest
序列化 ≤ MAX_SERIALIZED_BYTES（20MB）双重把关。
"""
from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from typing import Any, AsyncIterator

from openpyxl import load_workbook

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.db_workspaces import (
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceValidationError,
)
from knowledge_mining.mining.kb.services.artifact_import_service import (
    ArtifactImportError,
    _cast_value,
    _check_zip_bomb,
    _dedupe_headers,
    _infer_column_type,
)
from knowledge_mining.mining.kb.workspace_dataset import (
    MAX_SERIALIZED_BYTES,
    validate_dataset,
)

__all__ = [
    "MAX_DOCS",
    "WorkspacePermissionError",
    "materialize_source",
    "parse_excel",
    "read_kb_dir",
]

#: kb_dir 单次接入的文档数上限（超限整批拒绝，防巨型底表冲垮冻结预算）。
MAX_DOCS = 200

#: 文档底表固定列（67号 §4.1 B.3 口径）。
_DOC_COLS = ["知识库", "目录", "文件", "文档内容_JSON"]

#: L1/L2 章节结构取的最大标题层级（更深层折叠为正文）。
_MAX_SECTION_LEVEL = 2


class WorkspacePermissionError(WorkspaceError):
    """kb_dir 接入者对目标 KB 无读权限（决策 1：工作台族异常，
    service 层可反探测翻 404/403）。"""

    code = "workspace_permission"


class _SingleConnPool:
    """把既有连接伪装成 pool.connection() 上下文——让 KbDB.is_visible 在
    调用方连接上原样执行（ACL 谓词零复制；连接上无并发借还）。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        yield self._conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_dirs(directories: Any) -> list[str]:
    """scope 目录归一：strip / 去空 / 去重排序（口径同
    CompileInputsDB._norm_dirs——幂等查询计划稳定）。"""
    if directories is None:
        return []
    if not isinstance(directories, list):
        raise WorkspaceValidationError("directories must be a list")
    cleaned: set[str] = set()
    for d in directories:
        if not isinstance(d, str):
            raise WorkspaceValidationError(
                f"directory entries must be strings: {d!r}")
        text = d.strip()
        if text:
            cleaned.add(text)
    return sorted(cleaned)


def _ensure_budget(payload: dict[str, Any]) -> None:
    """冻结产物序列化预算：manifest（含内嵌 dataset）≤ 20MB；超限错误带
    实际字节数（Task 6 质量审查 LOW：无字节数无法判断超了多少）。"""
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    size = len(encoded.encode("utf-8"))
    if size > MAX_SERIALIZED_BYTES:
        raise WorkspaceValidationError(
            "frozen manifest exceeds serialization budget"
            f" {MAX_SERIALIZED_BYTES} bytes: actual {size} bytes")


# ------------------------------------------------------------ 文档底表


def _heading_level(section_path: list[dict[str, Any]]) -> int:
    """heading 段层级：section_path 最内层 level（IR heading_chain 镜像）；
    脏数据回落链长，再兜底 1。"""
    if section_path:
        raw = section_path[-1].get("level")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
        return max(len(section_path), 1)
    return 1


def _parse_path(raw: Any) -> list[dict[str, Any]]:
    """section_path 列（TEXT 存 JSON 数组）→ [{level,title}]；脏值空链兜底。"""
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [p for p in parsed if isinstance(p, dict)] if \
            isinstance(parsed, list) else []
    return []


def _chain_l12(section_path: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """section_path → L1/L2 前缀链 [(level,title)]（按文档序；更深层丢弃）。"""
    out: list[tuple[int, str]] = []
    for node in section_path:
        raw = node.get("level")
        level = raw if isinstance(raw, int) and not isinstance(raw, bool) \
            and raw > 0 else 1
        if level <= _MAX_SECTION_LEVEL:
            out.append((level, str(node.get("title") or "")))
    return out


def _build_sections(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L1/L2 章节结构（快照内阅读序段行 → sections）。

    **按 section_path 链前缀重建**，不数 heading 段——真实编译语义里带正文
    的标题不产独立 heading 段（标题并入正文段的文本前缀，链才含结构），
    只有「纯标题节」（紧跟下一标题）才有 block_type='heading' 行；链是唯一
    可靠的结构源。规则：

    - 每段取其链的 L1/L2 前缀，与当前开节栈逐位对齐：失配的深层节关闭、
      新出现的前缀开节（同级同名兄弟并入首见节——确定性）；
    - heading 段（最内层 ≤ L2）自身文本不重复进 text（title 已携带）；
    - 最内层 > L2 的 heading 段与其余正文段 raw_text 依序并入当前最内
      开节的 text（不丢内容）；
    - 首个 L1/L2 前的前导内容 → ``{level:1, title:"", text}`` 伪节；
    - 标题并入正文前缀的段（编译器 _emit_merged 行为）text 含标题文本——
      保留不去重；
    - text 以 ``\\n`` 连接。
    """
    sections: list[dict[str, Any]] = []   # 文档序全量节（含已关闭）
    stack: list[dict[str, Any]] = []      # 当前开着的 L1/L2 链
    leading: list[str] = []
    for seg in segments:
        path = _parse_path(seg["section_path"])
        l12 = _chain_l12(path)
        matched = 0
        while matched < len(stack) and matched < len(l12) and (
                stack[matched]["level"], stack[matched]["title"]) \
                == l12[matched]:
            matched += 1
        del stack[matched:]  # 关闭失配深层节（其 parts 已封存于 sections）
        for level, title in l12[len(stack):]:
            section = {"level": level, "title": title, "_parts": []}
            sections.append(section)
            stack.append(section)
        if seg["block_type"] == "heading":
            if _heading_level(path) > _MAX_SECTION_LEVEL:
                target = stack[-1]["_parts"] if stack else leading
                target.append(str(seg["raw_text"]))
            continue  # ≤L2 的标题文本由节 title 携带，不重复进 text
        target = stack[-1]["_parts"] if stack else leading
        target.append(str(seg["raw_text"]))
    out: list[dict[str, Any]] = []
    if leading:
        out.append({"level": 1, "title": "", "text": "\n".join(leading)})
    out.extend({"level": s["level"], "title": s["title"],
                "text": "\n".join(s["_parts"])} for s in sections)
    return out


#: scope 目录过滤谓词（口径镜像 db_compile_inputs._DIR_FILTER：精确命中或
#: 子目录前缀 strpos 判定——无 LIKE 元字符转义面；directory_path 为 NULL
#: 的存量行在指定 scope 时被保守排除——无法证明在 scope 内）。
_DIR_FILTER = """(
        d.directory_path = ANY(%(dirs)s::text[])
        OR EXISTS (SELECT 1 FROM unnest(%(dirs)s::text[]) AS p(dir)
                   WHERE strpos(d.directory_path, dir || '/') = 1)
    )"""

#: 文档「当前内容版本最新解析快照」锚定口径（镜像 PgSnapshotRepository.
#: latest_for_document：READY + 有指纹 + link 锚定当前对象与修订；
#: DISTINCT ON + linked_at/id 双 tiebreak；文档自身再 JOIN 一道保证
#: object/revision 与 asset_documents 当前行一致）。
_LATEST_SNAPSHOT_SQL = """
SELECT DISTINCT ON (d.id)
       d.id AS document_id, s.id AS snapshot_id
FROM asset_documents d
JOIN asset_document_snapshot_links l
  ON l.document_id = d.id
 AND l.source_storage_object_id = d.storage_object_id
 AND l.source_content_revision = d.content_revision
JOIN asset_document_snapshots s
  ON s.id = l.document_snapshot_id
 AND s.domain = %(dom)s
 AND s.lifecycle_status = 'READY'
 AND s.snapshot_fingerprint IS NOT NULL
WHERE d.id = ANY(%(ids)s)
ORDER BY d.id, l.linked_at DESC, s.id DESC"""


async def read_kb_dir(
    conn: Any, *, kb_id: str, directories: list[str], user: dict,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """KB 目录 → 文档底表数据集 + 冻结 manifest（决策与口径见模块 docstring）。

    - directories 空数组 = 全库；指定则精确命中或子目录前缀。
    - 每文档一行：``_id=document_id``、
      ``_source=f"{kb名}/{目录或'/'}/{文件名}"``、文档内容_JSON=canonical
      JSON 字符串（document_id/title/sections）。
    - manifest=``{frozen_at, doc_count, unparsed_docs, documents,
      dataset}``；documents 带内容修订与快照 id（冻结身份账）。
    - 本函数只读不写：落 kb_workspace_sources.input_manifest 归 service
      层（决策 3 冻结语义）。
    """
    if not isinstance(kb_id, str) or not kb_id:
        raise WorkspaceValidationError("kb_id must be a non-empty string")
    user_id = str((user or {}).get("id") or "")
    if not user_id:
        raise WorkspaceValidationError("user id is required")
    dirs = _norm_dirs(directories)

    cur = await conn.execute(
        "SELECT id, name, domain FROM knowledge_bases"
        " WHERE id = %s AND status = 'active'",
        (kb_id,),
    )
    kb_row = await cur.fetchone()
    if kb_row is None:
        raise WorkspaceNotFoundError(f"kb not found: {kb_id}")
    kb_name = str(kb_row["name"])

    if not await KbDB(_SingleConnPool(conn)).is_visible(
            kb_id=kb_id, user_id=user_id):
        raise WorkspacePermissionError(f"kb not readable: {kb_id}")

    doc_where = "d.kb_id = %(kb)s AND d.deleted_at IS NULL"
    doc_params: dict[str, Any] = {"kb": kb_id}
    if dirs:
        doc_where += f" AND {_DIR_FILTER}"
        doc_params["dirs"] = dirs
    cur = await conn.execute(
        f"SELECT COUNT(*) AS n FROM asset_documents d WHERE {doc_where}",
        doc_params,
    )
    doc_total = int((await cur.fetchone())["n"])
    if doc_total > MAX_DOCS:
        raise WorkspaceValidationError(
            f"documents in scope exceed limit {MAX_DOCS}: {doc_total}")
    cur = await conn.execute(
        f"""SELECT d.id, d.document_key, d.document_name, d.directory_path,
                   d.content_revision
            FROM asset_documents d
            WHERE {doc_where}
            ORDER BY d.document_key""",
        doc_params,
    )
    docs = [dict(r) for r in await cur.fetchall()]

    snap_of: dict[str, str] = {}
    if docs:
        cur = await conn.execute(
            _LATEST_SNAPSHOT_SQL,
            {"ids": [str(d["id"]) for d in docs], "dom": kb_row["domain"]},
        )
        snap_of = {str(r["document_id"]): str(r["snapshot_id"])
                   for r in await cur.fetchall()}

    seg_by_snap: dict[str, list[dict[str, Any]]] = {}
    if snap_of:
        cur = await conn.execute(
            """SELECT document_snapshot_id, segment_index, block_type,
                      raw_text, section_path, section_title
               FROM asset_raw_segments
               WHERE document_snapshot_id = ANY(%(snaps)s)
               ORDER BY document_snapshot_id, segment_index""",
            {"snaps": sorted(set(snap_of.values()))},
        )
        for r in await cur.fetchall():
            seg_by_snap.setdefault(
                str(r["document_snapshot_id"]), []).append(dict(r))

    rows: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    for doc in docs:
        name = str(doc["document_name"] or doc["document_key"])
        snapshot_id = snap_of.get(str(doc["id"]))
        if snapshot_id is None:
            unparsed.append({"document_id": str(doc["id"]), "title": name})
            continue
        directory = str(doc["directory_path"] or "")
        body = {
            "document_id": str(doc["id"]),
            "title": name,
            "sections": _build_sections(seg_by_snap.get(snapshot_id, [])),
        }
        rows.append({
            "_id": str(doc["id"]),
            "_source": f"{kb_name}/{directory or '/'}/{name}",
            "知识库": kb_name,
            "目录": directory,
            "文件": name,
            "文档内容_JSON": json.dumps(
                body, ensure_ascii=False, separators=(",", ":")),
        })
        documents.append({
            "document_id": str(doc["id"]),
            "title": name,
            "content_revision": int(doc["content_revision"] or 0),
            "snapshot_id": snapshot_id,
        })

    dataset = {"kind": "table", "cols": list(_DOC_COLS), "rows": rows}
    validate_dataset(dataset)
    manifest = {
        "frozen_at": _utcnow(),
        "doc_count": len(rows),
        "unparsed_docs": unparsed,
        "documents": documents,
        "dataset": dataset,
    }
    _ensure_budget(manifest)
    return dataset, manifest


# ------------------------------------------------------------ Excel


def _fallback_value(raw: Any) -> Any:
    """cast 失败的原值兜底（决策 5）：datetime/date/time 归一 ISO（json
    可序列化——time 值可过本预算闸（default=str）但 Task 6 起执行器侧的
    canonical 序列化（无 default）会爆 TypeError，必须在此归一），其余
    原样。"""
    if isinstance(raw, (datetime, date, time)):
        return raw.isoformat()
    return raw


def parse_excel(
    content: bytes, *, file_name: str, sheet_name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Excel 字节 → 数据集 + 冻结 manifest（护栏/表头清洗/类型推断/cast 全
    复用 artifact_import_service 私有四件套，零实现复制）。

    - sheet_name None=第一张表；不在 sheetnames → WorkspaceValidationError。
    - 空表（无表头或 0 数据行）→ WorkspaceValidationError。
    - 每数据行：``_id=f"row_{行号:x}"``（无主键场景序号即身份）、
      ``_source=f"{file_name}/{sheet标题}"``；列值按全量推断类型 cast，
      失败留原值（决策 5）。
    - manifest=``{file_name, sheet_name, row_count, frozen_at, dataset}``——
      整份数据集冻结进 manifest（重跑不重读文件；重传=更新来源）。
    """
    if not isinstance(content, (bytes, bytearray)):
        raise WorkspaceValidationError("excel content must be bytes")
    content = bytes(content)
    try:
        _check_zip_bomb(content)
    except ArtifactImportError as exc:
        raise WorkspaceValidationError(
            f"excel_rejected: {exc.code}") from exc
    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=False)
    except Exception as exc:  # openpyxl 暴露多种格式私有异常，统一收敛
        raise WorkspaceValidationError("invalid_xlsx") from exc
    try:
        names = list(workbook.sheetnames)
        if not names:
            raise WorkspaceValidationError("empty_sheet")
        target = sheet_name if sheet_name is not None else names[0]
        if target not in names:
            raise WorkspaceValidationError(f"unknown_sheet: {target}")
        sheet_title = str(workbook[target].title)
        iterator = workbook[target].iter_rows(values_only=True)
        header_row = next(iterator, None)
        if header_row is None:
            raise WorkspaceValidationError("empty_sheet")
        headers = _dedupe_headers(tuple(header_row))
        data_rows = list(iterator)
        if not data_rows:
            raise WorkspaceValidationError("empty_sheet")

        column_types = [
            _infer_column_type([row[i] if i < len(row) else None
                                for row in data_rows])
            for i in range(len(headers))
        ]
        rows: list[dict[str, Any]] = []
        for ordinal, row in enumerate(data_rows, start=1):
            entry: dict[str, Any] = {
                "_id": f"row_{ordinal:x}",
                "_source": f"{file_name}/{sheet_title}",
            }
            for i, name in enumerate(headers):
                raw = row[i] if i < len(row) else None
                ok, value = _cast_value(raw, column_types[i][0])
                entry[name] = value if ok else _fallback_value(raw)
            rows.append(entry)
    finally:
        workbook.close()

    dataset = {"kind": "table", "cols": headers, "rows": rows}
    validate_dataset(dataset)
    manifest = {
        "file_name": file_name,
        "sheet_name": sheet_title,
        "row_count": len(rows),
        "frozen_at": _utcnow(),
        "dataset": dataset,
    }
    _ensure_budget(manifest)
    return dataset, manifest


# ------------------------------------------------------------ 统一取数


def materialize_source(source_row: dict) -> dict[str, Any]:
    """sources 行 → 冻结数据集（两类连接器同构：input_manifest.dataset）。

    缺 dataset 键（旧形态/脏数据）→ WorkspaceValidationError（提示重新接入
    来源重新冻结——执行器不做静默降级）。
    """
    manifest = (source_row or {}).get("input_manifest")
    if not isinstance(manifest, dict) \
            or not isinstance(manifest.get("dataset"), dict):
        raise WorkspaceValidationError(
            "input_manifest has no frozen dataset：请重新接入来源以重新冻结")
    dataset = manifest["dataset"]
    validate_dataset(dataset)
    return dataset
