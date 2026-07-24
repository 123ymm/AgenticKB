"""Extract large 3GPP DOCX specs into Markdown and section JSONL.

The implementation intentionally uses only the Python standard library. The
3GPP DOCX files are large but structurally regular enough to parse directly
from WordprocessingML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"

NS = {
    "w": W_NS,
    "cp": CP_NS,
    "dc": DC_NS,
    "dcterms": DCTERMS_NS,
}

W = f"{{{W_NS}}}"

HEADING_STYLE_RE = re.compile(r"^Heading([1-9])$")
CLAUSE_RE = re.compile(
    r"^(?P<num>(?:\d+[A-Za-z]?)(?:\.\d+[A-Za-z]?)*)(?P<title>[A-Z][^\d].*|[a-z][^\d].*|\s+.*)$"
)
ANNEX_RE = re.compile(r"^Annex\s+(?P<annex>[A-Z]+)\b", re.IGNORECASE)

SPEC_TITLES = {
    "23501": "3GPP TS 23.501 - System Architecture for the 5G System",
    "23502": "3GPP TS 23.502 - Procedures for the 5G System",
    "23503": "3GPP TS 23.503 - Policy and Charging Control Framework for the 5G System",
}


@dataclass
class Block:
    kind: str
    text: str
    style: str = ""
    level: int | None = None
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Section:
    section_id: str
    title: str
    level: int
    clause: str | None
    source_doc: str
    block_start: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.kind == "heading":
                continue
            if block.kind == "table":
                parts.append(table_to_text(block.rows))
            elif block.text:
                parts.append(block.text)
        return "\n\n".join(p for p in parts if p).strip()

    def to_json_record(self, index: int) -> dict:
        text = self.text
        return {
            "section_index": index,
            "section_id": self.section_id,
            "source_doc": self.source_doc,
            "title": self.title,
            "level": self.level,
            "clause": self.clause,
            "block_start": self.block_start,
            "block_count": len(self.blocks),
            "char_count": len(text),
            "text": text,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def xml_text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, NS)
    return found.text if found is not None else None


def read_core_properties(zf: ZipFile) -> dict:
    if "docProps/core.xml" not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read("docProps/core.xml"))
    return {
        "title": xml_text(root, "dc:title"),
        "subject": xml_text(root, "dc:subject"),
        "creator": xml_text(root, "dc:creator"),
        "created": xml_text(root, "dcterms:created"),
        "modified": xml_text(root, "dcterms:modified"),
    }


def paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag == W + "br":
            parts.append("\n")
    return "".join(parts).replace("\u00a0", " ").strip()


def paragraph_style(p: ET.Element) -> str:
    pstyle = p.find("./w:pPr/w:pStyle", NS)
    if pstyle is None:
        return ""
    return pstyle.attrib.get(W + "val", "")


def table_rows(tbl: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in tbl.findall("./w:tr", NS):
        row: list[str] = []
        for tc in tr.findall("./w:tc", NS):
            cell_parts = []
            for p in tc.findall(".//w:p", NS):
                text = paragraph_text(p)
                if text:
                    cell_parts.append(text)
            row.append(" ".join(cell_parts).strip())
        if any(row):
            rows.append(row)
    return rows


def iter_body_blocks(document_root: ET.Element) -> Iterable[Block]:
    body = document_root.find(".//w:body", NS)
    if body is None:
        return
    for child in body:
        if child.tag == W + "p":
            text = paragraph_text(child)
            style = paragraph_style(child)
            if not text:
                continue
            if style.startswith("TOC"):
                continue
            yield Block(kind="paragraph", text=normalize_heading_spacing(text), style=style)
        elif child.tag == W + "tbl":
            rows = table_rows(child)
            if rows:
                yield Block(kind="table", text="", rows=rows)


def normalize_heading_spacing(text: str) -> str:
    # 3GPP Word headings often appear as "1Scope" or "4.2.1General".
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"^((?:\d+[A-Za-z]?)(?:\.\d+[A-Za-z]?)*)([A-Z][A-Za-z].*)$", text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text


def heading_info(block: Block) -> tuple[int, str | None] | None:
    style_match = HEADING_STYLE_RE.match(block.style)
    if style_match:
        level = int(style_match.group(1))
        clause = extract_clause(block.text)
        return level, clause

    if ANNEX_RE.match(block.text):
        return 1, None

    if block.style not in {"", "Normal"}:
        return None
    if "\t" in block.text or len(block.text) > 180:
        return None

    clause = extract_clause(block.text)
    if clause and len(clause.split(".")) <= 6:
        return min(len(clause.split(".")), 6), clause
    return None


def extract_clause(text: str) -> str | None:
    match = re.match(r"^((?:\d+[A-Za-z]?)(?:\.\d+[A-Za-z]?)*)(?:\s+|$)", text)
    if not match:
        return None
    clause = match.group(1)
    if "." not in clause and not clause.isdigit():
        return None
    return clause


def sanitize_anchor(text: str) -> str:
    value = re.sub(r"[^0-9A-Za-z._-]+", "-", text.strip()).strip("-")
    return value[:120] or "section"


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    sep = ["---"] * width
    body = normalized[1:]

    def fmt(row: list[str]) -> str:
        escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
        return "| " + " | ".join(escaped) + " |"

    lines = [fmt(header), fmt(sep)]
    lines.extend(fmt(row) for row in body)
    return "\n".join(lines)


def table_to_text(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(cell for cell in row) for row in rows)


def block_to_markdown(block: Block) -> str:
    if block.kind == "heading":
        level = min(block.level or 1, 6)
        return f"{'#' * level} {block.text}"
    if block.kind == "table":
        return table_to_markdown(block.rows)
    if block.style in {"B1", "B2", "B3", "B4", "BL"}:
        return f"- {block.text}"
    if block.style == "EX":
        return f"> {block.text}"
    return block.text


def parse_docx(path: Path) -> tuple[dict, list[Block], list[Section]]:
    with ZipFile(path) as zf:
        metadata = read_core_properties(zf)
        document_root = ET.fromstring(zf.read("word/document.xml"))

    blocks: list[Block] = []
    sections: list[Section] = []
    current: Section | None = None
    spec_key = re.sub(r"[^0-9]", "", path.stem)[:5]
    source_title = SPEC_TITLES.get(spec_key, path.stem)

    for index, block in enumerate(iter_body_blocks(document_root)):
        info = heading_info(block)
        if info:
            level, clause = info
            heading = Block(kind="heading", text=block.text, style=block.style, level=level)
            blocks.append(heading)
            section_id = f"{path.stem}:{sanitize_anchor(clause or block.text)}:{len(sections)+1}"
            current = Section(
                section_id=section_id,
                title=block.text,
                level=level,
                clause=clause,
                source_doc=path.name,
                block_start=len(blocks) - 1,
            )
            current.blocks.append(heading)
            sections.append(current)
            continue

        blocks.append(block)
        if current is None:
            current = Section(
                section_id=f"{path.stem}:front-matter",
                title="Front matter",
                level=1,
                clause=None,
                source_doc=path.name,
                block_start=0,
            )
            sections.append(current)
        current.blocks.append(block)

    metadata.update(
        {
            "source_doc": path.name,
            "source_title": source_title,
            "source_path": str(path),
            "sha256": sha256_file(path),
            "file_size": path.stat().st_size,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return metadata, blocks, sections


def write_markdown(path: Path, metadata: dict, blocks: list[Block]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {metadata.get('source_title') or metadata['source_doc']}",
        "",
        "```yaml",
        f"source_doc: {metadata['source_doc']}",
        f"source_path: {metadata['source_path']}",
        f"sha256: {metadata['sha256']}",
        f"extracted_at: {metadata['extracted_at']}",
        "```",
        "",
    ]
    for block in blocks:
        rendered = block_to_markdown(block)
        if rendered:
            lines.append(rendered)
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_sections(path: Path, sections: list[Section]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for index, section in enumerate(sections):
            f.write(json.dumps(section.to_json_record(index), ensure_ascii=False) + "\n")


def make_summary(manifest: dict) -> str:
    lines = [
        "# 3GPP DOCX Extraction Summary",
        "",
        f"Extracted at: {manifest['extracted_at']}",
        "",
        "| Source | Size bytes | Markdown bytes | Sections | Blocks | SHA-256 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for doc in manifest["documents"]:
        lines.append(
            "| {source_doc} | {file_size} | {markdown_size} | {section_count} | {block_count} | `{sha256}` |".format(
                **doc
            )
        )
    lines.extend(
        [
            "",
            "## Next Step",
            "",
            "Use `outputs/sections/*.jsonl` to select topic-specific clauses for realistic corpus construction.",
            "The full Markdown files are raw extracted material and should not be treated as curated final data.",
            "",
        ]
    )
    return "\n".join(lines)


def extract_all(input_root: Path, output_root: Path, pattern: str) -> dict:
    docs = sorted(input_root.glob(pattern))
    if not docs:
        raise FileNotFoundError(f"No DOCX files matched {input_root / pattern}")

    markdown_dir = output_root / "markdown"
    sections_dir = output_root / "sections"
    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "pattern": pattern,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "documents": [],
    }

    for docx_path in docs:
        metadata, blocks, sections = parse_docx(docx_path)
        md_path = markdown_dir / f"{docx_path.stem}.md"
        jsonl_path = sections_dir / f"{docx_path.stem}.sections.jsonl"
        write_markdown(md_path, metadata, blocks)
        write_sections(jsonl_path, sections)
        manifest["documents"].append(
            {
                **metadata,
                "markdown_path": str(md_path),
                "sections_path": str(jsonl_path),
                "markdown_size": md_path.stat().st_size,
                "sections_size": jsonl_path.stat().st_size,
                "section_count": len(sections),
                "block_count": len(blocks),
            }
        )

    (reports_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reports_dir / "summary.md").write_text(make_summary(manifest), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", default="cloud_core_coldstart_md")
    parser.add_argument("--output-root", default="data/3gpp_docx_extraction/outputs")
    parser.add_argument("--pattern", default="2350*-k10.docx")
    args = parser.parse_args()

    manifest = extract_all(Path(args.input_root), Path(args.output_root), args.pattern)
    print(json.dumps(
        {
            "documents": len(manifest["documents"]),
            "output_root": manifest["output_root"],
            "sections": sum(d["section_count"] for d in manifest["documents"]),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
