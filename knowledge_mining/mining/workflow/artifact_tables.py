"""Table-workbench operators. Reuse the workflow registry, without mining finalization."""
from __future__ import annotations

import re
from typing import Any

from .handler_registry import HandlerRegistry

PROFILE = "artifact_table"
OPERATOR = "table_extract"
COLUMNS = [
    {"key": "directory", "name": "目录", "type": "text"},
    {"key": "file_name", "name": "文件名", "type": "text"},
    {"key": "file_content", "name": "文件内容", "type": "text"},
    {"key": "table_name", "name": "表名", "type": "text"},
    {"key": "table_content", "name": "表格内容", "type": "json"},
]


def _caption_text(value: str) -> str:
    """Remove outer Markdown presentation without changing the title itself."""
    value = value.strip()
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = value.replace(r"\*", "*")
    for marker in ("***", "**", "*", "__", "_"):
        if value.startswith(marker) and value.endswith(marker) and len(value) > 2 * len(marker):
            value = value[len(marker):-len(marker)].strip()
    return value


def _table_caption(asset: dict, ref: str, elements: list[dict], ir: dict) -> tuple[str, str]:
    by_id = {e["element_id"]: e for e in elements}
    caption = by_id.get(asset.get("caption_element_id"), {})
    title = _caption_text(caption.get("text") or caption.get("normalized_text") or "")
    if title:
        return title, "caption"
    # The parser/reconciler identifies table assets as <element_id>-table.
    table_id = asset.get("table_id") or ref
    for index, element in enumerate(elements):
        if element.get("element_type") != "table" or table_id != f"{element['element_id']}-table":
            continue
        for relation in ir.get("relations", []):
            if relation.get("relation_type") == "caption_of" and relation.get("target_element_id") == element["element_id"]:
                caption = by_id.get(relation.get("source_element_id"), {})
                title = _caption_text(caption.get("text") or caption.get("normalized_text") or "")
                if title:
                    return title, "caption"
        # Conservative fallback for existing IR that did not bind Markdown captions.
        if index:
            previous = elements[index - 1]
            pages, previous_pages = set(element.get("page_span_ids") or []), set(previous.get("page_span_ids") or [])
            if pages and previous_pages and not pages.intersection(previous_pages):
                continue
            title = _caption_text(previous.get("text") or previous.get("normalized_text") or "")
            if previous.get("element_type") in {"paragraph", "caption", "heading"} and re.match(
                r"^表\s*[0-9一二三四五六七八九十百]+(?:[.－-][0-9]+)*(?:\s+|[：:、])\S+", title
            ) and "\n" not in title:
                return title, "adjacent_caption"
    return "", ""


def readable_table(columns: list, cells: list[dict]) -> dict:
    """Project stored cells to readable records without coercing source values."""
    names = {}
    for index, column in enumerate(columns):
        names[index] = (column.get("name") or column.get("column_name") or column.get("key") or "") if isinstance(column, dict) else str(column)
    for cell in cells:
        index = cell["column_index"]
        if not names.get(index):
            names[index] = cell.get("column_name") or ""
    used = set()
    for index in sorted(names):
        base = _caption_text(names[index]) or f"列{index + 1}"
        name, suffix = base, 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        names[index] = name
        used.add(name)
    headers = [names[i] for i in sorted(names)]
    rows = {}
    for cell in cells:
        if cell.get("is_header"):
            continue
        row = rows.setdefault(cell["row_index"], dict.fromkeys(headers))
        row[names[cell["column_index"]]] = cell.get("value")
    return {"表头": headers, "数据": [rows[i] for i in sorted(rows)]}


def extract_rows(source: dict, tables: list[dict], cells: list[dict], ir: dict) -> tuple[list[dict], str]:
    """One row per extracted table; preserve coordinates/headers/spans in JSON."""
    elements = ir.get("elements", [])
    if isinstance(elements, dict):
        elements = list(elements.values())
    elements = sorted(elements, key=lambda e: e.get("order_index", 0))
    text = "\n".join(e.get("text") or e.get("normalized_text") or "" for e in elements)
    assets = ir.get("structured_assets") or {}
    by_table: dict[str, list] = {}
    for cell in cells:
        by_table.setdefault(cell["table_ref"], []).append(
            {k: v for k, v in cell.items() if k not in ("snapshot_id", "table_ref")}
        )
    rows = []
    for table in tables:
        ref = table["table_ref"]
        asset = assets.get(ref) or next(
            (a for a in assets.values() if a.get("table_id") == ref), {},
        )
        caption, name_source = _table_caption(asset, ref, elements, ir)
        name = caption or table.get("sheet_name") or f"未命名表 {ref}"
        rows.append({
            "row_id": f"{source['id']}:{source['snapshot_id']}:{ref}",
            "directory": source.get("directory_path") or "",
            "file_name": source.get("document_name") or source["id"],
            "document_id": source["id"],
            "table_name": name,
            "table_content": readable_table(table["columns_json"], by_table.get(ref, [])),
            "provenance": {
                "document_id": source["id"], "snapshot_id": source["snapshot_id"],
                "table_ref": ref, "name_source": name_source if caption else
                "sheet" if table.get("sheet_name") else "generated",
                "readiness": table.get("readiness"),
            },
        })
    return rows, text


async def _extract(context: Any, parameters: dict, state: Any) -> dict:
    return await context.extract(parameters["sources"])


def table_handler_registry() -> HandlerRegistry:
    from .artifact_table_ops import CATALOG
    registry = HandlerRegistry()
    registry.register(OPERATOR, "1", _extract)
    for name, _ in CATALOG:
        async def handle(context, parameters, state, operator=name):
            return await context.process(operator, parameters)
        registry.register(name, "1", handle)
    return registry


def extraction_manifest(sources: list[dict]) -> dict:
    # A typed profile, not a fake document pipeline with assets_persisted capabilities.
    return {
        "profile": PROFILE, "schemaVersion": "1.0", "columns": COLUMNS,
        "nodes": [{"nodeId": "step1", "operatorType": OPERATOR,
                   "operatorVersion": "1", "params": {"sources": sources}}],
        "edges": [],
    }
