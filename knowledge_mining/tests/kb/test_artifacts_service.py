"""ArtifactService 权限矩阵 / 生命周期编排 / 乐观锁 —— 纯内存 fake DB（本地可跑）。

61号 §5.2 角色矩阵的 200/403/404 精确断言：非成员一律 ArtifactNotFound（404，
反探测，含同域非成员）；viewer 见 Forbidden 而非 404（制品存在性对成员不隐藏，
只是设计态不可见——审查定稿口径）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from knowledge_mining.mining.kb.db_artifacts import ArtifactError as ArtifactRepoError
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
    InvalidOperation,
    PrimaryKeyConflict,
    StaleArtifact,
    UsageNoteRequired,
)


NOW = datetime(2026, 10, 16, 10, 0, 0, 500000, tzinfo=timezone.utc)
NOW_ISO = "2026-10-16T10:00:00+00:00"


class FakeArtifactDB:
    """ArtifactDB 接口的内存实现：够 service 编排 + 调用序断言。"""

    def __init__(self) -> None:
        self.artifacts: dict[str, dict] = {}
        self.members: dict[str, dict[str, str]] = {}   # artifact_id -> user_id -> role
        self.rev0_rows: dict[str, list[dict]] = {}
        self.revision_rows: dict[str, dict[int, list[dict]]] = {}
        self.columns: dict[str, list[dict]] = {}
        self.domain_admins: set[tuple[str, str]] = set()
        self.calls: list[str] = []
        # 61号批次2b：导入向导 + 创建域门禁
        self.batches: dict[str, list[dict]] = {}
        self.domain_bindings: set[tuple[str, str]] = set()  # (user_id, domain)
        self.site_admins: set[str] = set()

    def _touch(self, artifact_id: str) -> None:
        art = self.artifacts[artifact_id]
        art["updated_at"] = datetime.now(timezone.utc)

    # ---- 基础读写 --------------------------------------------------------

    async def create_artifact(
        self, *, domain_id: str, kind: str = "table", title: str,
        description: str = "", owner_user_id: str, artifact_id: str | None = None,
        usage_note: str = "",
    ) -> dict:
        aid = artifact_id or "art_new"
        row = {
            "artifact_id": aid, "domain_id": domain_id, "kind": kind,
            "title": title, "description": description, "usage_note": usage_note,
            "owner_user_id": owner_user_id, "status": "draft",
            "current_revision": 0, "draft_open": False,
            "created_at": NOW, "updated_at": NOW,
        }
        self.artifacts[aid] = row
        self.members.setdefault(aid, {})[owner_user_id] = "owner"
        self.rev0_rows[aid] = []
        self.columns[aid] = []
        return dict(row)

    async def get_artifact(self, artifact_id: str) -> dict | None:
        art = self.artifacts.get(artifact_id)
        return dict(art) if art else None

    async def update_artifact_meta(
        self, artifact_id: str, *, title=None, description=None, usage_note=None,
    ) -> dict | None:
        art = self.artifacts.get(artifact_id)
        if art is None:
            return None
        if title is not None:
            art["title"] = title
        if description is not None:
            art["description"] = description
        if usage_note is not None:
            art["usage_note"] = usage_note
        self._touch(artifact_id)
        return dict(art)

    async def touch_updated_at(self, artifact_id: str) -> None:
        self.calls.append("touch_updated_at")
        if artifact_id in self.artifacts:
            self._touch(artifact_id)

    async def delete_artifact(self, artifact_id: str) -> None:
        self.artifacts.pop(artifact_id, None)
        self.members.pop(artifact_id, None)
        self.rev0_rows.pop(artifact_id, None)
        self.revision_rows.pop(artifact_id, None)
        self.columns.pop(artifact_id, None)
        self.batches.pop(artifact_id, None)

    # ---- 创建域门禁 / 导入批次 -------------------------------------------

    async def can_create_in_domain(self, user_id: str, domain_id: str) -> bool:
        return (user_id, domain_id) in self.domain_bindings \
            or user_id in self.site_admins

    async def first_domain_for_user(self, user_id: str) -> str | None:
        domains = sorted(d for (u, d) in self.domain_bindings if u == user_id)
        return domains[0] if domains else None

    async def create_batch(
        self, artifact_id: str, *, channel: str, file_name: str,
        sheet_name: str, row_count: int, created_by: str,
    ) -> dict:
        batch = {
            "batch_id": f"bat_{uuid.uuid4().hex[:12]}",
            "artifact_id": artifact_id, "channel": channel,
            "file_name": file_name, "sheet_name": sheet_name,
            "row_count": row_count, "created_by": created_by,
            "created_at": NOW,
        }
        self.batches.setdefault(artifact_id, []).append(batch)
        return dict(batch)

    async def list_batches(self, artifact_id: str) -> list[dict]:
        return [dict(b) for b in self.batches.get(artifact_id, [])]

    async def list_artifacts_for_user(self, user_id: str) -> list[dict]:
        out = []
        for aid, roles in self.members.items():
            if user_id in roles and aid in self.artifacts:
                out.append({**self.artifacts[aid], "my_role": roles[user_id]})
        return out

    async def list_all_artifacts(self, domain_id: str | None = None) -> list[dict]:
        self.calls.append(("list_all_artifacts", domain_id))
        return [
            dict(a) for a in self.artifacts.values()
            if domain_id is None or a["domain_id"] == domain_id
        ]

    # ---- 成员 ------------------------------------------------------------

    async def list_members(self, artifact_id: str) -> list[dict]:
        return [
            {"user_id": uid, "role": role}
            for uid, role in self.members.get(artifact_id, {}).items()
        ]

    async def upsert_member(self, artifact_id: str, user_id: str, role: str) -> dict:
        self.members.setdefault(artifact_id, {})[user_id] = role
        return {"user_id": user_id, "role": role}

    async def remove_member(self, artifact_id: str, user_id: str) -> None:
        self.members.get(artifact_id, {}).pop(user_id, None)

    async def artifact_role(self, artifact_id: str, user_id: str) -> str | None:
        return self.members.get(artifact_id, {}).get(user_id)

    async def is_domain_admin(self, user_id: str, domain_id: str) -> bool:
        return (user_id, domain_id) in self.domain_admins

    # ---- 生命周期 ---------------------------------------------------------

    async def publish(
        self, artifact_id: str, change_summary: str, published_by: str,
    ) -> dict:
        self.calls.append("publish")
        art = self.artifacts[artifact_id]
        if not (art.get("usage_note") or "").strip():
            raise ArtifactRepoError("usage_note_required")
        new_rev = art["current_revision"] + 1
        rows = self.rev0_rows.get(artifact_id, [])
        self.revision_rows.setdefault(artifact_id, {})[new_rev] = [dict(r) for r in rows]
        self.rev0_rows[artifact_id] = []
        art.update(
            current_revision=new_rev, status="published", draft_open=False,
        )
        self._touch(artifact_id)
        return {"revision": new_rev, "row_count": len(rows)}

    async def get_revision(self, artifact_id: str, revision: int) -> dict | None:
        if revision not in self.revision_rows.get(artifact_id, {}):
            return None
        return {
            "artifact_id": artifact_id, "revision": revision,
            "schema_json": [dict(c) for c in self.columns.get(artifact_id, [])],
            "row_count": len(self.revision_rows[artifact_id][revision]),
        }

    async def rollback_to(self, artifact_id: str, revision: int) -> dict:
        self.calls.append("rollback_to")
        art = self.artifacts[artifact_id]
        art["current_revision"] = revision
        art["status"] = "published"
        self._touch(artifact_id)
        return dict(art)

    async def deprecate(self, artifact_id: str) -> dict:
        art = self.artifacts[artifact_id]
        if art["status"] != "published":
            raise ArtifactRepoError("invalid_status")
        art["status"] = "deprecated"
        self._touch(artifact_id)
        return dict(art)

    async def republish(self, artifact_id: str) -> dict:
        art = self.artifacts[artifact_id]
        if art["status"] != "deprecated":
            raise ArtifactRepoError("invalid_status")
        art["status"] = "published"
        self._touch(artifact_id)
        return dict(art)

    async def discard_draft(self, artifact_id: str) -> dict:
        self.calls.append("discard_draft")
        art = self.artifacts[artifact_id]
        self.rev0_rows[artifact_id] = []
        art["draft_open"] = False
        self._touch(artifact_id)
        return dict(art)

    async def ensure_draft(self, artifact_id: str) -> dict:
        self.calls.append("ensure_draft")
        art = self.artifacts[artifact_id]
        if not art["draft_open"]:
            if art["current_revision"] > 0:
                current = self.revision_rows.get(artifact_id, {}).get(
                    art["current_revision"], [],
                )
                self.rev0_rows[artifact_id] = [dict(r) for r in current]
            art["draft_open"] = True
            self._touch(artifact_id)
        return dict(art)

    # ---- schema / 行 ------------------------------------------------------

    async def list_columns(
        self, artifact_id: str, include_dropped: bool = False,
    ) -> list[dict]:
        cols = self.columns.get(artifact_id, [])
        if include_dropped:
            return [dict(c) for c in cols]
        return [dict(c) for c in cols if c.get("status") != "dropped"]

    async def upsert_column(
        self, artifact_id: str, column_id: str, fields: dict,
    ) -> dict:
        cols = self.columns.setdefault(artifact_id, [])
        for col in cols:
            if col["column_id"] == column_id:
                col.update(fields)
                return dict(col)
        row = {"column_id": column_id,
               "column_name": str(fields.get("column_name") or column_id),
               "column_type": fields.get("column_type", "text"),
               "enum_values": fields.get("enum_values"),
               "is_indexed": bool(fields.get("is_indexed", False)),
               "is_primary_key": False, "alias": fields.get("alias") or [],
               "ordinal": int(fields.get("ordinal", len(cols))),
               "status": "active"}
        cols.append(row)
        return dict(row)

    async def drop_column(self, artifact_id: str, column_id: str) -> None:
        for col in self.columns.get(artifact_id, []):
            if col["column_id"] == column_id:
                col["status"] = "dropped"
                col["is_primary_key"] = False

    async def set_primary_key(
        self, artifact_id: str, column_ids: list[str],
    ) -> dict:
        self.calls.append("set_primary_key")
        if getattr(self, "pk_conflict", False):
            raise ArtifactRepoError("primary_key_conflict")
        for col in self.columns.get(artifact_id, []):
            col["is_primary_key"] = col["column_id"] in column_ids
        return {"artifact_id": artifact_id, "primary_key": list(column_ids),
                "rekeyed_rows": 0}

    async def replace_columns_for_artifact(
        self, artifact_id: str, columns: list[dict],
    ) -> None:
        self.calls.append("replace_columns_for_artifact")
        self.columns[artifact_id] = [dict(c) for c in columns]

    async def insert_rows_upsert(
        self, artifact_id: str, revision: int, rows: list[dict], updated_by: str = "",
    ) -> dict:
        self.calls.append("insert_rows_upsert")
        bucket = self.rev0_rows.setdefault(artifact_id, []) if revision == 0 \
            else self.revision_rows.setdefault(artifact_id, {}).setdefault(revision, [])
        existing = {r["row_key"]: r for r in bucket}
        inserted = updated = 0
        for row in rows:
            hit = existing.get(row["row_key"])
            if hit is None:
                bucket.append(dict(row))
                existing[row["row_key"]] = row
                inserted += 1
            else:
                hit["data"] = dict(row["data"])
                hit["provenance"] = list(hit.get("provenance", [])) + \
                    list(row.get("provenance", []))
                # conflicts 与 ordinal 按 UPSERT 语义覆盖（对齐真实仓储）
                hit["conflicts"] = dict(row.get("conflicts", {}))
                hit["ordinal"] = row.get("ordinal", hit.get("ordinal", 0))
                updated += 1
        return {"inserted": inserted, "updated": updated}

    async def update_cell(
        self, artifact_id: str, row_key: str, column: str, value, updated_by: str,
    ) -> dict | None:
        self.calls.append("update_cell")
        for row in self.rev0_rows.get(artifact_id, []):
            if row["row_key"] == row_key:
                row["data"] = {**row["data"], column: value}
                return dict(row)
        return None

    async def batch_assign(
        self, artifact_id: str, column: str, value, where_clauses: list, updated_by: str,
    ) -> int:
        self.calls.append("batch_assign")
        n = 0
        for row in self.rev0_rows.get(artifact_id, []):
            if self._match(row["data"], where_clauses):
                row["data"] = {**row["data"], column: value}
                n += 1
        return n

    @staticmethod
    def _match(data: dict, where_clauses: list) -> bool:
        for clause in where_clauses:
            col, op, val = clause["column"], clause.get("op", "eq"), clause["value"]
            cell = data.get(col)
            if op == "eq" and cell != val:
                return False
            if op == "ne" and cell == val:
                return False
        return True

    async def delete_row(self, artifact_id: str, row_key: str) -> bool:
        self.calls.append("delete_row")
        rows = self.rev0_rows.get(artifact_id, [])
        kept = [r for r in rows if r["row_key"] != row_key]
        if len(kept) == len(rows):
            return False
        self.rev0_rows[artifact_id] = kept
        return True

    async def list_rows(
        self, artifact_id: str, revision: int, page: int = 1, size: int = 50,
        sort_ordinal: bool = True,
    ) -> dict:
        if revision == 0:
            rows = self.rev0_rows.get(artifact_id, [])
        else:
            rows = self.revision_rows.get(artifact_id, {}).get(revision, [])
        return {"rows": [dict(r) for r in rows], "total": len(rows)}

    async def count_rows(self, artifact_id: str, revision: int) -> int:
        if revision == 0:
            return len(self.rev0_rows.get(artifact_id, []))
        return len(self.revision_rows.get(artifact_id, {}).get(revision, []))

    async def diff_draft_vs_current(self, artifact_id: str) -> dict:
        self.calls.append("diff_draft_vs_current")
        art = self.artifacts[artifact_id]
        cur = art["current_revision"]
        current_rows = self.revision_rows.get(artifact_id, {}).get(cur, []) \
            if cur > 0 else []
        draft = {r["row_key"]: r["data"] for r in self.rev0_rows.get(artifact_id, [])}
        current = {r["row_key"]: r["data"] for r in current_rows}
        added = sorted(set(draft) - set(current))
        removed = sorted(set(current) - set(draft))
        changed = []
        for key in sorted(set(draft) & set(current)):
            if draft[key] != current[key]:
                for col in sorted(set(draft[key]) | set(current[key])):
                    old, new = current[key].get(col), draft[key].get(col)
                    if old != new:
                        changed.append(
                            {"row_key": key, "column": col, "old": old, "new": new},
                        )
        return {
            "added": added, "removed": removed, "changed": changed,
            "added_count": len(added), "removed_count": len(removed),
            "changed_count": len(changed),
        }


def _user(uid: str, site_role: str = "member") -> dict:
    """auth.current_user 返回的 kb_users 行形态（id/site_role）。"""
    return {"id": uid, "username": uid, "site_role": site_role, "status": "active"}


async def _seeded_db() -> FakeArtifactDB:
    db = FakeArtifactDB()
    db.artifacts["art_main"] = {
        "artifact_id": "art_main", "domain_id": "plant-a", "kind": "table",
        "title": "MOP 表", "description": "", "usage_note": "运维查询用",
        "owner_user_id": "u-owner", "status": "published",
        "current_revision": 1, "draft_open": False,
        "created_at": NOW, "updated_at": NOW,
    }
    db.members["art_main"] = {
        "u-owner": "owner", "u-editor": "editor", "u-viewer": "viewer",
    }
    db.rev0_rows["art_main"] = []
    db.revision_rows["art_main"] = {
        1: [
            {"row_key": "MOP-1", "data": {"MOP": "MOP-1", "L1": "部署"},
             "provenance": [], "conflicts": {}, "ordinal": 1},
            {"row_key": "MOP-2", "data": {"MOP": "MOP-2", "L1": "扩容"},
             "provenance": [], "conflicts": {}, "ordinal": 2},
        ],
    }
    db.columns["art_main"] = [
        {"column_id": "c001", "column_name": "MOP", "column_type": "text",
         "is_indexed": True, "is_primary_key": True, "ordinal": 1, "status": "active"},
        {"column_id": "c002", "column_name": "L1", "column_type": "enum",
         "is_indexed": False, "is_primary_key": False, "ordinal": 2,
         "status": "active"},
    ]
    db.domain_admins.add(("u-domadmin", "plant-a"))
    return db


AID = "art_main"


# ---------------------------------------------------------------- 矩阵：site admin

@pytest.mark.asyncio
async def test_site_admin_bypasses_every_gate() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    admin = _user("u-siteadmin", site_role="admin")
    assert (await svc.get_artifact(user=admin, artifact_id=AID))["artifact_id"] == AID
    await svc.update_cell(
        user=admin, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
    )
    result = await svc.publish(user=admin, artifact_id=AID, change_summary="site admin")
    assert result["revision"] == 2
    await svc.upsert_member(
        user=admin, artifact_id=AID, member_user_id="u-new", role="viewer",
    )
    await svc.delete_artifact(user=admin, artifact_id=AID)
    assert await db.get_artifact(AID) is None


# ---------------------------------------------------------------- 矩阵：域 admin

@pytest.mark.asyncio
async def test_domain_admin_manages_own_domain_but_404_cross_domain() -> None:
    db = await _seeded_db()
    db.artifacts["art_other"] = {**db.artifacts["art_main"],
                                 "artifact_id": "art_other", "domain_id": "other-dom",
                                 "owner_user_id": "u-x"}
    db.members["art_other"] = {"u-x": "owner"}
    db.rev0_rows["art_other"] = []
    svc = ArtifactService(db)
    domadmin = _user("u-domadmin")
    # 本域：全权（发布/成员/删除路径都放行到 db）
    await svc.publish(user=domadmin, artifact_id=AID, change_summary="域管理员发布")
    await svc.upsert_member(
        user=domadmin, artifact_id=AID, member_user_id="u-new", role="editor",
    )
    await svc.delete_artifact(user=domadmin, artifact_id=AID)
    # 跨域且非成员 → 404（反探测，不是 403）
    with pytest.raises(ArtifactNotFound):
        await svc.get_artifact(user=domadmin, artifact_id="art_other")
    with pytest.raises(ArtifactNotFound):
        await svc.delete_artifact(user=domadmin, artifact_id="art_other")


# ---------------------------------------------------------------- 矩阵：owner / editor / viewer

@pytest.mark.asyncio
async def test_owner_full_access() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    assert (await svc.list_rows(user=owner, artifact_id=AID))["total"] == 2
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
    )
    await svc.publish(user=owner, artifact_id=AID, change_summary="rev2")
    await svc.upsert_member(
        user=owner, artifact_id=AID, member_user_id="u-new", role="viewer",
    )
    await svc.remove_member(user=owner, artifact_id=AID, member_user_id="u-new")
    await svc.delete_artifact(user=owner, artifact_id=AID)


@pytest.mark.asyncio
async def test_editor_can_edit_and_publish_but_not_members_or_delete() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    editor = _user("u-editor")
    # 读运行态 / 读设计态 / 编辑 / 发布：允许
    assert (await svc.list_rows(user=editor, artifact_id=AID))["total"] == 2
    assert (await svc.list_rows(user=editor, artifact_id=AID, draft=True))["total"] == 2
    await svc.update_cell(
        user=editor, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
    )
    await svc.publish(user=editor, artifact_id=AID, change_summary="editor 发布")
    await svc.discard_draft(user=editor, artifact_id=AID)
    await svc.rollback_to(user=editor, artifact_id=AID, revision=1)
    await svc.deprecate(user=editor, artifact_id=AID)
    await svc.republish(user=editor, artifact_id=AID)
    # 成员管理 / 删除：Forbidden
    with pytest.raises(ArtifactForbidden):
        await svc.upsert_member(
            user=editor, artifact_id=AID, member_user_id="u-new", role="viewer",
        )
    with pytest.raises(ArtifactForbidden):
        await svc.remove_member(user=editor, artifact_id=AID, member_user_id="u-viewer")
    with pytest.raises(ArtifactForbidden):
        await svc.delete_artifact(user=editor, artifact_id=AID)
    assert await db.get_artifact(AID) is not None


@pytest.mark.asyncio
async def test_viewer_reads_runtime_only() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    viewer = _user("u-viewer")
    # 元信息 + 运行态：允许
    assert (await svc.get_artifact(user=viewer, artifact_id=AID))["title"] == "MOP 表"
    assert (await svc.list_rows(user=viewer, artifact_id=AID))["total"] == 2
    assert (await svc.list_columns(user=viewer, artifact_id=AID, runtime=True)) != []
    # 设计态 / 编辑 / 生命周期 / 成员 / 删除：Forbidden
    with pytest.raises(ArtifactForbidden):
        await svc.list_rows(user=viewer, artifact_id=AID, draft=True)
    with pytest.raises(ArtifactForbidden):
        await svc.list_rows(user=viewer, artifact_id=AID, revision=1)
    with pytest.raises(ArtifactForbidden):
        await svc.diff_draft_vs_current(user=viewer, artifact_id=AID)
    with pytest.raises(ArtifactForbidden):
        await svc.list_columns(user=viewer, artifact_id=AID)
    with pytest.raises(ArtifactForbidden):
        await svc.update_cell(
            user=viewer, artifact_id=AID, row_key="MOP-1", column="L1", value="x",
        )
    with pytest.raises(ArtifactForbidden):
        await svc.publish(user=viewer, artifact_id=AID, change_summary="x")
    with pytest.raises(ArtifactForbidden):
        await svc.delete_artifact(user=viewer, artifact_id=AID)


@pytest.mark.asyncio
async def test_non_member_same_domain_gets_404_not_403() -> None:
    """★反探测：同域非成员对所有端点拿 ArtifactNotFound（404），绝不 403。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    stranger = _user("u-stranger")   # 同域（plant-a）非成员
    for call in (
        lambda: svc.get_artifact(user=stranger, artifact_id=AID),
        lambda: svc.list_rows(user=stranger, artifact_id=AID),
        lambda: svc.update_cell(
            user=stranger, artifact_id=AID, row_key="MOP-1", column="L1", value="x",
        ),
        lambda: svc.publish(user=stranger, artifact_id=AID, change_summary="x"),
        lambda: svc.upsert_member(
            user=stranger, artifact_id=AID, member_user_id="u-new", role="viewer",
        ),
        lambda: svc.delete_artifact(user=stranger, artifact_id=AID),
        lambda: svc.diff_draft_vs_current(user=stranger, artifact_id=AID),
    ):
        with pytest.raises(ArtifactNotFound):
            await call()
    # 不存在的制品：同样 404
    with pytest.raises(ArtifactNotFound):
        await svc.get_artifact(user=stranger, artifact_id="art_missing")


# ---------------------------------------------------------------- 发布门禁

@pytest.mark.asyncio
async def test_publish_without_usage_note_raises() -> None:
    db = await _seeded_db()
    db.artifacts[AID]["usage_note"] = "   "
    svc = ArtifactService(db)
    with pytest.raises(UsageNoteRequired):
        await svc.publish(user=_user("u-owner"), artifact_id=AID, change_summary="x")
    assert db.artifacts[AID]["current_revision"] == 1   # 未推进


@pytest.mark.asyncio
async def test_publish_returns_revision_and_diff_summary() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    # 编辑 draft：改 1 行 + 删 1 行 + 加 1 行
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
    )
    await svc.delete_row(user=owner, artifact_id=AID, row_key="MOP-2")
    await svc.insert_rows(
        user=owner, artifact_id=AID,
        rows=[{"row_key": "MOP-3", "data": {"MOP": "MOP-3", "L1": "割接"},
               "provenance": [], "conflicts": {}, "ordinal": 3}],
    )
    result = await svc.publish(user=owner, artifact_id=AID, change_summary="rev2")
    assert result["revision"] == 2
    assert result["row_count"] == 2
    assert result["diff"] == {
        "added_count": 1, "removed_count": 1, "changed_count": 1,
    }


# ---------------------------------------------------------------- 乐观锁

@pytest.mark.asyncio
async def test_stale_expected_updated_at_raises() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    stale = "2026-10-16T09:58:00+00:00"
    with pytest.raises(StaleArtifact):
        await svc.update_cell(
            user=_user("u-owner"), artifact_id=AID, row_key="MOP-1",
            column="L1", value="x", expected_updated_at=stale,
        )
    with pytest.raises(StaleArtifact):
        await svc.publish(
            user=_user("u-owner"), artifact_id=AID, change_summary="x",
            expected_updated_at=stale,
        )
    # 未被允许的变更没有触达仓储
    assert "update_cell" not in db.calls
    assert "publish" not in db.calls


@pytest.mark.asyncio
async def test_matching_or_absent_expected_updated_at_passes() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    # NOW 带 .5 微秒；expected 截到秒 → 容差放行
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
        expected_updated_at=NOW_ISO,
    )
    fresh = db.artifacts[AID]["updated_at"].isoformat()
    # 不传 → 放行
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练2",
    )
    assert "update_cell" in db.calls
    assert fresh  # sanity


@pytest.mark.asyncio
async def test_invalid_expected_updated_at_format_rejected() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    with pytest.raises(InvalidOperation):
        await svc.update_cell(
            user=_user("u-owner"), artifact_id=AID, row_key="MOP-1",
            column="L1", value="x", expected_updated_at="not-a-timestamp",
        )


# ---------------------------------------------------------------- ensure_draft 链

@pytest.mark.asyncio
async def test_ensure_draft_runs_before_every_design_edit() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
    )
    await svc.batch_assign(
        user=owner, artifact_id=AID, column="L1", value="批量",
        where_clauses=[{"column": "MOP", "op": "eq", "value": "MOP-1"}],
    )
    await svc.delete_row(user=owner, artifact_id=AID, row_key="MOP-2")
    for edit in ("update_cell", "batch_assign", "delete_row"):
        assert db.calls.index("ensure_draft") < db.calls.index(edit)
    assert db.artifacts[AID]["draft_open"] is True


# ---------------------------------------------------------------- 成员守卫

@pytest.mark.asyncio
async def test_removing_last_owner_is_blocked() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    with pytest.raises(InvalidOperation):
        await svc.remove_member(
            user=_user("u-owner"), artifact_id=AID, member_user_id="u-owner",
        )
    assert db.members[AID].get("u-owner") == "owner"


@pytest.mark.asyncio
async def test_removing_owner_when_another_remains_is_allowed() -> None:
    db = await _seeded_db()
    db.members[AID]["u-owner2"] = "owner"
    svc = ArtifactService(db)
    await svc.remove_member(
        user=_user("u-owner"), artifact_id=AID, member_user_id="u-owner2",
    )
    assert "u-owner2" not in db.members[AID]


@pytest.mark.asyncio
async def test_member_role_validation_and_missing_member() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    with pytest.raises(InvalidOperation):
        await svc.upsert_member(
            user=owner, artifact_id=AID, member_user_id="u-new", role="boss",
        )
    with pytest.raises(ArtifactNotFound):
        await svc.remove_member(
            user=owner, artifact_id=AID, member_user_id="u-ghost",
        )


# ---------------------------------------------------------------- 编排细节

@pytest.mark.asyncio
async def test_update_cell_missing_row_raises_invalid_operation() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    with pytest.raises(InvalidOperation):
        await svc.update_cell(
            user=_user("u-owner"), artifact_id=AID, row_key="MOP-404",
            column="L1", value="x",
        )


@pytest.mark.asyncio
async def test_runtime_read_of_never_published_artifact_is_empty() -> None:
    db = await _seeded_db()
    await db.create_artifact(
        domain_id="plant-a", title="新制品", owner_user_id="u-owner",
        artifact_id="art_new",
    )
    db.rev0_rows["art_new"] = [
        {"row_key": "r1", "data": {"a": 1}, "provenance": [], "conflicts": {},
         "ordinal": 1},
    ]
    db.members["art_new"]["u-viewer"] = "viewer"
    svc = ArtifactService(db)
    # current_revision=0：运行态没有任何已发布行（rev0 是设计态，viewer 不可见）
    result = await svc.list_rows(user=_user("u-viewer"), artifact_id="art_new")
    assert result["rows"] == [] and result["total"] == 0


@pytest.mark.asyncio
async def test_list_artifacts_admin_gets_all_and_member_gets_own() -> None:
    db = await _seeded_db()
    db.artifacts["art_other"] = {**db.artifacts["art_main"],
                                 "artifact_id": "art_other",
                                 "domain_id": "other-dom",
                                 "owner_user_id": "u-x"}
    db.members["art_other"] = {"u-x": "owner"}
    db.rev0_rows["art_other"] = []
    svc = ArtifactService(db)
    mine = await svc.list_artifacts(user=_user("u-viewer"))
    assert [a["artifact_id"] for a in mine] == ["art_main"]
    assert mine[0]["my_role"] == "viewer"
    site_all = await svc.list_artifacts(user=_user("u-siteadmin", site_role="admin"))
    assert {a["artifact_id"] for a in site_all} == {"art_main", "art_other"}
    dom_all = await svc.list_artifacts(user=_user("u-domadmin"), domain_id="plant-a")
    assert [a["artifact_id"] for a in dom_all] == ["art_main"]
    dom_filtered = await svc.list_artifacts(user=_user("u-domadmin"), domain_id="none")
    assert dom_filtered == []


@pytest.mark.asyncio
async def test_create_artifact_makes_creator_owner() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    created = await svc.create_artifact(
        user=_user("u-maker"), domain_id="plant-a", title="  新表  ",
        description="d",
    )
    assert created["owner_user_id"] == "u-maker"
    assert created["title"] == "新表"
    assert db.members[created["artifact_id"]]["u-maker"] == "owner"
    with pytest.raises(InvalidOperation):
        await svc.create_artifact(user=_user("u-maker"), domain_id="plant-a", title="  ")
    with pytest.raises(InvalidOperation):
        await svc.create_artifact(user=_user("u-maker"), domain_id="", title="t")


@pytest.mark.asyncio
async def test_set_primary_key_maps_repo_conflict_error() -> None:
    db = await _seeded_db()
    db.pk_conflict = True
    svc = ArtifactService(db)
    with pytest.raises(PrimaryKeyConflict):
        await svc.set_primary_key(
            user=_user("u-owner"), artifact_id=AID, column_ids=["c001"],
        )


@pytest.mark.asyncio
async def test_update_meta_whitelist_and_role() -> None:
    db = await _seeded_db()
    svc = ArtifactService(db)
    updated = await svc.update_meta(
        user=_user("u-editor"), artifact_id=AID,
        fields={"usage_note": "何时用：割接前", "rogue_field": "x"},
    )
    assert updated["usage_note"] == "何时用：割接前"
    assert "rogue_field" not in updated
    with pytest.raises(ArtifactForbidden):
        await svc.update_meta(
            user=_user("u-viewer"), artifact_id=AID, fields={"title": "改名"},
        )
