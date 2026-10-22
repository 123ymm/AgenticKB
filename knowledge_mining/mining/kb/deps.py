"""FastAPI dependencies for KB routes."""
from __future__ import annotations

from fastapi import Request

from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.services.kb_service import KbService


async def get_kb_db(request: Request) -> KbDB:
    return KbDB(request.app.state.pg_pool)


async def get_kb_service(request: Request) -> KbService:
    return KbService(KbDB(request.app.state.pg_pool))


async def get_document_service(request: Request) -> DocumentService:
    from knowledge_mining.mining.kb.services.document_service import DocumentService
    from knowledge_mining.mining.file_management.repositories_pg import (
        PgStorageObjectRepository,
    )

    config = request.app.state.object_store_config
    return DocumentService(
        KbDB(request.app.state.pg_pool),
        object_store=request.app.state.object_store,
        storage_objects=PgStorageObjectRepository(request.app.state.pg_pool),
        source_bucket=f"{config.bucket_prefix}source",
    )


async def get_folder_service(request: Request):
    from knowledge_mining.mining.kb.services.folder_service import FolderService
    return FolderService(KbDB(request.app.state.pg_pool))


async def get_user_service(request: Request) -> UserService:
    from knowledge_mining.mining.kb.services.user_service import UserService
    return UserService(KbDB(request.app.state.pg_pool))


def get_purge_service(request: Request) -> "Any":
    """统一硬删管线（2026-09-16 删除体系：pool + 对象存储注入）."""
    from knowledge_mining.mining.kb.services.purge_service import PurgeService
    return PurgeService(request.app.state.pg_pool,
                        getattr(request.app.state, "object_store", None))


async def get_artifact_db(request: Request) -> "Any":
    """61号：知识制品六表仓储。"""
    from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
    return ArtifactDB(request.app.state.pg_pool)


async def get_artifact_service(request: Request) -> "Any":
    """61号：制品业务层（权限矩阵/生命周期/乐观锁）。63号批次2：注入 wiki
    生命周期协作者（kind=wiki 四分支；单参构造 wiki=None 兼容性保留——
    表格-only 部署/测试不受影响）。"""
    from knowledge_mining.mining.kb.services.artifact_service import ArtifactService
    return ArtifactService(await get_artifact_db(request),
                           wiki=_wiki_lifecycle(request))


# --------------------------------------------------- wiki（63号批次2 Task 3）

#: LlmTaskClient 模块级单例（httpx.Client 连接池跨请求复用——每请求新建
#: 不 close 会泄漏连接池；同步阻塞客户端，仅经 asyncio.to_thread 调用）。
_TASK_CLIENT_SINGLETON: "Any" = None


def _get_task_client() -> "Any":
    global _TASK_CLIENT_SINGLETON
    if _TASK_CLIENT_SINGLETON is None:
        from knowledge_mining.mining.infra.llm_task_client import LlmTaskClient
        from knowledge_mining.mining.infra.mining_config import MiningConfig
        # 配置来源照 api/app.py:214-215 模式（启动期 control_plane 已缓存）。
        _TASK_CLIENT_SINGLETON = LlmTaskClient(
            base_url=MiningConfig().llm_service_url)
    return _TASK_CLIENT_SINGLETON


def _wiki_lifecycle(request: Request) -> "Any":
    """request.state 缓存——同一请求内 get_artifact_service 与 get_wiki_service
    共享同一 WikiLifecycle 实例（无环装配：lifecycle 不回头引用 service；
    全局防重入由服务模块级 _EMBED_INFLIGHT 兜底）。"""
    lifecycle = getattr(request.state, "wiki_lifecycle", None)
    if lifecycle is None:
        from knowledge_mining.mining.kb.db_wiki import WikiDB
        from knowledge_mining.mining.kb.services.wiki_service import WikiLifecycle
        lifecycle = WikiLifecycle(
            WikiDB(request.app.state.pg_pool), _get_task_client())
        request.state.wiki_lifecycle = lifecycle
    return lifecycle


async def get_wiki_db(request: Request) -> "Any":
    """63号：wiki 专有三表仓储（pages/links/units）。"""
    from knowledge_mining.mining.kb.db_wiki import WikiDB
    return WikiDB(request.app.state.pg_pool)


async def get_wiki_service(request: Request) -> "Any":
    """63号：wiki 页面编辑编排（权限复用 ArtifactService；lifecycle 同实例）。
    批次3：加注入编译取材域 CompileInputsDB（sources/指纹/批次——来源绑定三
    端点与编译管线数据源）。"""
    from knowledge_mining.mining.kb.db_compile_inputs import CompileInputsDB
    from knowledge_mining.mining.kb.services.artifact_service import ArtifactService
    from knowledge_mining.mining.kb.services.wiki_service import WikiService
    return WikiService(
        artifact_service=ArtifactService(await get_artifact_db(request),
                                         wiki=_wiki_lifecycle(request)),
        wdb=await get_wiki_db(request),
        lifecycle=_wiki_lifecycle(request),
        inputs_db=CompileInputsDB(request.app.state.pg_pool),
    )


# --------------------------------------------------- wiki 编译（63号批次3 Task 3）

#: LLMServiceAsyncGenerationClient 模块级单例（内部持 LlmTaskClient 的
#: httpx.Client 连接池——每请求新建泄漏；同步阻塞 execute，编译服务内以
#: asyncio.to_thread 调用）。
_GENERATION_CLIENT_SINGLETON: "Any" = None


def _get_generation_client() -> "Any":
    global _GENERATION_CLIENT_SINGLETON
    if _GENERATION_CLIENT_SINGLETON is None:
        from knowledge_mining.mining.infra.mining_config import MiningConfig
        from knowledge_mining.mining.retrieval_projection.llm_generation import (
            LLMServiceAsyncGenerationClient,
        )
        # 构造参数逐字段取自 MiningConfig 的 llm_async_* 域（chat 等待用
        # llm_async_chat_wait_timeout——页写作单次调用语义，非 embedding 批
        # 量的 llm_async_wait_timeout）。
        cfg = MiningConfig()
        _GENERATION_CLIENT_SINGLETON = LLMServiceAsyncGenerationClient(
            base_url=cfg.llm_service_url,
            poll_interval=cfg.llm_async_poll_interval,
            wait_timeout=cfg.llm_async_chat_wait_timeout,
            max_attempts=cfg.llm_async_max_attempts,
        )
    return _GENERATION_CLIENT_SINGLETON


async def get_compile_service(request: Request) -> "Any":
    """63号批次3：编译管线编排（CompileService）。wiki_service 复用
    get_wiki_service 装配（CompileService 持有它做 ROLE_DESIGN 前置与全量
    链接重算——wdb/inputs_db 各自轻构造，无共享可变态）。"""
    from knowledge_mining.mining.kb.db_compile_inputs import CompileInputsDB
    from knowledge_mining.mining.kb.services.wiki_compile_service import (
        CompileService,
    )
    return CompileService(
        wdb=await get_wiki_db(request),
        inputs_db=CompileInputsDB(request.app.state.pg_pool),
        generation_client=_get_generation_client(),
        wiki_service=await get_wiki_service(request),
    )


async def get_artifact_import_service(request: Request) -> "Any":
    """61号：Excel 三步导入向导（写路径经 ArtifactService 权限门禁）。"""
    from knowledge_mining.mining.kb.services.artifact_import_service import (
        ArtifactImportService,
    )
    return ArtifactImportService(await get_artifact_db(request))
