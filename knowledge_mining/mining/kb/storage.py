"""KB file storage path strategy + traversal guard."""
from __future__ import annotations

from pathlib import Path


def build_storage_path(upload_root: Path, kb_id: str,
                       directory_path: str | None, filename: str) -> Path:
    """Build the on-disk path for a KB file; reject any traversal outside the KB root."""
    base = (upload_root / kb_id).resolve()
    parts = [p for p in (directory_path or "").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("unsafe directory path")
    fname = Path(filename).name  # strip any path components from filename itself
    if not fname:
        raise ValueError("empty filename")
    candidate = base.joinpath(*parts, fname)
    full = candidate.resolve()
    try:
        full.relative_to(base)
    except ValueError as exc:
        raise ValueError("path escapes kb storage root") from exc
    return full


def build_document_key(directory_path: str | None, filename: str) -> str:
    """Build document_key matching the mining pipeline's derivation.

    pipeline (jobs/run.py:880): ``doc_key = f"doc:/{relative_path}"`` where relative_path
    is the file path relative to the mining input dir. KB triggers mining with
    input = ``{upload_root}/{kb_id}/``, so relative_path = ``{directory_path}/{filename}``.
    """
    parts = [p for p in (directory_path or "").split("/") if p]
    parts.append(Path(filename).name)
    return "doc:/" + "/".join(parts)
