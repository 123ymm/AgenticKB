"""Admin endpoints — worker diagnostics, config status/reload, retention cleanup."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from llm_service.models import CleanupRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _require_internal_auth(request: Request) -> None:
    """Fail-closed internal gate: valid X-Internal-Auth required.

    The control plane strips client-forged copies and injects the real secret
    only for authenticated users — direct calls without it are rejected.
    """
    expected = getattr(request.app.state, "internal_verify_secret", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="internal verify secret not configured — endpoint refuses to run",
        )
    provided = request.headers.get("x-internal-auth", "")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="valid internal auth required")


@router.get("/worker-status")
async def worker_status(request: Request):
    """Diagnostic: return actual Worker concurrency and poll interval."""
    worker = getattr(request.app.state, "worker", None)
    if not worker:
        return {"ok": True, "worker_running": False}
    return {
        "ok": True,
        "worker_running": worker._running,
        "concurrency": worker._concurrency,
        "poll_interval": worker._poll_interval,
        "active_tasks": len(worker._tasks),
    }


@router.get("/config-status")
async def config_status(request: Request):
    """62号 D1：当前生效的按域配置摘要（不含密钥）。

    全局 active_model、各覆盖域 effective active/rerank 模型、被作废的
    覆盖及原因——发布验收与巡检入口。Internal auth 闸（见上）。
    """
    _require_internal_auth(request)
    dc = getattr(request.app.state, "domain_configs", None)
    if dc is None:
        raise HTTPException(status_code=503, detail="domain configs not loaded")
    return {"success": True, "data": dc.status()}


@router.post("/reload-config")
async def reload_config(request: Request):
    """62号 B3：重拉控制面配置（全局 + 各域覆盖）并热生效。

    与被删的旧 reload-config（aaaad19，全运行时热切换）不同：这里只原子
    换 DomainConfigs 并清空 provider 解析缓存——chat/rerank 的 provider 与
    域覆盖立即按新配置生效；worker 并发、task 重试、template TTL 等运行时
    旋钮在启动期消费，仍需重启。全局配置拉取失败保持旧配置运行。

    双闸（同 cleanup）：X-Internal-Auth + X-KB-Role=admin——它改路由与密钥。
    """
    _require_internal_auth(request)
    if request.headers.get("x-kb-role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")

    from llm_service.config import load_domain_configs

    dc = getattr(request.app.state, "domain_configs", None)
    resolver = getattr(request.app.state, "provider_resolver", None)
    if dc is None or resolver is None:
        raise HTTPException(status_code=503, detail="domain configs not loaded")

    try:
        new_dc = load_domain_configs()
    except Exception as exc:
        logger.exception("reload-config: fetch failed, keeping current config")
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"fetch failed, keeping current config: {exc}"},
        )

    dc.replace_with(new_dc)
    resolver.clear_cache()
    status = dc.status()
    logger.info(
        "reload-config applied: %d domain override(s), %d discarded",
        len(status["domains"]), len(status["invalid_domains"]),
    )
    return {"ok": True, "data": status}


@router.post("/cleanup")
async def cleanup_tasks(body: CleanupRequest, request: Request):
    """Batched retention cleanup for terminal agent_llm_* audit rows.

    Deletes tasks with terminal status (succeeded/failed/dead_letter/
    cancelled) finished before the retention cutoff, their child rows
    (requests/attempts/results/events), and stale agent_llm_model_calls.
    Optionally scoped to one knowledge_domain. Never touches queued/running
    tasks, prompt templates, or kb_* tables. dry_run=true (the default) only
    returns row-count estimates. Time-budgeted; returns truncated=true +
    remaining counts when the budget runs out — re-invoke to continue.

    Fail-closed auth: requires BOTH a valid X-Internal-Auth (the control
    plane strips client-forged copies and injects the real secret only for
    authenticated users) and X-KB-Role=admin. Direct calls missing either
    are rejected — this endpoint permanently deletes data.
    """
    _require_internal_auth(request)
    if request.headers.get("x-kb-role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")

    from llm_service.runtime.cleanup import run_cleanup

    result = await run_cleanup(
        request.app.state.db,
        retention_days=body.retention_days,
        dry_run=body.dry_run,
        knowledge_domain=body.knowledge_domain,
    )
    deleted_or_estimates = result.get("deleted") or result.get("estimates") or {}
    logger.info(
        "cleanup retention_days=%s domain=%s dry_run=%s -> tasks=%s truncated=%s",
        body.retention_days,
        body.knowledge_domain or "ALL",
        body.dry_run,
        deleted_or_estimates.get("tasks"),
        result.get("truncated"),
    )
    return {"success": True, "data": result}
