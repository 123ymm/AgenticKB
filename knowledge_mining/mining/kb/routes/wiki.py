"""Wiki 制品 REST 路由 —— /api/artifacts/{artifact_id}/wiki/*（63号 §5.5）。

十七端点：toc 目录读·写（66号批次1 Task 1.4：runtime/revision 快照读 +
draft 行读 + PUT 整体替换，行级乐观锁）/ pages 清单 / page 单页读·改·删 /
links 出入边 / status units 进度 / search 三通道检索转发（批次4 Task 4：
ROLE_RUNTIME_READ 前置 → httpx 转发 serving internal
/artifact-wiki-search，响应原样透传）/ sources 来源绑定三端点（批次3：
GET/PUT/DELETE——绑定 kb 目录 + 脏标记观测）/ compile 编译四端点（批次3
Task 3：启动/进度/目录确认/取消——业务在 CompileService，见
services/wiki_compile_service.py）+ unit-source 回原文薄端点（66号批次5
Task 5.2 Q01：unit_id → 原文截读 500 字，双重权限在服务层）+ reproject
投影修复端点（66号批次5 Task 5.3 Q01：editor+ 幂等重入投影，不动正文）。
业务与权限全部在服务层。

网关铁律：page_key 走 URL path 一律 UTF-8 hex 传输（raw path 含 % 即 400）——
路由收 hex 先 decode_page_key（非法 hex → 422 invalid_page_key）；links 的
page_key 是明文 query 参数（query 不经网关 path 校验）。

权限铁律：rev0（设计态）对 runtime 角色不可见——draft=true 或显式 revision
一律 design 角色（viewer → 403，对齐表格 diff/列定义模式）；DELETE 锁令牌随
body（对齐表格 RowDeleteReq 模式）。toc PUT 的锁令牌是 **toc 行** 的
updated_at（行已存在时必传——铁律见 wiki_service.save_toc）。
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


class WikiTocPut(BaseModel):
    """PUT 目录草稿（66号批次1 Task 1.4）：page_key 级目录整体替换。
    sections 浅形态 pydantic 拦（非列表 → 422 loc=body.sections，先例=页
    PUT 的 title 缺失）；深层畸形（节/键形态）与页键存在性在 service
    （invalid_toc_shape / toc_unknown_page_keys 带具体细节，先例=
    invalid_frontmatter 的裸 code:detail 透传）。expected_updated_at 是
    **toc 行**的乐观锁令牌（行已存在时必传，缺 → 409 stale_artifact；
    首写免令牌——铁律见 wiki_service.save_toc）。"""
    sections: list[Any]
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
    # 66号批次1 Task 1.4：目录三码——rev0 未发布反探测 / 快照版本不存在 /
    # 目录形态或页键校验（后两者带细节后缀，裸 detail 透传，先例=
    # invalid_frontmatter）。
    "toc_not_found": 404,
    "revision_not_found": 404,
    # Q01 回原文（66号批次5 Task 5.2）：非本制品 provenance / 原文退役 /
    # 源库无读权单码 404 反探测（不区分暴露存在性）。
    "unit_not_found": 404,
    "invalid_toc_shape": 422,
    "toc_unknown_page_keys": 422,
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
    "toc_not_found": "该制品尚无已发布目录（先发布或切草稿视图编辑）",
    # 质量审查 M-2：无后缀 404 码统一走 {code,message} 信封（与 toc_not_found
    # 同面）；带细节后缀的 422 码（invalid_toc_shape/toc_unknown_page_keys）
    # 维持裸 detail 透传（先例=invalid_frontmatter）。
    "revision_not_found": "该版本不存在（无对应发布快照）",
    # Q01：与「不存在」同面——不向无源库读权者泄露 unit 存在性。
    "unit_not_found": "来源单元不存在或无权限访问",
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

@router.get("/{artifact_id}/wiki/toc")
async def get_toc(
    artifact_id: str,
    draft: bool = Query(default=False),
    revision: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    """目录读（66号批次1 Task 1.4，draft/revision 路由对齐 pages）：缺省
    runtime 读 current 快照 sections；?draft=true 读 toc 行本身（附
    pages_total/updated_at）；?revision=N 读历史快照。"""
    try:
        return await svc.toc(
            user=user, artifact_id=artifact_id,
            draft=draft, revision=revision)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.put("/{artifact_id}/wiki/toc")
async def put_toc(
    artifact_id: str,
    body: WikiTocPut,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    """PUT 目录草稿（整体替换）。返回完整 toc 行（updated_at=下次编辑的
    乐观锁令牌）。"""
    try:
        return await svc.save_toc(
            user=user, artifact_id=artifact_id,
            toc={"sections": body.sections},
            expected_updated_at=body.expected_updated_at,
        )
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.get("/{artifact_id}/wiki/pages")
async def list_pages(
    artifact_id: str,
    draft: bool = Query(default=False),
    revision: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    """页面清单（轻量）。信封回填 revision=实际读取版本（66号 D03.1：图谱
    pages/links 两请求混版判定——两请求间发生发布可检出）。"""
    try:
        resolved = await svc.resolve_view_revision(
            user=user, artifact_id=artifact_id,
            draft=draft, revision=revision)
        return {"revision": resolved, "pages": await svc.list_pages(
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
    """出边/入边。信封回填 revision=实际读取版本（口径同 pages 端点——66号
    D03.1 混版判定；links 不支持显式 revision 查询参数，随 draft/缺省视图）。"""
    try:
        resolved = await svc.resolve_view_revision(
            user=user, artifact_id=artifact_id, draft=draft)
        return {"revision": resolved, "links": await svc.list_links(
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


# ------------------------------------------------- Q01 回原文（66号批次5 Task 5.2）
# 阅读器点 [[src:unit_id]] 引用 → 来源单元原文截读（前 500 字）。薄端点：
# 视图 revision 口径对齐单页读（draft/显式 revision → design；缺省 runtime），
# 业务与双重权限（制品读取 + 源 kb 可读二次校验）在 WikiService.unit_source。

@router.get("/{artifact_id}/wiki/unit-source")
async def unit_source(
    artifact_id: str,
    unit_id: str = Query(min_length=1, max_length=200),
    draft: bool = Query(default=False),
    revision: int | None = Query(default=None, ge=1),
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    """Q01 回原文：unit_id（=retrieval units v2 representation_id，即页
    provenance unit_ids 元素）→ {unit_id, content_text(截500字), kb_id,
    snapshot_id, section_ref}。404 反探测单码：非本制品 provenance / 原文
    退役 / 无源库读权不区分（unit_not_found）。"""
    try:
        return await svc.unit_source(
            user=user, artifact_id=artifact_id, unit_id=unit_id,
            draft=draft, revision=revision)
    except (ArtifactError, WikiError) as exc:
        raise _map_error(exc) from None


@router.post("/{artifact_id}/wiki/reproject")
async def reproject(
    artifact_id: str,
    user: dict[str, Any] = Depends(current_user),
    svc: Any = Depends(get_wiki_service),
):
    """Q01 投影修复（66号批次5 Task 5.3）：editor+，对 current_revision
    幂等重入投影——failed>0 / 投影缺失才重建（reprojected=true，embedding
    随之重拉起），健康态 no-op；不改正文、不产伪发布版本（66号 §14）。
    返回 {artifact_id, revision, reprojected}。"""
    try:
        return await svc.reproject(user=user, artifact_id=artifact_id)
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
