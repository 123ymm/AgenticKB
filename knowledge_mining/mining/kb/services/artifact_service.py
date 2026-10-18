"""ArtifactService — 知识制品业务层（61号 §五 权限矩阵 + §四 生命周期编排）。

职责：读/写权限 enforcement（反探测 404）+ 生命周期编排（ensure_draft 前置 /
发布门禁 / 乐观锁）+ 业务错误翻译。Route 层捕获错误族翻译 HTTP 状态。

权限口径（61号 §5.2 + 审查定稿）：
- site admin 全通；本域 domain admin 全通（以库为准，is_domain_admin 现查）。
- 其余按 kb_artifact_members 角色（owner_user_id 落库兜底为 owner）。
- 非成员一律 ArtifactNotFound（404 反探测，含同域非成员——不暴露存在性）。
- 成员但角色不足 → ArtifactForbidden（403）。viewer 不见设计态（rev0/diff/
  列定义），运行态（current_revision）与元信息可读。
- 成员管理与删除仅 owner（admin 旁路例外）。

user 对象形态 = auth.current_user 返回的 kb_users 行（id / site_role）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from knowledge_mining.mining.kb.db_artifacts import ArtifactError as ArtifactRepoError
from knowledge_mining.mining.kb.db_artifacts import _row_key_value

#: 运行态读（元信息/已发布行）：成员任一角色。
ROLE_RUNTIME_READ = ("owner", "editor", "viewer")
#: 设计态（rev0 行 / diff / 列定义 / 编辑 / 发布族）：owner+editor。
ROLE_DESIGN = ("owner", "editor")
#: 成员管理与删除：仅 owner（site/domain admin 由旁路放行）。
ROLE_OWNER_ONLY = ("owner",)

#: 允许直接 PATCH 的元信息字段白名单。
_META_FIELDS = {"title", "description", "usage_note"}

_MAX_TITLE_LENGTH = 200

#: codex二审批号⑦：普通编辑不得改主键列——改了 data 与 row_key 即脱钩
#: （行地址按主键值编码）。改主键必须走设置主键功能（整体重算行键）。
_PK_IMMUTABLE_MSG = (
    "primary_key_immutable:改主键值请用设置主键功能（重算行键）"
)


# ----------------------------------------------------------------- errors

class ArtifactError(Exception):
    """Base for artifact business errors."""


class ArtifactNotFound(ArtifactError):
    """404——制品不存在或请求者非成员（反探测统一口径）。"""


class ArtifactForbidden(ArtifactError):
    """403——成员但角色不足。"""


class UsageNoteRequired(ArtifactError):
    """422——发布必填 usage_note（SRA 实证：说明卡缺失等于没发布）。"""


class PrimaryKeyConflict(ArtifactError):
    """409——改主键后多行折到同一 row_key。"""


class StaleArtifact(ArtifactError):
    """409——乐观锁失配（expected_updated_at ≠ 库内 updated_at）。"""


class NoPendingChanges(ArtifactError):
    """409——干净态发布（draft_open=FALSE 且 current>0，无待发布变更）。"""


class InvalidOperation(ArtifactError):
    """400——状态机/参数非法（非法角色、行不存在、非法 revision 等）。"""


#: 仓储 ArtifactRepoError.code → 业务错误。
_REPO_CODE_TO_ERROR: dict[str, type[ArtifactError]] = {
    "usage_note_required": UsageNoteRequired,
    "primary_key_conflict": PrimaryKeyConflict,
    "artifact_not_found": ArtifactNotFound,
    "no_pending_changes": NoPendingChanges,
    "stale_artifact": StaleArtifact,
}


def _translate(exc: ArtifactRepoError) -> ArtifactError:
    mapped = _REPO_CODE_TO_ERROR.get(exc.code)
    if mapped is not None:
        return mapped(exc.code)
    return InvalidOperation(exc.code)


# ----------------------------------------------------------------- helpers

def _uid(user: dict[str, Any]) -> str:
    return str(user["id"])


def _is_site_admin(user: dict[str, Any]) -> bool:
    return user.get("site_role") == "admin"


def _parse_ts(value: Any) -> datetime:
    """ISO 字符串/datetime → aware datetime；非法 → InvalidOperation。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise InvalidOperation("invalid_expected_updated_at") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _check_freshness(
    artifact: dict[str, Any], expected_updated_at: Any,
) -> None:
    """乐观锁前置快检（体验友好）：全精度比较（codex二审批号⑥a——秒截断
    会把同秒不同微秒的旧 token 放行；PG TIMESTAMPTZ 与 isoformat 都是微秒
    精度，直接比）。并发权威在 db 层 _assert_expected_updated_at（FOR UPDATE
    原子校验+写+touch 同事务）。None=不校验。"""
    if expected_updated_at is None:
        return
    expected = _parse_ts(expected_updated_at)
    current = _parse_ts(artifact.get("updated_at"))
    if expected != current:
        raise StaleArtifact(str(artifact.get("artifact_id")))


def _expected_dt(expected_updated_at: Any) -> Any:
    """expected_updated_at → datetime（db 层原子比对用）；None 透传。
    格式非法早抛（_require_design_session 前置已拦过一次，这里兜直调路径）。"""
    return _parse_ts(expected_updated_at) if expected_updated_at is not None else None


# 单格/单值数值 cast（导入向导与手工编辑共用口径——artifact_import_service
# 从此处 import，保证 _cast_value 与 update_cell 校验语义同源）。

def _try_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _try_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _is_formula(value: Any) -> bool:
    """公式注入防御（对抗审查修复⑩，与 55号 user_import_service 同口径）：
    以 ``= + - @`` 开头的文本一律拒绝。定义在本模块、artifact_import_service
    反向 import——单向依赖（import→service），避免循环导入。"""
    return isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))


def _cast_cell(value: Any, column_type: str) -> tuple[bool, Any]:
    """单格写入的类型校验（§7.2 + 对抗审查修复⑫）：int/float/date/bool 设卡
    （date=fromisoformat 可解、bool=严格 true/false/1/0，与导入 _cast_value
    同源）；text/enum 不校验。空白串=合法置空。返回 (ok, casted)；
    ok=False 即类型错误（update_cell 置 null + conflicts 标记 /
    batch_assign strict 400）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return True, None
    if column_type == "int":
        casted = _try_int(value)
        return casted is not None, casted
    if column_type == "float":
        casted = _try_float(value)
        return casted is not None, casted
    if column_type == "date":
        if isinstance(value, datetime):
            return True, value.date().isoformat()
        if isinstance(value, date):
            return True, value.isoformat()
        if isinstance(value, str):
            try:
                return True, datetime.fromisoformat(value.strip()).date().isoformat()
            except ValueError:
                return False, value
        return False, value
    if column_type == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, int) and value in (0, 1):
            return True, bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return True, value.strip().lower() == "true"
        return False, value
    # text / enum：datetime 归一 ISO；字符串 strip；数值/布尔原样
    if isinstance(value, (datetime, date)):
        return True, value.isoformat()
    if isinstance(value, str):
        return True, value.strip()
    return True, value


# ----------------------------------------------------------------- service

class ArtifactService:
    def __init__(self, db: Any) -> None:
        self._db = db

    # ------------------------------------------------------------ 权限核心

    async def _assert_role(
        self, user: dict[str, Any], artifact_id: str, *allowed: str,
    ) -> tuple[dict[str, Any], str]:
        """返回 (artifact, role)。★非成员一律 ArtifactNotFound（含同域非成员，
        不暴露存在性）；成员角色不在 allowed → ArtifactForbidden。admin 旁路
        返回的角色（admin / domain_admin）满足任意 allowed。

        codex二审批号⑧：非 admin 成员路径还必须当前绑定在制品域内——撤销
        域绑定后残留的成员行不放行（ArtifactNotFound 404，域外不可见口径，
        与 KB 面硬边界对齐）。"""
        artifact = await self._db.get_artifact(artifact_id)
        if artifact is None:
            raise ArtifactNotFound(artifact_id)
        if _is_site_admin(user):
            return artifact, "admin"
        if await self._db.is_domain_admin(_uid(user), artifact["domain_id"]):
            return artifact, "domain_admin"
        if not await self._db.user_in_domain(_uid(user), artifact["domain_id"]):
            raise ArtifactNotFound(artifact_id)
        role = await self._db.artifact_role(artifact_id, _uid(user))
        if role is None and artifact.get("owner_user_id") == _uid(user):
            role = "owner"
        if role is None:
            raise ArtifactNotFound(artifact_id)
        if allowed and role not in allowed and role not in ("admin", "domain_admin"):
            raise ArtifactForbidden(artifact_id)
        return artifact, role

    # ------------------------------------------------------ 制品 CRUD / 列表

    async def create_artifact(
        self, *, user: dict[str, Any], domain_id: str, kind: str = "table",
        title: str, description: str | None = None,
    ) -> dict[str, Any]:
        if not domain_id or not str(domain_id).strip():
            raise InvalidOperation("invalid_domain")
        if kind not in ("table", "wiki"):
            raise InvalidOperation("invalid_kind")
        if not isinstance(title, str):
            raise InvalidOperation("invalid_title")
        cleaned = title.strip()
        if not cleaned:
            raise InvalidOperation("invalid_title")
        if len(cleaned) > _MAX_TITLE_LENGTH:
            raise InvalidOperation("invalid_title")
        try:
            return await self._db.create_artifact(
                domain_id=str(domain_id).strip(), kind=kind, title=cleaned,
                description=description or "", owner_user_id=_uid(user),
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def list_artifacts(
        self, *, user: dict[str, Any], domain_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """site admin → 全量；指定域且是本域 domain admin → 该域全量；
        其余 → 我参与的（按成员行），可选按域过滤。"""
        if _is_site_admin(user):
            return await self._db.list_all_artifacts(domain_id)
        if domain_id is not None and await self._db.is_domain_admin(
            _uid(user), domain_id,
        ):
            return await self._db.list_all_artifacts(domain_id)
        artifacts = await self._db.list_artifacts_for_user(_uid(user))
        if domain_id is None:
            return artifacts
        return [a for a in artifacts if a.get("domain_id") == domain_id]

    async def get_artifact(
        self, *, user: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        """详情附 my_role（批次4b 遗留修复）：admin/domain_admin 旁路归一为
        owner（前端 canWrite = my_role ∈ {owner, editor} 判定口径）。"""
        artifact, role = await self._assert_role(
            user, artifact_id, *ROLE_RUNTIME_READ,
        )
        my_role = "owner" if role in ("admin", "domain_admin") else role
        return {**artifact, "my_role": my_role}

    async def update_meta(
        self, *, user: dict[str, Any], artifact_id: str,
        fields: dict[str, Any], expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        provided = {k: v for k, v in fields.items() if k in _META_FIELDS}
        if "title" in provided:
            title = provided["title"]
            if not isinstance(title, str) or not title.strip() \
                    or len(title.strip()) > _MAX_TITLE_LENGTH:
                raise InvalidOperation("invalid_title")
            provided["title"] = title.strip()
        try:
            updated = await self._db.update_artifact_meta(artifact_id, **provided)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc
        if updated is None:
            raise ArtifactNotFound(artifact_id)
        return updated

    async def delete_artifact(
        self, *, user: dict[str, Any], artifact_id: str,
    ) -> None:
        await self._assert_role(user, artifact_id, *ROLE_OWNER_ONLY)
        await self._db.delete_artifact(artifact_id)

    # -------------------------------------------------------------- 成员

    async def list_members(
        self, *, user: dict[str, Any], artifact_id: str,
    ) -> list[dict[str, Any]]:
        await self._assert_role(user, artifact_id, *ROLE_RUNTIME_READ)
        return await self._db.list_members(artifact_id)

    async def upsert_member(
        self, *, user: dict[str, Any], artifact_id: str,
        member_user_id: str, role: str,
    ) -> dict[str, Any]:
        """成员治理（对抗审查修复⑪）：①目标用户必须在 kb_users 且 active 且
        未软删（查库为准，不存在/非 active → 400）；②last-owner 保护——目标行
        现为 owner、制品 owner 数 ≤1 且新角色非 owner → 403（owner 兜底计数
        含 owner_user_id 不落 members 行的形态）。"""
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_OWNER_ONLY)
        if role not in ("owner", "editor", "viewer"):
            raise InvalidOperation("invalid_member_role")
        status = await self._db.get_user_status(member_user_id)
        if status != "active":
            raise InvalidOperation(f"invalid_member_user:{member_user_id}")
        members = await self._db.list_members(artifact_id)
        target = next(
            (m for m in members if str(m.get("user_id")) == str(member_user_id)),
            None,
        )
        if target is not None and str(target.get("role")) == "owner" \
                and role != "owner":
            owner_ids = {
                str(m["user_id"]) for m in members if m.get("role") == "owner"
            }
            owner_ids.add(str(artifact.get("owner_user_id")))
            if len(owner_ids) <= 1:
                raise ArtifactForbidden("last_owner")
        try:
            return await self._db.upsert_member(
                artifact_id, member_user_id, role,
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def remove_member(
        self, *, user: dict[str, Any], artifact_id: str, member_user_id: str,
    ) -> None:
        await self._assert_role(user, artifact_id, *ROLE_OWNER_ONLY)
        members = await self._db.list_members(artifact_id)
        target = next(
            (m for m in members if str(m.get("user_id")) == str(member_user_id)), None,
        )
        if target is None:
            raise ArtifactNotFound(member_user_id)
        if target.get("role") == "owner":
            owners = [m for m in members if m.get("role") == "owner"]
            if len(owners) <= 1:
                raise InvalidOperation("last_owner")
        await self._db.remove_member(artifact_id, member_user_id)

    # ------------------------------------------------------------ 生命周期

    async def publish(
        self, *, user: dict[str, Any], artifact_id: str,
        change_summary: str = "", expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """发布：owner+editor；usage_note 非空（service 先拦 + 仓储兜底竞态）；
        返回 {revision, row_count, diff 摘要}（diff 在发布前现算=发布预览数字）。"""
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        if not (artifact.get("usage_note") or "").strip():
            raise UsageNoteRequired(artifact_id)
        diff = await self._db.diff_draft_vs_current(artifact_id)
        try:
            result = await self._db.publish(
                artifact_id, change_summary, _uid(user),
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc
        return {
            "revision": result["revision"],
            "row_count": result.get("row_count", 0),
            "diff": {
                "added_count": diff["added_count"],
                "removed_count": diff["removed_count"],
                "changed_count": diff["changed_count"],
            },
        }

    async def rollback_to(
        self, *, user: dict[str, Any], artifact_id: str, revision: int,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        try:
            return await self._db.rollback_to(artifact_id, int(revision))
        except (TypeError, ValueError) as exc:
            raise InvalidOperation("invalid_revision") from exc
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def deprecate(
        self, *, user: dict[str, Any], artifact_id: str,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        try:
            return await self._db.deprecate(artifact_id)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def republish(
        self, *, user: dict[str, Any], artifact_id: str,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        try:
            return await self._db.republish(artifact_id)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def discard_draft(
        self, *, user: dict[str, Any], artifact_id: str,
        expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """丢弃 draft（逃生门）：幂等——无 draft 时同样回到干净态。"""
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        try:
            return await self._db.discard_draft(artifact_id)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def diff_draft_vs_current(
        self, *, user: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        """发布预览差异：editor+（viewer 不见设计态）。"""
        await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        return await self._db.diff_draft_vs_current(artifact_id)

    async def ensure_draft(
        self, *, user: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        """显式开草稿（向导/编辑器进入设计态的入口；内部编辑动作也会自动调）。"""
        try:
            return await self._db.ensure_draft(artifact_id)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    # --------------------------------------------------------- schema（设计态）

    async def list_columns(
        self, *, user: dict[str, Any], artifact_id: str,
        include_dropped: bool = False, runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """runtime=True → 发布定格快照（viewer+）；否则实时设计态列（editor+）。"""
        if runtime:
            artifact, _ = await self._assert_role(
                user, artifact_id, *ROLE_RUNTIME_READ,
            )
            revision = int(artifact.get("current_revision") or 0)
            if revision <= 0:
                return []
            snap = await self._db.get_revision(artifact_id, revision)
            if snap is None:
                return []
            return list(snap.get("schema_json") or [])
        await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        return await self._db.list_columns(
            artifact_id, include_dropped=include_dropped,
        )

    async def upsert_column(
        self, *, user: dict[str, Any], artifact_id: str, column_id: str,
        fields: dict[str, Any], expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """PATCH 式列变更 + active 列名唯一校验（对抗审查修复⑳，DB 部分唯一
        索引兜底）：改名/新建撞既有 active 列名 → 400。dropped 列不占名。

        codex二审批号②：检测到 column_name 变更时——alias 数组追加旧名
        （旧名兼容读取，§7.1）+ rename_from 透传 db 层（同事务迁移 rev0 行
        data 键，用例⑨闭环：不迁键则存量行旧键残留，新 schema 读数全空）。"""
        await self._require_design_session(user, artifact_id, expected_updated_at)
        columns = await self._db.list_columns(artifact_id)
        new_name = fields.get("column_name")
        if new_name is not None:
            self._assert_name_not_taken(
                columns, str(new_name), exclude_column_id=column_id,
            )
        rename_from = None
        if new_name is not None:
            current = next(
                (c for c in columns
                 if str(c.get("column_id")) == str(column_id)), None,
            )
            if current is not None \
                    and str(new_name) != str(current.get("column_name")):
                old_name = str(current.get("column_name"))
                alias = [str(a) for a in (current.get("alias") or [])]
                if old_name and old_name not in alias:
                    alias.append(old_name)
                fields = {**fields, "alias": alias}
                rename_from = old_name
        try:
            return await self._db.upsert_column(
                artifact_id, column_id, fields, rename_from=rename_from,
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    @staticmethod
    def _assert_name_not_taken(
        active_columns: list[dict[str, Any]], name: str,
        *, exclude_column_id: str | None = None,
    ) -> None:
        clash = [
            c for c in active_columns
            if str(c.get("column_name")) == name
            and str(c.get("column_id")) != str(exclude_column_id)
        ]
        if clash:
            raise InvalidOperation(f"duplicate_column_name:{name}")

    async def drop_column(
        self, *, user: dict[str, Any], artifact_id: str, column_id: str,
        expected_updated_at: Any = None,
    ) -> None:
        await self._require_design_session(user, artifact_id, expected_updated_at)
        await self._db.drop_column(artifact_id, column_id)
        await self._db.touch_updated_at(artifact_id)

    async def set_primary_key(
        self, *, user: dict[str, Any], artifact_id: str,
        column_ids: list[str], expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        await self._require_design_session(user, artifact_id, expected_updated_at)
        if not isinstance(column_ids, list) or not all(
            isinstance(c, str) for c in column_ids
        ):
            raise InvalidOperation("invalid_column_ids")
        try:
            return await self._db.set_primary_key(artifact_id, column_ids)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    async def replace_columns(
        self, *, user: dict[str, Any], artifact_id: str,
        columns: list[dict[str, Any]],
    ) -> None:
        """首次导入建列（Task 5 导入服务调用；删旧插新，draft 态）。计划内
        列名重复 → 400（对抗审查修复⑳，DB 部分唯一索引兜底）。"""
        await self._require_design_session(user, artifact_id, None)
        names = [str(c.get("column_name") or "") for c in columns]
        if len(names) != len(set(names)):
            duplicated = sorted({n for n in names if names.count(n) > 1})
            raise InvalidOperation(
                f"duplicate_column_name:{','.join(duplicated)}")
        try:
            await self._db.replace_columns_for_artifact(artifact_id, columns)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc

    # ------------------------------------------------------------- 行读写

    async def list_rows(
        self, *, user: dict[str, Any], artifact_id: str,
        draft: bool = False, revision: int | None = None,
        page: int = 1, size: int = 50, sort_ordinal: bool = True,
    ) -> dict[str, Any]:
        """revision 路由（61号 §9.1）：draft=True → 草稿视图（editor+）——draft_open
        时读 rev0，未开草稿投影 current（discard 语义：干净态=与已发布一致）；
        显式 revision → 历史读（editor+）；缺省 → current_revision（viewer+）。
        从未发布（current_revision=0）的运行态是空集——rev0 是设计态，viewer
        不可见。"""
        if draft or revision is not None:
            artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
            if revision is not None:
                rev = int(revision)
            elif artifact.get("draft_open"):
                rev = 0
            else:
                rev = int(artifact.get("current_revision") or 0)
        else:
            artifact, _ = await self._assert_role(
                user, artifact_id, *ROLE_RUNTIME_READ,
            )
            rev = int(artifact.get("current_revision") or 0)
            if rev <= 0:
                return {"rows": [], "total": 0, "page": max(1, page), "size": size}
        return await self._db.list_rows(
            artifact_id, rev, page=page, size=size, sort_ordinal=sort_ordinal,
        )

    async def count_rows(
        self, *, user: dict[str, Any], artifact_id: str,
        draft: bool = False,
    ) -> int:
        if draft:
            await self._assert_role(user, artifact_id, *ROLE_DESIGN)
            return await self._db.count_rows(artifact_id, 0)
        artifact, _ = await self._assert_role(
            user, artifact_id, *ROLE_RUNTIME_READ,
        )
        current = int(artifact.get("current_revision") or 0)
        return await self._db.count_rows(artifact_id, current) if current > 0 else 0

    async def update_cell(
        self, *, user: dict[str, Any], artifact_id: str, row_key: str,
        column: str, value: Any, expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """单格更新（§7.2 + 对抗审查修复⑨⑩）。行写防线：①值过 _is_formula
        （=+-@ 前缀 400）；②列必须存在（name/alias 可寻址，未知 400）且以
        规范 column_name 落库（防 alias 旧名写影子键）；③主键列禁改
        （codex二审批号⑦——primary_key_immutable 400）；④类型 cast 失败 →
        值置 null + conflicts[column]="type_error:值"（与导入 tolerant 路径
        对称），不阻断。乐观锁：expected 透传 db 层原子校验（FOR UPDATE +
        写 + touch 同事务，codex二审批号⑥b），service 不再单独 touch。"""
        await self._require_design_session(user, artifact_id, expected_updated_at)
        if value is not None and _is_formula(value):
            raise InvalidOperation("formula_not_allowed")
        column_def = await self._find_column(artifact_id, column)
        if column_def is None:
            raise InvalidOperation(f"unknown_column:{column}")
        if column_def.get("is_primary_key"):
            raise InvalidOperation(_PK_IMMUTABLE_MSG)
        canonical = str(column_def["column_name"])
        expected = _expected_dt(expected_updated_at)
        ok, casted = _cast_cell(value, str(column_def.get("column_type") or "text"))
        if ok:
            row = await self._db.update_cell(
                artifact_id, row_key, canonical, casted, _uid(user),
                expected_updated_at=expected,
            )
        else:
            row = await self._db.update_cell_conflict(
                artifact_id, row_key, canonical, f"type_error:{value}", _uid(user),
                expected_updated_at=expected,
            )
        if row is None:
            raise InvalidOperation("row_not_found")
        return row

    async def batch_assign(
        self, *, user: dict[str, Any], artifact_id: str, column: str,
        value: Any, where_clauses: list[dict[str, Any]],
        expected_updated_at: Any = None,
    ) -> dict[str, int]:
        """批量赋值（§7.2 + 对抗审查修复⑥⑩）。校验前置（批量场景 intolerant）：
        ①值过 _is_formula（400）；②目标列必须存在（unknown_column 400，alias
        解析为规范名）；③主键列禁改（codex二审批号⑦）；④int/float/date/bool
        列值 cast 失败直接 400（strict——批量写坏多行无逃生门，与单格
        tolerant 语义刻意不同）。乐观锁同 update_cell（db 层原子，⑥b）。"""
        await self._require_design_session(user, artifact_id, expected_updated_at)
        if value is not None and _is_formula(value):
            raise InvalidOperation("formula_not_allowed")
        column_def = await self._find_column(artifact_id, column)
        if column_def is None:
            raise InvalidOperation(f"unknown_column:{column}")
        if column_def.get("is_primary_key"):
            raise InvalidOperation(_PK_IMMUTABLE_MSG)
        canonical = str(column_def["column_name"])
        ok, casted = _cast_cell(value, str(column_def.get("column_type") or "text"))
        if not ok:
            raise InvalidOperation(f"type_error:{value}")
        try:
            updated = await self._db.batch_assign(
                artifact_id, canonical, casted, where_clauses, _uid(user),
                expected_updated_at=_expected_dt(expected_updated_at),
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc
        return {"updated": updated}

    async def delete_row(
        self, *, user: dict[str, Any], artifact_id: str, row_key: str,
        expected_updated_at: Any = None,
    ) -> None:
        """乐观锁：expected 透传 db 层原子校验+touch（codex二审批号⑥b）。"""
        await self._require_design_session(user, artifact_id, expected_updated_at)
        try:
            hit = await self._db.delete_row(
                artifact_id, row_key,
                expected_updated_at=_expected_dt(expected_updated_at),
            )
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc
        if not hit:
            raise InvalidOperation("row_not_found")

    async def insert_rows(
        self, *, user: dict[str, Any], artifact_id: str,
        rows: list[dict[str, Any]], revision: int = 0,
    ) -> dict[str, int]:
        """追加导入落行（Task 5 导入服务调用）：UPSERT by row_key + ensure_draft
        前置 + 乐观锁令牌推进。"""
        await self._require_design_session(user, artifact_id, None)
        result = await self._db.insert_rows_upsert(
            artifact_id, revision, rows, updated_by=_uid(user),
        )
        await self._db.touch_updated_at(artifact_id)
        return result

    async def insert_single_row(
        self, *, user: dict[str, Any], artifact_id: str,
        data: dict[str, Any], expected_updated_at: Any = None,
    ) -> dict[str, Any]:
        """手工行增（§7.2 行增 + 对抗审查修复⑩）。行写防线：①每值过
        _is_formula（400）；②data 键必须是 active 列（name/alias 可寻址，
        未知键 400，alias 归一到规范名）；③类型 cast 复用 _cast_cell（失败置
        null + conflicts 标记——与 update_cell tolerant 同口径）。主键：有
        主键 → 规范名取值拼接，rev0 已有同键 → 409、主键值缺失 → 400；
        无主键 → row_key=uuid。provenance 记 manual 来源。"""
        await self._require_design_session(user, artifact_id, expected_updated_at)
        if not isinstance(data, dict):
            raise InvalidOperation("invalid_row_data")
        columns = await self._db.list_columns(artifact_id)
        pk_names = [
            str(c["column_name"]) for c in columns if c.get("is_primary_key")
        ]
        canonical_data: dict[str, Any] = {}
        conflicts: dict[str, Any] = {}
        for raw_key, raw_value in data.items():
            if raw_value is not None and _is_formula(raw_value):
                raise InvalidOperation(f"formula_not_allowed:{raw_key}")
            column_def = self._match_column(columns, str(raw_key))
            if column_def is None:
                raise InvalidOperation(f"unknown_column:{raw_key}")
            name = str(column_def["column_name"])
            ok, casted = _cast_cell(
                raw_value, str(column_def.get("column_type") or "text"),
            )
            if ok:
                canonical_data[name] = casted
            else:
                canonical_data[name] = None
                conflicts[name] = f"type_error:{raw_value}"
        if pk_names and all(
            canonical_data.get(name) in (None, "") for name in pk_names
        ):
            raise InvalidOperation("missing_primary_key")
        row_key = _row_key_value(canonical_data, pk_names)
        if pk_names and await self._db.get_row(artifact_id, 0, row_key) is not None:
            raise PrimaryKeyConflict(f"duplicate_key:{row_key}")
        ordinal = await self._db.count_rows(artifact_id, 0) + 1
        await self._db.insert_rows_upsert(
            artifact_id, 0,
            [{
                "row_key": row_key,
                "data": canonical_data,
                "provenance": [{"channel": "manual", "created_by": _uid(user)}],
                "conflicts": conflicts,
                "ordinal": ordinal,
            }],
            updated_by=_uid(user),
        )
        await self._db.touch_updated_at(artifact_id)
        row = await self._db.get_row(artifact_id, 0, row_key)
        if row is None:
            raise InvalidOperation("row_not_found")
        return row

    async def _find_column(
        self, artifact_id: str, column: str,
    ) -> dict[str, Any] | None:
        """按列名或 alias 找 active 列定义（§7.1 改名后旧名仍可寻址）。"""
        return self._match_column(
            await self._db.list_columns(artifact_id), column,
        )

    @staticmethod
    def _match_column(
        columns: list[dict[str, Any]], column: str,
    ) -> dict[str, Any] | None:
        for col in columns:
            if str(col.get("column_name")) == column or column in [
                str(a) for a in (col.get("alias") or [])
            ]:
                return col
        return None

    # ------------------------------------------------------------- 内部

    async def _require_design_session(
        self, user: dict[str, Any], artifact_id: str, expected_updated_at: Any,
    ) -> dict[str, Any]:
        """设计态编辑动作统一前置：角色（owner+editor）→ 乐观锁 → ensure_draft。"""
        artifact, _ = await self._assert_role(user, artifact_id, *ROLE_DESIGN)
        _check_freshness(artifact, expected_updated_at)
        try:
            await self._db.ensure_draft(artifact_id)
        except ArtifactRepoError as exc:
            raise _translate(exc) from exc
        return artifact
