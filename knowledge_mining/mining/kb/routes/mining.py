"""KB mining trigger — POST /api/kb/{kb_id}/mine.

镜像 api/routes/runs.py:create_run，但 input_path 锁定到 KB 的上传目录，
run metadata 带 kb_id。复用 _domain_run_lock（与旧 /api/runs 共享域级 mutex）。

⚠️ 已知限制（设计 §10）：pipeline 用 (domain, document_key) 经 get_document_by_key
定位文档身份。P1 把 UNIQUE 改成 (kb_id, document_key) 后，同域多 KB 同 document_key
的场景下该查询会歧义（可能挂到别的 KB 文档）。常见场景（单 KB 或键不撞）正常；
多 KB 同 key 的彻底解需把 kb_id 纳入 document_key 或显式透传 document_id（后续）。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from knowledge_mining.mining.api.routes.runs import _domain_run_lock
from knowledge_mining.mining.infra.upload_config import UploadConfig
from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.deps import get_kb_db
from knowledge_mining.mining.kb.db import KbDB

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kb/{kb_id}/mine", tags=["kb-mining"])


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=202)
async def mine_kb(
    kb_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    kbdb: KbDB = Depends(get_kb_db),
) -> dict[str, Any]:
    """Trigger mining for a KB's uploaded (not-yet-mined / changed) documents.

    Lifecycle SKIP/RESTORE ensures unchanged docs cost nothing; only new/changed docs mine.
    """
    kb = await kbdb.get_kb(kb_id)
    if kb is None:
        raise HTTPException(404, f"KB {kb_id} not found")
    # 看不到 → 404（不泄露），看得到但不能写 → 403
    if not await kbdb.is_visible(kb_id=kb_id, user_id=user["id"]):
        raise HTTPException(404, f"KB {kb_id} not found")
    if not await kbdb.can_write(kb_id=kb_id, user_id=user["id"]):
        raise HTTPException(403, "only owner or editor may trigger mining")

    domain = kb["domain"]
    input_path = str((UploadConfig().upload_root_path / kb_id).resolve())
    if not Path(input_path).is_dir():
        raise HTTPException(400, "KB has no uploaded files to mine")

    db_config = request.app.state.db_config
    run_lock = _domain_run_lock(domain)
    if not run_lock.acquire(blocking=False):
        raise HTTPException(
            409, f"A mining run is already in progress for domain '{domain}'.",
        )

    run_id = uuid.uuid4().hex
    started_at = _utcnow()
    try:
        async with request.app.state.pg_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mining_runs "
                "(id, input_path, domain, status, current_stage, started_at, metadata_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                [run_id, input_path, domain, "queued", "queued", started_at,
                 json.dumps({"kb_id": kb_id})],
            )
    except Exception:
        run_lock.release()
        raise

    def _run_in_thread() -> None:
        try:
            from knowledge_mining.mining.jobs.run import run as mining_run
            mining_run(input_path, db_config=db_config, domain=domain, run_id=run_id)
        except Exception:
            logger.exception("KB mining run %s failed", run_id)
        finally:
            run_lock.release()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return {"run_id": run_id, "kb_id": kb_id, "status": "queued", "started_at": started_at}
