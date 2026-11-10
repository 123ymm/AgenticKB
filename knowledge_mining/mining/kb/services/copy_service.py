"""KbCopyService — 整库拷贝业务编排（docs/kb-copy-design.md v1.1）。

语义：**快照血缘共享**，不直建 Build——拷文件 + 目录树 + snapshot links；
拷完文档均为 uploaded、不可检索；新库自选范式触发挖掘，指纹命中自动复用
既有快照（零 LLM 重嵌），失配则全量重算。

配置范围：仅 visibility（受防放大规则约束）；范式 / 成员 / 制品一律不复制。

错误族复用 kb_service（NotFound/Forbidden/Duplicate/DomainNotBound），新增
SourceEmpty / SourceTooLarge（422）——route 层 _map_error 统一译 HTTP。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.errors import UniqueViolation

from knowledge_mining.mining.kb.db import KbDB, UserDeletionConflict
from knowledge_mining.mining.kb.services.kb_service import (
    DomainNotBound,
    Duplicate,
    Forbidden,
    InvalidVisibility,
    KbError,
    NotFound,
    normalize_kb_name,
)

# 同步单事务的文档数上限（设计 §3）：超过走导出/导入通道。
MAX_COPY_DOCUMENTS = 5000

_VALID_VISIBILITY = {"private", "public"}

# 派生名后缀「-副本-YYYYMMDD」的固定长度（12 字符），截源名时保后缀完整。
_COPY_SUFFIX_LEN = len("-副本-20261023")


class SourceEmpty(KbError):
    """源库无 active 文档——拷贝没有意义，引导直接新建。"""


class SourceTooLarge(KbError):
    """源库文档数超过同步事务上限——引导导出/导入通道。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_copy_name(source_name: str) -> str:
    """缺省名派生：``<源名>-副本-YYYYMMDD``；超长先截源名，后缀保持完整。"""
    suffix = f"-副本-{datetime.now(timezone.utc):%Y%m%d}"
    stem = source_name
    if len(stem) + len(suffix) > 80:
        stem = stem[: 80 - len(suffix)].rstrip()
    return stem + suffix


class CopyService:
    def __init__(self, db: KbDB) -> None:
        self._db = db

    async def copy_kb(
        self, *, kb_id: str, actor_id: str, overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """整库拷贝。``overrides`` 为请求体中**显式提供**的字段
        （new_name / visibility / description）——区分「未传」与「显式置 null」。"""
        kb = await self._db.get_kb(kb_id)
        if kb is None:
            raise NotFound(kb_id)
        # 读权限：看不到（他人 private / 软删）统一 404，不泄露存在性。
        if not await self._db.is_visible(kb_id=kb_id, user_id=actor_id):
            raise NotFound(kb_id)
        # 建库资格：副本落在源库同域，操作者须可在该域建库。
        if not await self._db.can_create_in_domain(
            user_id=actor_id, domain=kb["domain"],
        ):
            raise DomainNotBound(kb["domain"])

        # ── 可见性（防放大规则，设计 §3）─────────────────────────────
        # public 仅当操作者是源库 owner 或 site-admin；member 拷他人 private
        # 库后设 public = 把他人私有知识域内公开，必须挡。
        privileged = kb["owner_id"] == actor_id or await self._is_site_admin(actor_id)
        if "visibility" in overrides:
            visibility = overrides["visibility"]
            if visibility not in _VALID_VISIBILITY:
                raise InvalidVisibility(
                    visibility if visibility is not None else "null"
                )
            if visibility == "public" and not privileged:
                raise Forbidden(
                    "visibility_not_allowed: 仅源库 owner / 站点管理员"
                    "可将副本设为 public"
                )
        else:
            # 未显式指定：继承源库值；非特权继承 public 时强制降为 private。
            visibility = kb.get("visibility") if privileged else "private"
            if visibility not in _VALID_VISIBILITY:
                visibility = "private"

        # ── 描述（缺省继承源库；显式 null = 清空）────────────────────
        if "description" in overrides:
            description = overrides["description"]
        else:
            description = kb.get("description")

        # ── 名称（缺省派生 <源名>-副本-YYYYMMDD）─────────────────────
        if overrides.get("new_name") is not None:
            name = normalize_kb_name(overrides["new_name"])
        else:
            name = normalize_kb_name(_derive_copy_name(kb["name"]))

        # ── 文档数闸门（空库 / 超同步上限 → 422）──────────────────────
        n_docs = await self._db.count_active_documents_in_kb(kb_id)
        if n_docs == 0:
            raise SourceEmpty(
                "拷贝失败：源知识库没有可用文档（空库或全部已删除）"
            )
        if n_docs > MAX_COPY_DOCUMENTS:
            raise SourceTooLarge(
                f"拷贝失败：源知识库文档数 {n_docs} 超过同步拷贝上限 "
                f"{MAX_COPY_DOCUMENTS}，请使用导出/导入通道"
            )

        copy_source = {
            "kb_id": kb_id,
            "kb_name": kb["name"],
            "copied_at": _utcnow(),
        }
        try:
            result = await self._db.copy_kb(
                source_kb_id=kb_id, operator_id=actor_id, name=name,
                visibility=visibility, description=description,
                copy_source=copy_source,
            )
        except UserDeletionConflict as exc:
            # 事务内复核操作者已失效（禁用/删除/解绑）——同 create_kb 译法。
            raise DomainNotBound(kb["domain"]) from exc
        except UniqueViolation as exc:
            raise Duplicate(
                f"拷贝失败：该知识域下你名下已有同名的活跃知识库「{name}」"
                "（换一个名称，或删除/重命名现有库后重试）"
            ) from exc
        if result is None:
            # 校验后、锁前的竞态：源库已被软删/不存在——不泄露存在性。
            raise NotFound(kb_id)

        new_kb = result["kb"]
        new_kb["copy_source"] = copy_source
        new_kb["copy_stats"] = result["stats"]
        new_kb["next_step_hint"] = "select_mining_paradigm"
        # 与 GET /api/kb/{id} 响应形状对齐（readiness 派生失败不阻塞）。
        try:
            new_kb["readiness"] = await self._db.get_kb_readiness(new_kb["id"])
        except Exception:  # pragma: no cover - 缺表等环境问题
            new_kb["readiness"] = None
        return new_kb

    async def _is_site_admin(self, user_id: str) -> bool:
        user = await self._db.get_user(user_id)
        return bool(user) and user.get("site_role") == "admin"
