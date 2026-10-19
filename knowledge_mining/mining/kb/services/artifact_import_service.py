"""ArtifactImportService — Excel 三步导入向导（61号 §六 建表/追加两模式）。

三入口（本服务不直接写业务表——落库动作一律经 ArtifactService 的权限门禁）：
- parse_upload(file_bytes, file_name)：护栏（50MB 文件/解压 200MB/200 列/
  10 万行）→ 临时文件 + 进程内预览登记（TTL 1h 惰性清理）。
- build_mapping(...)：纯计算不落库——列映射校验（source 在表头/目标列存在/
  列类型合法）+ missing_columns 提示（追加模式 schema 有文件无）。
- execute_import(service, ...)：权限与 ensure_draft 前置 → 全量校验（公式
  注入（codex三审 P2-3：只查所选 sheet 的表头+数据值）+ 类型 cast + 文件内
  主键重复；strict/block 策略下先报错、零写入）→ create_batch（provenance
  需要 batch_id，先于行写入）→ 建表 replace_columns / 追加新列 upsert_column
  → insert_rows 主键 UPSERT。

预览登记为进程内 dict：mining API 单进程单 worker 部署成立；多副本部署需
换共享存储（61号 MVP 边界，见 §13 演进预留）。登记绑定 artifact_id+uploader
（对抗审查修复⑬——token 跨制品/跨用户不可用，失配 404）；每 uploader 活跃
登记 ≤5（修复⑭，超限清最旧 + 429）；启动无清扫要求（MVP，TTL 惰性清理足够）。

公式注入防御与 55号 user_import_service 同口径：以 ``= + - @`` 开头的文本
格一律拒绝（data_only=False 下公式格本身就是 "=..." 字符串）。codex三审
P2-3：拒绝只在 execute_import 重读**所选 sheet** 时执行——parse 预览只读
展示合法（汇总 sheet 带公式不影响数据 sheet 的导入）；文件级护栏（zip 炸弹/
行列上限）仍在 parse 层（安全护栏不放松）。
"""
from __future__ import annotations

import io
import os
import tempfile
import time
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from openpyxl import load_workbook

from knowledge_mining.mining.kb.db_artifacts import _encode_key_parts
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactError,
    ArtifactNotFound,
    ArtifactService,
    InvalidOperation,
    PrimaryKeyConflict,
    _is_formula,
    _try_float,
    _try_int,
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # 文件 ≤ 50MB
MAX_EXPANDED_BYTES = 200 * 1024 * 1024       # 解压 ≤ 200MB
MAX_ZIP_MEMBERS = 1_000
MAX_COLUMNS = 200                            # 单 sheet ≤ 200 列
MAX_ROWS = 100_000                           # 单 sheet ≤ 10 万行
INFERENCE_SAMPLE_ROWS = 200                  # 类型推断抽样 ≤ 200 行（默认 sheet）
SECONDARY_SHEET_SAMPLE_ROWS = 50             # 其余 sheet 类型推断抽样 ≤ 50 行
SAMPLE_ROWS = 50                             # 预览样例行 ≤ 50
ENUM_MAX_DISTINCT = 50                       # distinct ≤ 50 且非全数值 → enum
PREVIEW_TTL_SECONDS = 3600.0                 # 预览 token 1h
CONFLICTS_SAMPLE_LIMIT = 20
#: 单 uploader 活跃预览登记上限（对抗审查修复⑭；超限清最旧+429）。
MAX_ACTIVE_PREVIEWS_PER_UPLOADER = 5

_VALID_COLUMN_TYPES = {"text", "int", "float", "date", "enum", "bool"}

_TEMP_PREFIX = "kb_artifact_upload_"

#: 进程内预览登记（模块级——deps 每请求新建服务实例，登记必须跨请求存活）。
_PREVIEW_TOKENS: dict[str, dict[str, Any]] = {}


class ArtifactImportError(ArtifactError):
    """向导文件/预览级错误。route 层翻译：file_too_large→413、
    preview_expired→410、其余（invalid_xlsx/unsupported_file_type/
    too_many_rows/too_many_columns/formula_not_allowed/...）→422。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- helpers

def _remove_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())

# _is_formula 定义在 artifact_service（行写三入口共用），此处 import 复用。


def _is_date_like(value: Any) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.strip())
            return True
        except ValueError:
            return False
    return False


def _infer_column_type(values: list[Any]) -> tuple[str, list[str]]:
    """抽样 cast（int→float→date 试探）→ distinct ≤50 非全数值 → enum → text。"""
    non_blank = [v for v in values if not _is_blank(v)]
    if not non_blank:
        return "text", []
    if all(_try_int(v) is not None for v in non_blank):
        return "int", []
    if all(_try_float(v) is not None for v in non_blank):
        return "float", []
    if all(_is_date_like(v) for v in non_blank):
        return "date", []
    distinct = sorted({str(v).strip() for v in non_blank})
    if len(distinct) <= ENUM_MAX_DISTINCT:
        return "enum", distinct
    return "text", []


def _cast_value(raw: Any, column_type: str) -> tuple[bool, Any]:
    """按列类型 cast。返回 (ok, value)；ok=False 即类型错误（strict 阻断 /
    tolerant 置 null+conflicts）。date 统一存 ISO 字符串（jsonb 可序列化）。"""
    if _is_blank(raw):
        return True, None
    if column_type == "int":
        casted = _try_int(raw)
        return casted is not None, casted
    if column_type == "float":
        casted = _try_float(raw)
        return casted is not None, casted
    if column_type == "date":
        if isinstance(raw, datetime):
            return True, raw.date().isoformat()
        if isinstance(raw, date):
            return True, raw.isoformat()
        if isinstance(raw, str):
            try:
                return True, datetime.fromisoformat(raw.strip()).date().isoformat()
            except ValueError:
                return False, raw
        return False, raw
    if column_type == "bool":
        if isinstance(raw, bool):
            return True, raw
        if isinstance(raw, int) and raw in (0, 1):
            return True, bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return True, raw.strip().lower() == "true"
        return False, raw
    # text / enum：datetime 归一 ISO；字符串 strip；数值/布尔原样（jsonb 可存）
    if isinstance(raw, (datetime, date)):
        return True, raw.isoformat()
    if isinstance(raw, str):
        return True, raw.strip()
    return True, raw


def _dedupe_headers(header_values: tuple) -> list[str]:
    """表头清洗：空白→列序号占位名；重名→追加序号后缀（Excel 常见脏表头）。"""
    names: list[str] = []
    seen: dict[str, int] = {}
    for position, raw in enumerate(header_values, start=1):
        text = str(raw).strip() if raw is not None and str(raw).strip() else f"列{position}"
        count = seen.get(text, 0)
        seen[text] = count + 1
        names.append(text if count == 0 else f"{text}_{count + 1}")
    return names


def _check_zip_bomb(content: bytes) -> None:
    """zipfile 炸弹检查（同 user_import_service 口径：条目数/解压总量/压缩比）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            expanded = sum(m.file_size for m in members)
            compressed = sum(max(m.compress_size, 1) for m in members)
            if (
                len(members) > MAX_ZIP_MEMBERS
                or expanded > MAX_EXPANDED_BYTES
                or expanded > compressed * 100
            ):
                raise ArtifactImportError("xlsx_archive_limits_exceeded")
    except ArtifactImportError:
        raise
    except zipfile.BadZipFile as exc:
        raise ArtifactImportError("invalid_xlsx") from exc


def _open_workbook(path: str):
    try:
        return load_workbook(path, read_only=True, data_only=False)
    except ArtifactImportError:
        raise
    except Exception as exc:  # openpyxl 暴露多种格式私有异常，统一收敛
        raise ArtifactImportError("invalid_xlsx") from exc


def _cleanup_expired_previews() -> None:
    """TTL 惰性清理：每次 parse/get/build/execute 顺带清过期登记与临时文件。"""
    now = time.monotonic()
    expired = [
        token for token, record in _PREVIEW_TOKENS.items()
        if now - record["parsed_at"] > PREVIEW_TTL_SECONDS
    ]
    for token in expired:
        record = _PREVIEW_TOKENS.pop(token, None)
        if record is not None:
            _remove_file(record["path"])


def _consume_preview(preview_token: str) -> None:
    record = _PREVIEW_TOKENS.pop(preview_token, None)
    if record is not None:
        _remove_file(record["path"])


def _assert_preview_binding(
    record: dict[str, Any],
    artifact_id: str | None, uploader: str | None,
) -> None:
    """token 绑定校验（对抗审查修复⑬）：已绑定登记（routes 上传必带
    artifact_id+uploader）的 artifact_id/uploader 与请求上下文不符 →
    ArtifactNotFound（route 翻 404——token 属于他人/他制品，不暴露登记
    内容）。未绑定登记（服务层直调测试形态）不设卡。"""
    if record.get("artifact_id") is not None and (
        record.get("artifact_id") != artifact_id
        or record.get("uploader") != uploader
    ):
        raise ArtifactNotFound("preview_token_mismatch")


def _enforce_preview_cap(uploader: str | None) -> None:
    """每 uploader 活跃登记 ≤ MAX_ACTIVE_PREVIEWS_PER_UPLOADER（修复⑭）：
    超限先清最旧（下次重试即可成功）再报 preview_limit（route 翻 429）。
    未绑定 uploader 的登记（直调测试形态）不计。"""
    if uploader is None:
        return
    active = [
        (token, record) for token, record in _PREVIEW_TOKENS.items()
        if record.get("uploader") == uploader
    ]
    if len(active) < MAX_ACTIVE_PREVIEWS_PER_UPLOADER:
        return
    oldest_token, _ = min(active, key=lambda item: item[1]["parsed_at"])
    evicted = _PREVIEW_TOKENS.pop(oldest_token, None)
    if evicted is not None:
        _remove_file(evicted["path"])
    raise ArtifactImportError("preview_limit")


def _normalize_policy(raw: Any) -> dict[str, str]:
    """conflict_policy 归一：{type_error: strict|tolerant, key_conflict:
    block|keep_first}；缺省 strict/block。"""
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        raw = {"key_conflict": raw}
    if not isinstance(raw, dict):
        raise InvalidOperation("invalid_conflict_policy")
    type_policy = str(raw.get("type_error", "strict"))
    key_policy = str(raw.get("key_conflict", "block"))
    if type_policy not in ("strict", "tolerant") \
            or key_policy not in ("block", "keep_first"):
        raise InvalidOperation("invalid_conflict_policy")
    return {"type_error": type_policy, "key_conflict": key_policy}


def _row_key(data: dict[str, Any], pk_names: list[str]) -> tuple[str, bool]:
    """主键值 hex 编码（多列组合，61号 §6.3 + codex三审 P1-1：与
    db_artifacts._row_key_value 同构——追加 UPSERT 与 set_primary_key 重算/
    手工行增对同一主键值必须得到同一 row_key，否则同键行判重失效）；无主键
    或主键值全空 → uuid。返回 (row_key, missing_pk)——missing_pk=True 时
    调用方落冲突标记。"""
    if not pk_names:
        return uuid.uuid4().hex, False
    parts = [
        "" if data.get(name) is None else str(data.get(name))
        for name in pk_names
    ]
    if not any(parts):
        return uuid.uuid4().hex, True
    return _encode_key_parts(parts), False


# ----------------------------------------------------------------- service

class ArtifactImportService:
    """向导编排。构造入参只需 ArtifactDB 兼容对象（create_batch 用）；
    写路径全部通过调用方传入的 ArtifactService（权限/乐观锁/ensure_draft）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    # ------------------------------------------------------------ 第一步

    def parse_upload(
        self, file_bytes: bytes, file_name: str, *,
        artifact_id: str | None = None, uploader: str | None = None,
    ) -> dict[str, Any]:
        """护栏 → 临时文件 → 全部 sheet 扫描（每 sheet 类型推断+样例）→ 预览
        登记（绑定 artifact_id+uploader，修复⑬）。routes 侧经 anyio.to_thread
        调用（修复⑮：openpyxl 扫描不占事件循环）。"""
        _cleanup_expired_previews()
        _enforce_preview_cap(uploader)
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise ArtifactImportError("file_too_large")
        if Path(file_name).suffix.lower() != ".xlsx":
            raise ArtifactImportError("unsupported_file_type")
        _check_zip_bomb(file_bytes)
        token = uuid.uuid4().hex
        path = os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{token}.xlsx")
        with open(path, "wb") as handle:
            handle.write(file_bytes)
        try:
            sheets = self._scan_all_sheets(path)
        except Exception:
            _remove_file(path)
            raise
        default = sheets[0]
        _PREVIEW_TOKENS[token] = {
            "path": path,
            "file_name": file_name,
            "sheets": sheets,
            # 顶层 columns/sample_rows = 默认 sheet 引用（向后兼容，codex二审
            # 功能项①：preview.sheets 改 per-sheet 对象后旧消费者仍可读）
            "columns": default["columns"],
            "sample_rows": default["sample_rows"],
            "parsed_at": time.monotonic(),
            "artifact_id": artifact_id,
            "uploader": uploader,
        }
        return {"preview_token": token, "preview": self._preview_payload(token)}

    def get_preview(
        self, preview_token: str, *,
        artifact_id: str | None = None, uploader: str | None = None,
    ) -> dict[str, Any]:
        """与 parse_upload 同信封：{preview_token, preview}（绑定校验同
        _require_preview——失配 404）。"""
        _cleanup_expired_previews()
        record = _PREVIEW_TOKENS.get(preview_token)
        if record is None:
            raise ArtifactImportError("preview_expired")
        _assert_preview_binding(record, artifact_id, uploader)
        return {
            "preview_token": preview_token,
            "preview": self._preview_payload(preview_token),
        }

    @staticmethod
    def _preview_payload(preview_token: str) -> dict[str, Any]:
        record = _PREVIEW_TOKENS[preview_token]
        sheets = record["sheets"]
        return {
            "file_name": record["file_name"],
            # codex二审功能项①：sheets 为 per-sheet 对象 [{name, columns,
            # sample_rows}]；顶层 columns/sample_rows 保留默认 sheet 引用
            "sheets": [dict(sheet) for sheet in sheets],
            "default_sheet": sheets[0]["name"],
            "columns": record["columns"],
            "sample_rows": record["sample_rows"],
        }

    @staticmethod
    def _sheet_names(record: dict[str, Any]) -> list[str]:
        return [str(sheet["name"]) for sheet in record["sheets"]]

    def _require_preview(
        self, preview_token: str, *,
        artifact_id: str | None = None, uploader: str | None = None,
    ) -> dict[str, Any]:
        _cleanup_expired_previews()
        record = _PREVIEW_TOKENS.get(preview_token)
        if record is None:
            raise ArtifactImportError("preview_expired")
        _assert_preview_binding(record, artifact_id, uploader)
        return record

    @staticmethod
    def _scan_all_sheets(path: str) -> list[dict[str, Any]]:
        """全部 sheet 扫描（codex二审功能项①：只扫默认 sheet 时，切换 sheet
        后映射仍用首个 sheet 的列 → 异构第二 sheet 导入必失败）。

        每 sheet：护栏检查（列数/行数/公式）+ 类型推断抽样 + 样例行。抽样
        预算：默认 sheet = INFERENCE_SAMPLE_ROWS（200），其余 sheet =
        SECONDARY_SHEET_SAMPLE_ROWS（50）——避免宽多 sheet 文件抽样成本翻倍。
        默认 sheet 空表头 → empty_sheet（原有口径）；其余 sheet 空则保留
        columns=[] 的占位条目（选中时 build_mapping 重读表头自然报错，不上传
        整体失败——Excel 常见尾随空 sheet）。行数上限护栏仍按单 sheet 计。
        """
        workbook = _open_workbook(path)
        try:
            sheets: list[dict[str, Any]] = []
            for position, name in enumerate(workbook.sheetnames):
                sample_budget = (
                    INFERENCE_SAMPLE_ROWS if position == 0
                    else SECONDARY_SHEET_SAMPLE_ROWS
                )
                sheets.append(ArtifactImportService._scan_one_sheet(
                    workbook[str(name)], sample_budget,
                    require_header=position == 0,
                ))
            return sheets
        finally:
            workbook.close()

    @staticmethod
    def _scan_one_sheet(
        sheet: Any, sample_budget: int, *, require_header: bool,
    ) -> dict[str, Any]:
        """codex三审 P2-3：parse 层不做公式拒绝（预览只读展示合法——汇总
        sheet 带公式不应阻断数据 sheet 的上传预览）；公式格以 "=..." 字符串
        参与 infer（通常落 text）。文件级护栏（列数/行数）照旧。"""
        iterator = sheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if header_row is None:
            if require_header:
                raise ArtifactImportError("empty_sheet")
            return {"name": str(sheet.title), "columns": [], "sample_rows": []}
        headers = _dedupe_headers(header_row)
        if len(headers) > MAX_COLUMNS:
            raise ArtifactImportError("too_many_columns")
        sample: list[dict[str, Any]] = []
        data_rows = 0
        for row in iterator:
            data_rows += 1
            if data_rows > MAX_ROWS:
                raise ArtifactImportError("too_many_rows")
            if len(sample) < sample_budget:
                sample.append({name: value for name, value in zip(headers, row)})
        columns = []
        for name in headers:
            inferred, candidates = _infer_column_type(
                [row.get(name) for row in sample])
            columns.append({
                "source_name": name,
                "inferred_type": inferred,
                "enum_candidates": candidates if inferred == "enum" else [],
            })
        return {
            "name": str(sheet.title),
            "columns": columns,
            "sample_rows": sample[:SAMPLE_ROWS],
        }

    @staticmethod
    def _read_headers(
        path: str, sheet: str, header_row: int, *,
        check_formula: bool = False,
    ) -> list[str]:
        """指定 sheet / 表头行 → 清洗后的表头（build_mapping 与 execute 共用）。
        check_formula=True（execute_import 重读所选 sheet 时，codex三审 P2-3）：
        表头公式格拒绝 formula_not_allowed；build_mapping 预计算不查。"""
        workbook = _open_workbook(path)
        try:
            if sheet not in workbook.sheetnames:
                raise ArtifactImportError("unknown_sheet")
            headers = None
            for index, row in enumerate(
                workbook[sheet].iter_rows(values_only=True), start=1,
            ):
                if index == header_row:
                    if check_formula:
                        for cell in row:
                            if _is_formula(cell):
                                raise ArtifactImportError("formula_not_allowed")
                    headers = _dedupe_headers(row)
                    break
                if index > header_row:
                    break
            if headers is None:
                raise ArtifactImportError("empty_sheet")
            if len(headers) > MAX_COLUMNS:
                raise ArtifactImportError("too_many_columns")
            return headers
        finally:
            workbook.close()

    @staticmethod
    def _iter_data_rows(
        path: str, sheet: str, header_row: int,
    ) -> Iterator[tuple[int, tuple]]:
        """表头行之后的数据行流式迭代（Excel 物理行号, 原始行元组）。"""
        workbook = _open_workbook(path)
        try:
            if sheet not in workbook.sheetnames:
                raise ArtifactImportError("unknown_sheet")
            for index, row in enumerate(
                workbook[sheet].iter_rows(values_only=True), start=1,
            ):
                if index > header_row:
                    yield index, row
        finally:
            workbook.close()

    # ------------------------------------------------------------ 第二步

    def build_mapping(
        self,
        preview_token: str,
        *,
        sheet: str | None = None,
        header_row: int = 1,
        column_ops: list[dict[str, Any]] | None = None,
        target_columns: list[dict[str, Any]] | None = None,
        artifact_id: str | None = None,
        uploader: str | None = None,
    ) -> dict[str, Any]:
        """纯计算不落库。建表模式 ops 全 action='new'；追加模式 action∈
        {map_to: target 列 id | new | drop}。target_columns=制品现有 active 列
        （调用方经 service.list_columns 取，权限已查）——追加模式用于目标列
        校验与 missing_columns 提示。绑定校验（修复⑬）：失配 404。"""
        record = self._require_preview(
            preview_token, artifact_id=artifact_id, uploader=uploader,
        )
        sheet_names = self._sheet_names(record)
        sheet = sheet or sheet_names[0]
        if sheet not in sheet_names:
            raise ArtifactImportError("unknown_sheet")
        try:
            header_row = int(header_row)
        except (TypeError, ValueError) as exc:
            raise InvalidOperation("invalid_header_row") from exc
        if header_row < 1:
            raise InvalidOperation("invalid_header_row")
        headers = self._read_headers(record["path"], sheet, header_row)
        header_set = set(headers)
        ops = list(column_ops or [])
        if not ops:
            raise InvalidOperation("empty_column_ops")
        active = list(target_columns or [])
        active_by_id = {str(c["column_id"]): c for c in active}
        mode = "append" if any(
            str(op.get("action")) == "map_to" for op in ops) else "create"
        seen_sources: set[str] = set()
        plan_columns: list[dict[str, Any]] = []
        for op in ops:
            source = str(op.get("source") or "").strip()
            if not source or source not in header_set:
                raise InvalidOperation(f"unknown_source_column:{op.get('source')}")
            if source in seen_sources:
                raise InvalidOperation(f"duplicate_source_column:{source}")
            seen_sources.add(source)
            action = str(op.get("action") or "new")
            if action == "drop":
                plan_columns.append({"source": source, "action": "drop"})
                continue
            if action == "map_to":
                target_id = str(op.get("target") or op.get("column_id") or "").strip()
                target = active_by_id.get(target_id)
                if target is None:
                    raise InvalidOperation(f"unknown_target_column:{target_id}")
                plan_columns.append({
                    "source": source,
                    "action": "map_to",
                    "target_column_id": target_id,
                    "target_name": str(target["column_name"]),
                    "column_type": str(target.get("column_type") or "text"),
                })
                continue
            if action != "new":
                raise InvalidOperation(f"invalid_action:{action}")
            target_name = str(op.get("target_name") or "").strip()
            if not target_name:
                raise InvalidOperation(f"missing_target_name:{source}")
            column_type = str(op.get("column_type") or "text")
            if column_type not in _VALID_COLUMN_TYPES:
                raise InvalidOperation(f"invalid_column_type:{column_type}")
            entry: dict[str, Any] = {
                "source": source,
                "action": "new",
                "target_name": target_name,
                "column_type": column_type,
                "is_primary_key": bool(op.get("is_primary_key", False)),
                "is_indexed": bool(op.get("is_indexed", False)),
            }
            enum_values = op.get("enum_values")
            if enum_values is not None:
                if not isinstance(enum_values, list) or not all(
                    isinstance(v, (str, int, float)) for v in enum_values
                ):
                    raise InvalidOperation(f"invalid_enum_values:{source}")
                entry["enum_values"] = enum_values
            plan_columns.append(entry)
        missing_columns: list[dict[str, Any]] = []
        if mode == "append":
            mapped_ids = {
                c["target_column_id"] for c in plan_columns
                if c["action"] == "map_to"
            }
            missing_columns = [
                {"column_id": str(c["column_id"]), "column_name": str(c["column_name"])}
                for c in active if str(c["column_id"]) not in mapped_ids
            ]
        return {
            "mode": mode,
            "preview_token": preview_token,
            "sheet": sheet,
            "header_row": header_row,
            "columns": plan_columns,
            "missing_columns": missing_columns,
        }

    # ------------------------------------------------------------ 第三步

    async def execute_import(
        self,
        service: ArtifactService,
        *,
        user: dict[str, Any],
        artifact_id: str,
        preview_token: str,
        plan: dict[str, Any],
        conflict_policy: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """确认导入。全量校验（类型 + 文件内主键重复）先行——strict/block
        策略下报错即零写入；通过后 create_batch → 建列 → insert_rows UPSERT。
        token 绑定校验（修复⑬）：登记制品/uploader 与路径上下文失配 → 404。"""
        record = self._require_preview(
            preview_token, artifact_id=artifact_id, uploader=str(user["id"]),
        )
        policy = _normalize_policy(conflict_policy)
        plan_columns = list(plan.get("columns") or [])
        if not plan_columns:
            raise InvalidOperation("empty_plan_columns")
        sheet = str(plan.get("sheet") or self._sheet_names(record)[0])
        try:
            header_row = int(plan.get("header_row") or 1)
        except (TypeError, ValueError) as exc:
            raise InvalidOperation("invalid_header_row") from exc
        # mode 以实际动作推导（plan.mode 仅供参考，防手搓 plan 说谎）
        effective_mode = "append" if any(
            str(c.get("action")) == "map_to" for c in plan_columns) else "create"

        # 权限（owner/editor）+ 现状 schema + 设计态会话前置
        active = await service.list_columns(user=user, artifact_id=artifact_id)
        await service.ensure_draft(user=user, artifact_id=artifact_id)
        if effective_mode == "create" and active:
            raise InvalidOperation(
                "schema_already_exists: 建表模式仅用于无列制品；追加导入请用 map_to 映射")
        if effective_mode == "append" and not active:
            raise InvalidOperation(
                "no_schema: 追加模式需要已有列定义；首次导入请用建表模式")

        mapped, create_specs, append_specs = self._build_column_specs(
            plan_columns, active, effective_mode)
        # codex三审 P2-3：公式拒绝限定所选 sheet——表头在此查，数据值在下方
        # 行流内逐格查（行为不变：拒绝 → formula_not_allowed，零写入）。
        headers = self._read_headers(
            record["path"], sheet, header_row, check_formula=True)
        pk_names = self._primary_key_names(plan_columns, active, effective_mode)
        # 追加导入 ordinal 起点 = 现有 rev0 行数 + 1（对抗审查修复⑧：追加批
        # 的行号接在存量之后，不再与已有行挤 1..N）。ensure_draft 已检出，
        # 此处计数即检出席。
        ordinal_base = await self._db.count_rows(artifact_id, 0)

        prepared: list[dict[str, Any]] = []
        seen_keys: dict[str, int] = {}
        conflicts_sample: list[dict[str, Any]] = []
        strict_type_samples: list[dict[str, Any]] = []
        block_samples: list[dict[str, Any]] = []
        type_errors = 0
        key_conflicts = 0
        scanned = 0
        for row_index, raw_row in self._iter_data_rows(
            record["path"], sheet, header_row,
        ):
            scanned += 1
            if scanned > MAX_ROWS:
                raise ArtifactImportError("too_many_rows")
            for cell in raw_row:
                if _is_formula(cell):
                    raise ArtifactImportError("formula_not_allowed")
            if all(_is_blank(value) for value in raw_row):
                continue  # 全空行跳过（Excel 常见尾随空行）
            values = dict(zip(headers, raw_row))
            data: dict[str, Any] = {}
            conflicts: dict[str, Any] = {}
            for source, target, column_type in mapped:
                raw = values.get(source)
                ok, value = _cast_value(raw, column_type)
                if ok:
                    data[target] = value
                    continue
                type_errors += 1
                sample = {
                    "row_index": row_index, "row_key": None, "column": target,
                    "message": f"type_error:{raw}",
                }
                if len(conflicts_sample) < CONFLICTS_SAMPLE_LIMIT:
                    conflicts_sample.append(dict(sample))
                if policy["type_error"] == "strict":
                    if len(strict_type_samples) < CONFLICTS_SAMPLE_LIMIT:
                        strict_type_samples.append(dict(sample))
                else:
                    data[target] = None
                    conflicts[target] = f"type_error:{raw}"
            row_key, missing_pk = _row_key(data, pk_names)
            if missing_pk:
                conflicts["__row__"] = "missing_primary_key"
            if row_key in seen_keys:
                key_conflicts += 1
                if policy["key_conflict"] == "block":
                    if len(block_samples) < CONFLICTS_SAMPLE_LIMIT:
                        block_samples.append(
                            {"row_index": row_index, "row_key": row_key})
                    continue
                kept = prepared[seen_keys[row_key]]
                kept.setdefault("conflicts", {}).setdefault(
                    "__duplicate_key__", []).append(row_index)
                if len(conflicts_sample) < CONFLICTS_SAMPLE_LIMIT:
                    conflicts_sample.append({
                        "row_index": row_index, "row_key": row_key,
                        "column": None,
                        "message": f"duplicate_row_key:kept_first:{row_key}",
                    })
                continue
            seen_keys[row_key] = len(prepared)
            prepared.append({
                "row_key": row_key,
                "data": data,
                "provenance": [],
                "conflicts": conflicts,
                "ordinal": ordinal_base + len(prepared) + 1,
                "_row_index": row_index,
            })
        # strict / block：全量校验失败 → 聚合样例报错，不落任何行
        if strict_type_samples:
            raise PrimaryKeyConflict(
                "type_error:" + repr(strict_type_samples[:10]))
        if block_samples:
            raise PrimaryKeyConflict("duplicate_key:" + repr(block_samples[:10]))

        # 批次先建（provenance 引用 batch_id），再建列、落行
        batch = await self._db.create_batch(
            artifact_id, channel="upload",
            file_name=str(record["file_name"]), sheet_name=sheet,
            row_count=len(prepared), created_by=str(user["id"]),
        )
        provenance_base = {
            "batch_id": batch["batch_id"],
            "file_name": str(record["file_name"]),
            "sheet_name": sheet,
        }
        for row in prepared:
            row["provenance"] = [
                {**provenance_base, "row_index": row.pop("_row_index")}]
        if create_specs:
            await service.replace_columns(
                user=user, artifact_id=artifact_id, columns=create_specs)
        for column_id, fields in append_specs:
            await service.upsert_column(
                user=user, artifact_id=artifact_id,
                column_id=column_id, fields=fields,
            )
        result = await service.insert_rows(
            user=user, artifact_id=artifact_id, rows=prepared,
        )
        _consume_preview(preview_token)
        return {
            "artifact_id": artifact_id,
            "mode": effective_mode,
            "batch_id": batch["batch_id"],
            "sheet": sheet,
            "row_count": len(prepared),
            "inserted": result["inserted"],
            "updated": result["updated"],
            "type_errors": type_errors,
            "key_conflicts": key_conflicts,
            "conflicts_sample": conflicts_sample,
            "missing_columns": list(plan.get("missing_columns") or []),
        }

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _build_column_specs(
        plan_columns: list[dict[str, Any]],
        active: list[dict[str, Any]],
        mode: str,
    ) -> tuple[list[tuple[str, str, str]], list[dict], list[tuple[str, dict]]]:
        """→ (mapped[(source, target_name, column_type)], 建表列 specs,
        追加新列 [(column_id, upsert fields)])。"""
        active_by_id = {str(c["column_id"]): c for c in active}
        existing_ids = set(active_by_id)
        next_ordinal = max(
            (int(c.get("ordinal") or 0) for c in active), default=0)
        mapped: list[tuple[str, str, str]] = []
        create_specs: list[dict[str, Any]] = []
        append_specs: list[tuple[str, dict[str, Any]]] = []
        for position, column in enumerate(plan_columns, start=1):
            action = str(column.get("action") or "new")
            if action == "drop":
                continue
            if action == "map_to":
                target = active_by_id.get(
                    str(column.get("target_column_id") or column.get("target") or ""))
                if target is None:
                    raise InvalidOperation(
                        f"unknown_target_column:{column.get('target_column_id')}")
                mapped.append((
                    str(column["source"]),
                    str(target["column_name"]),
                    str(target.get("column_type") or "text"),
                ))
                continue
            name = str(column.get("target_name") or "").strip()
            column_type = str(column.get("column_type") or "text")
            if not name or column_type not in _VALID_COLUMN_TYPES:
                raise InvalidOperation(f"invalid_plan_column:{column.get('source')}")
            if mode == "create":
                create_specs.append({
                    "column_id": f"c{position:03d}",
                    "column_name": name,
                    "column_type": column_type,
                    "enum_values": column.get("enum_values"),
                    "is_primary_key": bool(column.get("is_primary_key")),
                    "is_indexed": bool(column.get("is_indexed")),
                    "alias": [],
                    "ordinal": position,
                })
            else:
                next_ordinal += 1
                column_id = f"c{next_ordinal:03d}"
                while column_id in existing_ids:
                    next_ordinal += 1
                    column_id = f"c{next_ordinal:03d}"
                existing_ids.add(column_id)
                fields: dict[str, Any] = {
                    "column_name": name,
                    "column_type": column_type,
                    "is_indexed": bool(column.get("is_indexed")),
                    "ordinal": next_ordinal,
                }
                if column.get("enum_values") is not None:
                    fields["enum_values"] = column["enum_values"]
                append_specs.append((column_id, fields))
            mapped.append((str(column["source"]), name, column_type))
        return mapped, create_specs, append_specs

    @staticmethod
    def _primary_key_names(
        plan_columns: list[dict[str, Any]],
        active: list[dict[str, Any]],
        mode: str,
    ) -> list[str]:
        """建表模式：plan 中标记 is_primary_key 的列；追加模式：沿用 schema
        既有主键（文件映射无关——主键是制品属性不是批次属性）。"""
        if mode == "append":
            return [str(c["column_name"]) for c in active if c.get("is_primary_key")]
        ordered = [
            str(c["target_name"]) for c in plan_columns
            if str(c.get("action") or "new") != "drop" and c.get("is_primary_key")
        ]
        return ordered
