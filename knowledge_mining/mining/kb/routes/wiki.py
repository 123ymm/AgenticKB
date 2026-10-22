"""Wiki 制品 REST 路由 —— /api/artifacts/{artifact_id}/wiki/*（63号 §5.5）。

六端点：pages 清单 / page 单页读·改·删 / links 出入边 / status units 进度。
业务与权限全部在 WikiService（见 services/wiki_service.py）。

网关铁律：page_key 走 URL path 一律 UTF-8 hex 传输（raw path 含 % 即 400）——
路由收 hex 先 decode_page_key（非法 hex → 422 invalid_page_key）；links 的
page_key 是明文 query 参数（query 不经网关 path 校验）。

权限铁律：rev0（设计态）对 runtime 角色不可见——draft=true 或显式 revision
一律 design 角色（viewer → 403，对齐表格 diff/列定义模式）；DELETE 锁令牌随
body（对齐表格 RowDeleteReq 模式）。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import WikiError, decode_page_key
from knowledge_mining.mining.kb.deps import get_wiki_service
from knowledge_mining.mining.kb.routes.artifacts import (
    _map_error as _map_artifact_error,
)
from knowledge_mining.mining.kb.services.artifact_service import ArtifactError

router = APIRouter(prefix="/api/artifacts", tags=["wiki-artifacts"])


# ----------------------------------------------------------------- models

class WikiPagePut(BaseModel):
    """PUT 单页（创建/更新）。

    page_key 缺省 = 后端 normalize_page_key(title)（新建流前端只传 title）；
    编辑流显式传既有 page_key 稳定寻址（title 改名不漂移）；显式 page_key 与
    path 键不同 = 改名流（63号 §3.1：改名=新页+旧页删，服务不自动迁移）。
    """
    title: str = Field(min_length=1, max_length=200)
    page_type: Literal["topic", "contradiction", "open_question", "glossary"] \
        = "topic"
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    body_md: str = ""
    page_key: str | None = Field(default=None, min_length=1, max_length=200)
    provenance: list[Any] | None = None
    flags: dict[str, Any] | None = None
    ordinal: int | None = None
    expected_updated_at: str | None = None


class WikiPageDeleteReq(BaseModel):
    expected_updated_at: str | None = None


# ---------------------------------------------------------------- helpers

#: WikiError.code → HTTP 状态（"invalid_frontmatter:字段" 按前缀归并）。
#: 发布族门禁（wiki_no_pages/no_pending_changes/usage_note_required）不在本表
#: ——publish 端点在 artifacts 路由（lifecycle 已翻译为 ArtifactError 族，
#: 状态码真源 = routes/artifacts._map_error）。
_WIKI_HTTP_STATUS: dict[str, int] = {
    "page_not_found": 404,
    "artifact_not_found": 404,
    "stale_artifact": 409,
    "page_key_conflict": 409,
    "invalid_page_key": 422,
    "invalid_page_type": 422,
    "invalid_frontmatter": 422,
    "invalid_title": 422,
}

#: 409 信封文案（对齐 routes/artifacts 的 {code, message} 形态）。
_WIKI_MESSAGES: dict[str, str] = {
    "stale_artifact": "制品已被他人修改，请刷新后重试",
    "page_key_conflict": "目标 page_key 已被其他页占用——改名=新页+旧页删，"
                         "请先处理占用页",
}


def _map_wiki_error(exc: WikiError) -> HTTPException:
    code = str(exc.code).split(":", 1)[0]
    status = _WIKI_HTTP_STATUS.get(code, 400)
    if code in _WIKI_MESSAGES:
        return HTTPException(status, {"code": code,
                                      "message": _WIKI_MESSAGES[code]})
    return HTTPException(status, str(exc.code))


def _map_error(exc: Exception) -> HTTPException:
    """WikiError 走本表；ArtifactError（权限/新鲜度族）复用 artifacts 路由
    的翻译（同 prefix 同信封）。"""
    if isinstance(exc, WikiError):
        return _map_wiki_error(exc)
    if isinstance(exc, ArtifactError):
        return _map_artifact_error(exc)
    return HTTPException(500, str(exc))


def _decode_hex(page_key_hex: str) -> str:
    """hex path 参数 → page_key（非法 hex/空串 → 422 invalid_page_key）。"""
    try:
        return decode_page_key(page_key_hex)
    except WikiError as exc:
        raise _map_wiki_error(exc) from None


# --------------------------------------------------------------- endpoints

@router.get("/{artifact_id}/wiki/pages")
async def list_pages(
    artifact_id: str,
    draft: bool = Query(default=False),
    revision: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        return {"pages": await svc.list_pages(
            user=user, artifact_id=artifact_id,
            draft=draft, revision=revision)}
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/wiki/page/{page_key_hex}")
async def get_page(
    artifact_id: str,
    page_key_hex: str,
    draft: bool = Query(default=False),
    revision: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    page_key = _decode_hex(page_key_hex)
    try:
        return await svc.get_page(
            user=user, artifact_id=artifact_id, page_key=page_key,
            draft=draft, revision=revision)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.put("/{artifact_id}/wiki/page/{page_key_hex}")
async def put_page(
    artifact_id: str,
    page_key_hex: str,
    body: WikiPagePut,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    address_key = _decode_hex(page_key_hex)
    try:
        return await svc.upsert_page(
            user=user, artifact_id=artifact_id,
            page_key=body.page_key, address_key=address_key,
            title=body.title, page_type=body.page_type,
            frontmatter=body.frontmatter, body_md=body.body_md,
            provenance=body.provenance, flags=body.flags,
            ordinal=body.ordinal,
            expected_updated_at=body.expected_updated_at,
        )
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.delete("/{artifact_id}/wiki/page/{page_key_hex}")
async def delete_page(
    artifact_id: str,
    page_key_hex: str,
    body: WikiPageDeleteReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    page_key = _decode_hex(page_key_hex)
    try:
        await svc.delete_page(
            user=user, artifact_id=artifact_id, page_key=page_key,
            expected_updated_at=(body or WikiPageDeleteReq()).expected_updated_at,
        )
        return {"deleted": True}
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/wiki/links")
async def list_links(
    artifact_id: str,
    page_key: str | None = Query(default=None),
    direction: Literal["out", "in"] = Query(default="out"),
    draft: bool = Query(default=False),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        return {"links": await svc.list_links(
            user=user, artifact_id=artifact_id, page_key=page_key,
            direction=direction, draft=draft)}
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/wiki/status")
async def wiki_status(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        return await svc.status(user=user, artifact_id=artifact_id)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None
