"""Snapshot stage: manage shared document snapshots for v1.1.

Implements the three-layer model:
- document (identity via document_key)
- snapshot (shared content via normalized_content_hash)
- link (document -> snapshot mapping)
"""
from __future__ import annotations

import uuid
from typing import Any

from knowledge_mining.mining.infra.db import AssetCoreDB
from knowledge_mining.mining.contracts.models import RawFileData, DocumentProfile


def select_or_create_snapshot(
    asset_db: AssetCoreDB,
    doc: RawFileData,
    profile: DocumentProfile,
    *,
    domain: str,
    batch_id: str | None = None,
    existing_doc: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Select existing or create new snapshot for a document.

    Returns (document_id, snapshot_id, link_id).
    If snapshot already exists (same normalized_content_hash), reuses it.
    """
    from knowledge_mining.mining.ingestion import get_mime_type

    mime_type = get_mime_type(doc.file_type)

    # 1. Resolve document identity.
    # KB 路径：身份已由 KB 预建，且 walk 阶段已按 storage_path 解析为 existing_doc（G1）→
    #          直接复用 id，不再按 document_key 二次查找（消解多库同 key 歧义）。
    # Legacy 路径（/api/runs，未预建身份）：existing_doc 为 None → upsert 建身份（deprecated 例外）。
    if existing_doc:
        document_id = existing_doc["id"]
    else:
        document_id = asset_db.upsert_document(
            domain=domain,
            document_id=uuid.uuid4().hex,
            document_key=profile.document_key,
            document_name=doc.file_name,
            document_type=profile.document_type,
            metadata_json=doc.metadata_json,
        )

    # 2. Find or create snapshot
    snapshot_id = uuid.uuid4().hex
    existing_snap = asset_db.get_snapshot_by_hash(
        domain=domain,
        normalized_content_hash=doc.normalized_content_hash,
    )
    if existing_snap:
        snapshot_id = existing_snap["id"]

    snapshot_id = asset_db.upsert_snapshot(
        domain=domain,
        snapshot_id=snapshot_id,
        normalized_content_hash=doc.normalized_content_hash,
        raw_content_hash=doc.raw_content_hash,
        mime_type=mime_type,
        title=doc.title,
        scope_json=doc.scope_json,
        tags_json=doc.tags_json,
        parser_profile_json={"file_type": doc.file_type},
        metadata_json=doc.metadata_json,
    )

    # 3. Create link (always new for this ingestion)
    link_id = uuid.uuid4().hex
    asset_db.insert_snapshot_link(
        domain=domain,
        link_id=link_id,
        document_id=document_id,
        document_snapshot_id=snapshot_id,
        source_batch_id=batch_id,
        relative_path=doc.relative_path,
        source_uri=doc.source_uri,
        title=doc.title,
        scope_json=doc.scope_json,
        tags_json=doc.tags_json,
    )

    return document_id, snapshot_id, link_id
