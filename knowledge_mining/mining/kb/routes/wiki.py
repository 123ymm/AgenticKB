"""Wiki 制品 REST 路由 —— /api/artifacts/{artifact_id}/wiki/*（63号 §5.5）。

十四端点：pages 清单 / page 单页读·改·删 / links 出入边 / status units 进度 /
search 三通道检索转发（批次4 Task 4：ROLE_RUNTIME_READ 前置 → httpx 转发
serving internal /artifact-wiki-search，响应原样透传）/
sources 来源绑定三端点（批次3：GET/PUT/DELETE——绑定 kb 目录 + 脏标记观测）/
compile 编译四端点（批次3 Task 3：启动/进度/目录确认/取消——业务在
CompileService，见 services/wiki_compile_service.py）。
业务与权限全部在服务层。

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
from pydantic import BaseModel, Field, field_validator

from knowledge_mining.mining.kb.auth import current_user
from knowledge_mining.mining.kb.db_wiki import WikiError, decode_page_key
from knowledge_mining.mining.kb.deps import (
    get_compile_service,
    get_wiki_search_client,
    get_wiki_service,
)
from knowledge_mining.mining.kb.routes.artifacts import (
    _map_error as _map_artifact_error,
)
from knowledge_mining.mining.kb.services.artifact_service import ArtifactError
from knowledge_mining.mining.kb.services.wiki_compile_service import MAX_UNITS

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


class WikiSourcePut(BaseModel):
    """PUT 绑定来源（批次3）：kb_id（TEXT id，全 ASCII；路由层先 strip——
    纯空白过不了 min_length）；directories 空数组=全库（scope 契约见批次3
    决策6；量上限在 service 层 422 invalid_scope）；expected_updated_at
    乐观锁令牌——绑定写推进令牌（touch），编辑器持有的旧令牌随后保存会
    409，属预期失效。"""
    kb_id: str = Field(min_length=1, max_length=200)
    directories: list[str] = Field(default_factory=list)
    expected_updated_at: str | None = None

    @field_validator("kb_id", mode="before")
    @classmethod
    def _strip_kb_id(cls, value: Any) -> Any:
        """路由层 strip 空白（审查硬化："  " 不再过 min_length；非 str 原样
        交给 pydantic 报类型错）。"""
        return value.strip() if isinstance(value, str) else value


class WikiSourceDeleteReq(BaseModel):
    expected_updated_at: str | None = None


class WikiCompileStartReq(BaseModel):
    """POST 编译启动（批次3 Task 3）：expected_updated_at 乐观锁快检（编译
    页写在事务内自行推进令牌——start 后前端须刷新制品详情）。"""
    expected_updated_at: str | None = None


class WikiTocPageHint(BaseModel):
    """确认目录的页提示（用户可编辑增删）。cluster_ids 是提示性来源簇，
    指向不存在簇 → 该页无取材（引用校验兜底）。"""
    title: str = Field(min_length=1, max_length=200)
    cluster_ids: list[str] = Field(default_factory=list)
    page_type: str = "topic"


class WikiTocSection(BaseModel):
    section_title: str = Field(min_length=1, max_length=200)
    page_hints: list[WikiTocPageHint] = Field(default_factory=list)


class WikiCompileTocReq(BaseModel):
    """POST 目录确认：toc 为用户（前端目录树编辑后）完整版本；展开页数
    >200 → 422 too_many_pages。"""
    toc: list[WikiTocSection]
    expected_updated_at: str | None = None


class WikiCompileCancelReq(BaseModel):
    """POST 取消：expected_updated_at 不参与校验（取消是安全操作，且编译
    页写持续推令牌——校验必假阳性；字段保留对齐其余编译端点信封）。"""
    expected_updated_at: str | None = None


# ---------------------------------------------------------------- helpers

#: WikiError.code → HTTP 状态（"invalid_frontmatter:字段" 按前缀归并）。
#: 发布族门禁（wiki_no_pages/no_pending_changes/usage_note_required）不在本表
#: ——publish 端点在 artifacts 路由（lifecycle 已翻译为 ArtifactError 族，
#: 状态码真源 = routes/artifacts._map_error）。
_WIKI_HTTP_STATUS: dict[str, int] = {
    "page_not_found": 404,
    "artifact_not_found": 404,
    "source_not_found": 404,
    "compile_not_found": 404,
    "stale_artifact": 409,
    "page_key_conflict": 409,
    "compile_in_progress": 409,
    "invalid_compile_state": 409,
    "invalid_page_key": 422,
    "invalid_page_type": 422,
    "invalid_frontmatter": 422,
    "invalid_title": 422,
    "invalid_scope": 422,
    "invalid_toc": 422,
    "invalid_query": 422,
    "no_sources": 422,
    "too_many_units": 422,
    "too_many_pages": 422,
    # 批次4 Task 4：serving 转发故障族——网关类基础设施错误统一 502
    # （WikiSearchClient 状态翻译产出，见 kb/wiki_search_client.py）。
    "serving_unavailable": 502,
    "serving_timeout": 502,
    "serving_error": 502,
}

#: 409/404 信封文案（对齐 routes/artifacts 的 {code, message} 形态；审查 F5
#: 补 compile_not_found——404 也走信封，与其他编译码一致）。
_WIKI_MESSAGES: dict[str, str] = {
    "stale_artifact": "制品已被他人修改，请刷新后重试",
    "page_key_conflict": "目标 page_key 已被其他页占用——改名=新页+旧页删，"
                         "请先处理占用页",
    "compile_in_progress": "该制品已有编译在飞（preparing/awaiting_toc/"
                           "writing），请先等待或取消",
    "invalid_compile_state": "编译状态不允许该操作（如确认目录仅限 "
                             "awaiting_toc 态）",
    "compile_not_found": "该制品尚无编译批次（先启动编译）",
    "no_sources": "未绑定任何知识库来源，请先在来源面板绑定",
    # 上限数从服务常量派生（审查 Minor：文案与 MAX_UNITS 不双源漂移）。
    "too_many_units": f"绑定范围内检索单元超上限（{MAX_UNITS}），"
                      "请收窄目录范围",
    "too_many_pages": "确认目录展开页数超上限（200），请删减后再确认",
    "invalid_toc": "目录结构不合法（需非空节列表，每节含 title 非空页提示）",
    # 批次4 Task 4：检索转发族文案（invalid_query 与 serving isBlank 同口径；
    # 502 三码为 serving 侧故障的网关化中文提示）。
    "invalid_query": "检索参数非法：q 不能为空白，mode 仅支持 fusion/dense/keyword",
    "serving_unavailable": "检索服务暂不可用（内部通道未配置或网络不可达），请稍后重试",
    "serving_timeout": "检索服务响应超时，请稍后重试",
    "serving_error": "检索服务内部错误，请稍后重试",
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


# ------------------------------------------------- 检索转发（63号批次4 Task 4）
# web 阅读器搜索框专用：ROLE_RUNTIME_READ（viewer 可搜；非成员 404 反探测
# 在 mining 侧即拦）→ httpx 转发 serving internal 三通道检索
# （dense ANN/keyword/fusion，业务整段在 Java ArtifactWikiSearchService）。
# 响应 serving wiki_search 视图原样透传；serving 故障族 → 502（码表见上）。

@router.get("/{artifact_id}/wiki/search")
async def wiki_search(
    artifact_id: str,
    q: str = Query(min_length=1),
    mode: Literal["fusion", "dense", "keyword"] = Query(default="fusion"),
    top_k: int = Query(default=10, ge=1, le=50),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
    forward: Any = Depends(get_wiki_search_client),
):
    """wiki 三通道检索转发（不做缓存）。响应
    {view, revision, degraded_dense, results:[{page_key,title,unit_key,
    snippet,score,channels}]}——serving 契约 snake_case 原样。"""
    try:
        return await svc.search(
            user=user, artifact_id=artifact_id, q=q, mode=mode,
            top_k=top_k, forward=forward)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


# ------------------------------------------------- 来源绑定（63号批次3 Task 1）
# kb_id 是 TEXT id（全 ASCII），走 path 无网关编码问题（区别于需 hex 的
# page_key）；DELETE 锁令牌随 body（对齐 WikiPageDeleteReq 模式）。

@router.get("/{artifact_id}/wiki/sources")
async def list_sources(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        return {"sources": await svc.list_sources(
            user=user, artifact_id=artifact_id)}
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.put("/{artifact_id}/wiki/sources")
async def put_source(
    artifact_id: str,
    body: WikiSourcePut,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        return await svc.upsert_source(
            user=user, artifact_id=artifact_id, kb_id=body.kb_id,
            directories=body.directories,
            expected_updated_at=body.expected_updated_at,
        )
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.delete("/{artifact_id}/wiki/sources/{kb_id}")
async def delete_source(
    artifact_id: str,
    kb_id: str,
    body: WikiSourceDeleteReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    try:
        await svc.delete_source(
            user=user, artifact_id=artifact_id, kb_id=kb_id,
            expected_updated_at=(body or WikiSourceDeleteReq()).expected_updated_at,
        )
        return {"deleted": True}
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


# ------------------------------------------------- 编译管线（63号批次3 Task 3）
# 全部 design 角色（status 亦然——编译过程含未发布内容，runtime 不可见）。

@router.post("/{artifact_id}/wiki/compile")
async def start_compile(
    artifact_id: str,
    body: WikiCompileStartReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_compile_service),
):
    """启动编译（守卫事务查在飞/量级护栏/建批 → 后台 prepare）。返回
    {batch_id, state}。"""
    try:
        return await svc.start(
            user=user, artifact_id=artifact_id,
            expected_updated_at=(body or WikiCompileStartReq()).expected_updated_at,
        )
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/wiki/compile/status")
async def compile_status(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_compile_service),
):
    """编译进度：{batch_id, state, progress, stats, conflicts, toc(
    awaiting_toc 时), error}。"""
    try:
        return await svc.status(user=user, artifact_id=artifact_id)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/wiki/compile/toc")
async def confirm_toc(
    artifact_id: str,
    body: WikiCompileTocReq,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_compile_service),
):
    """确认（可编辑）目录 → writing。返回 {state}。"""
    try:
        return await svc.confirm_toc(
            user=user, artifact_id=artifact_id,
            toc=[section.model_dump() for section in body.toc],
            expected_updated_at=body.expected_updated_at,
        )
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/wiki/compile/cancel")
async def cancel_compile(
    artifact_id: str,
    body: WikiCompileCancelReq | None = None,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_compile_service),
):
    """取消在飞编译（awaiting_toc 即时生效；writing 批边界停止）。返回
    {state}。"""
    try:
        return await svc.cancel(user=user, artifact_id=artifact_id)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None
