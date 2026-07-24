"""Search extracted 3GPP section JSONL files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def iter_sections(sections_dir: Path):
    for path in sorted(sections_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield path, json.loads(line)


def score_record(record: dict, terms: list[str]) -> int:
    title = record.get("title", "").lower()
    text = record.get("text", "").lower()
    score = 0
    for term in terms:
        score += title.count(term) * 1000
        score += min(text.count(term), 50)
    return score


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Keyword query, e.g. 'network slicing'")
    parser.add_argument("--sections-dir", default="data/3gpp_docx_extraction/outputs/sections")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Print JSON lines instead of text")
    parser.add_argument("--include-change-history", action="store_true")
    args = parser.parse_args()

    terms = [term.lower() for term in args.query.split() if term.strip()]
    if not terms:
        raise SystemExit("empty query")

    results = []
    for path, record in iter_sections(Path(args.sections_dir)):
        title = record.get("title", "")
        if not args.include_change_history and "change history" in title.lower():
            continue
        hay = f"{record.get('title', '')}\n{record.get('text', '')}".lower()
        if all(term in hay for term in terms):
            score = score_record(record, terms)
            results.append((score, path.name, record))

    results.sort(key=lambda item: (-item[0], item[1], item[2].get("section_index", 0)))
    for score, file_name, record in results[: args.limit]:
        output = {
            "score": score,
            "file": file_name,
            "source_doc": record.get("source_doc"),
            "clause": record.get("clause"),
            "title": record.get("title"),
            "char_count": record.get("char_count"),
            "section_id": record.get("section_id"),
        }
        if args.json:
            print(json.dumps(output, ensure_ascii=False))
        else:
            print(
                f"{score:>5} | {file_name} | {output['clause'] or '-'} | "
                f"{output['title']} | chars={output['char_count']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
