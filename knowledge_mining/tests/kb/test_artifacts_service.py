"""ArtifactService 权限矩阵 / 生命周期编排 / 乐观锁 —— 纯内存 fake DB（本地可跑）。

61号 §5.2 角色矩阵的 200/403/404 精确断言：非成员一律 ArtifactNotFound（404，
反探测，含同域非成员）；viewer 见 Forbidden 而非 404（制品存在性对成员不隐藏，
只是设计态不可见——审查定稿口径）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from knowledge_mining.mining.kb.db_artifacts import ArtifactError as ArtifactRepoError
from knowledge_mining.mining.kb.db_artifacts import _encode_key_parts
from knowledge_mining.mining.kb.db_artifacts import _row_key_value
from knowledge_mining.mining.kb.services.artifact_service import (
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
    InvalidOperation,
    NoPendingChanges,
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
        # codex二审功能项②：发布定格 schema 快照（真库 revisions.schema_json
        # 在 publish 时落库——运行态读快照而非实时列）
        self.revision_schemas: dict[str, dict[int, list[dict]]] = {}
        # 版本历史（镜像真库 list_revisions：不含 artifact_id，由路由层补）
        self.revisions: dict[str, list[dict]] = {}
        self.columns: dict[str, list[dict]] = {}
        self.domain_admins: set[tuple[str, str]] = set()
        self.calls: list[str] = []
        # 61号批次2b：导入向导 + 创建域门禁
        self.batches: dict[str, list[dict]] = {}
        self.domain_bindings: set[tuple[str, str]] = set()  # (user_id, domain)
        self.site_admins: set[str] = set()
        # 对抗审查修复⑪对齐：kb_users 镜像（user_id -> status；未登记=None）
        self.users: dict[str, str] = {}

    def _touch(self, artifact_id: str) -> None:
        art = self.artifacts[artifact_id]
        art["updated_at"] = datetime.now(timezone.utc)

    def _assert_expected(self, artifact_id: str, expected_updated_at) -> None:
        """对齐真库（codex二审批号⑥b）：写路径原子乐观锁——全精度比对
        updated_at，失配 stale_artifact。"""
        if expected_updated_at is None:
            return
        art = self.artifacts.get(artifact_id)
        if art is None:
            raise ArtifactRepoError("artifact_not_found")
        if art["updated_at"] != expected_updated_at:
            raise ArtifactRepoError("stale_artifact")

    # ---- 基础读写 --------------------------------------------------------

    async def create_artifact(
        self, *, domain_id: str, kind: str = "table", title: str,
        description: str = "", owner_user_id: str, artifact_id: str | None = None,
        usage_note: str = "", visibility: str = "private",
    ) -> dict:
        # 默认 id 与真库同规则（art_+hex）——单测试内多次 create 不互踩
        aid = artifact_id or f"art_{uuid.uuid4().hex[:12]}"
        row = {
            "artifact_id": aid, "domain_id": domain_id, "kind": kind,
            "title": title, "description": description, "usage_note": usage_note,
            "owner_user_id": owner_user_id, "visibility": visibility,
            "status": "draft",
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

    async def readable_artifact_domain(
        self, artifact_id: str, user_id: str,
    ) -> str | None:
        """008 钥匙候选校验桩：成员/owner/public 且域绑定 → domain_id。"""
        art = self.artifacts.get(artifact_id)
        if art is None:
            return None
        if (user_id, art["domain_id"]) not in self.domain_bindings:
            return None
        if art.get("visibility") == "public":
            return art["domain_id"]
        members = self.members.get(artifact_id, {})
        if members.get(user_id) or art.get("owner_user_id") == user_id:
            return art["domain_id"]
        return None

    async def update_artifact_meta(
        self, artifact_id: str, *, title=None, description=None,
        usage_note=None, visibility=None,
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
        if visibility is not None:
            art["visibility"] = visibility
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
        self.revisions.pop(artifact_id, None)
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
        """codex三审 P2-2 对齐真库：成员行在但当前域绑定已撤 → 不出现在列表。"""
        out = []
        for aid, roles in self.members.items():
            if user_id in roles and aid in self.artifacts:
                art = self.artifacts[aid]
                if (user_id, art["domain_id"]) not in self.domain_bindings:
                    continue
                out.append({**art, "my_role": roles[user_id]})
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

    async def get_user_status(self, user_id: str) -> str | None:
        """对齐真库：未登记（kb_users 无行/软删）→ None。"""
        return self.users.get(user_id)

    async def is_domain_admin(self, user_id: str, domain_id: str) -> bool:
        return (user_id, domain_id) in self.domain_admins

    async def user_in_domain(self, user_id: str, domain_id: str) -> bool:
        return (user_id, domain_id) in self.domain_bindings

    # ---- 生命周期 ---------------------------------------------------------

    def _normalize_alias_keys(self, artifact_id: str) -> None:
        """codex三审 P1-2b 对齐真库（COALESCE 口径）：发布定格前对 rev0 行做
        alias 旧名→当前名归一——行同时含新旧键时保留新键值、仅清旧键。"""
        for col in self.columns.get(artifact_id, []):
            current_name = str(col.get("column_name"))
            for alias_name in (col.get("alias") or []):
                alias_name = str(alias_name)
                if not alias_name or alias_name == current_name:
                    continue
                for row in self.rev0_rows.get(artifact_id, []):
                    data = row["data"]
                    if alias_name in data:
                        value = data.get(current_name, data[alias_name])
                        row["data"] = {
                            **{k: v for k, v in data.items() if k != alias_name},
                            current_name: value,
                        }

    async def publish(
        self, artifact_id: str, change_summary: str, published_by: str,
        expected_updated_at=None,
    ) -> dict:
        self.calls.append("publish")
        art = self.artifacts[artifact_id]
        self._assert_expected(artifact_id, expected_updated_at)
        if not (art.get("usage_note") or "").strip():
            raise ArtifactRepoError("usage_note_required")
        # 对齐真库（对抗审查修复③）：干净态发布门禁
        if not art.get("draft_open") and int(art["current_revision"]) > 0:
            raise ArtifactRepoError("no_pending_changes")
        self._normalize_alias_keys(artifact_id)
        history = [r["revision"] for r in self.revisions.get(artifact_id, [])]
        row_revs = list(self.revision_rows.get(artifact_id, {}).keys())
        new_rev = max([int(art["current_revision"]), *history, *row_revs], default=0) + 1
        rows = self.rev0_rows.get(artifact_id, [])
        self.revision_rows.setdefault(artifact_id, {})[new_rev] = [dict(r) for r in rows]
        self.revision_schemas.setdefault(artifact_id, {})[new_rev] = [
            dict(c) for c in self.columns.get(artifact_id, [])]
        self.revisions.setdefault(artifact_id, []).append({
            "revision": new_rev, "row_count": len(rows),
            "change_summary": change_summary, "published_by": published_by,
            "published_at": NOW,
        })
        self.rev0_rows[artifact_id] = []
        art.update(
            current_revision=new_rev, status="published", draft_open=False,
        )
        self._touch(artifact_id)
        return {"revision": new_rev, "row_count": len(rows)}

    async def list_revisions(self, artifact_id: str) -> list[dict]:
        # 与真库一致：revision 倒序
        return [dict(r) for r in reversed(self.revisions.get(artifact_id, []))]

    async def get_revision(self, artifact_id: str, revision: int) -> dict | None:
        if revision not in self.revision_rows.get(artifact_id, {}):
            return None
        # 发布路径产出的 revision 读定格快照；直 plant revision_rows 的测试
        # 形态无快照 → 回落实时列（与旧行为兼容）
        schema = self.revision_schemas.get(artifact_id, {}).get(revision)
        if schema is None:
            schema = [dict(c) for c in self.columns.get(artifact_id, [])]
        return {
            "artifact_id": artifact_id, "revision": revision,
            "schema_json": [dict(c) for c in schema],
            "row_count": len(self.revision_rows[artifact_id][revision]),
        }

    async def rollback_to(
        self, artifact_id: str, revision: int, expected_updated_at=None,
    ) -> dict:
        """对齐真库（对抗审查修复④）：仅校验 revision 存在于版本历史——
        「数据不动」语义下拨任何历史版（含 > current）都安全。"""
        self.calls.append("rollback_to")
        art = self.artifacts[artifact_id]
        self._assert_expected(artifact_id, expected_updated_at)
        known = {r["revision"] for r in self.revisions.get(artifact_id, [])} \
            | set(self.revision_rows.get(artifact_id, {}).keys())
        if revision < 1 or revision not in known:
            raise ArtifactRepoError("invalid_revision")
        art["current_revision"] = revision
        art["status"] = "published"
        self._touch(artifact_id)
        return dict(art)

    async def deprecate(self, artifact_id: str, expected_updated_at=None) -> dict:
        art = self.artifacts[artifact_id]
        self._assert_expected(artifact_id, expected_updated_at)
        # 008：draft/published 双源可停用（废稿也要能走 停用→删除 两级确认）
        if art["status"] not in ("draft", "published"):
            raise ArtifactRepoError("invalid_status")
        art["status"] = "deprecated"
        self._touch(artifact_id)
        return dict(art)

    async def republish(self, artifact_id: str, expected_updated_at=None) -> dict:
        art = self.artifacts[artifact_id]
        self._assert_expected(artifact_id, expected_updated_at)
        if art["status"] != "deprecated":
            raise ArtifactRepoError("invalid_status")
        # 008：从未发布过（current_revision=0）恢复回 draft，发布过回 published
        art["status"] = "published" if art["current_revision"] > 0 else "draft"
        self._touch(artifact_id)
        return dict(art)

    async def discard_draft(self, artifact_id: str, expected_updated_at=None) -> dict:
        self.calls.append("discard_draft")
        art = self.artifacts[artifact_id]
        self._assert_expected(artifact_id, expected_updated_at)
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
            # 对齐真库（codex二审批号⑥裁定）：checkout 不推进乐观锁令牌——
            # rev0 是 current 逐字节副本，内容未变。
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
        *, rename_from: str | None = None, expected_updated_at=None,
    ) -> dict:
        """对齐真库（codex二审批号①②）：改名时同"事务"迁移 rev0 data 键 +
        恒推进 updated_at（乐观锁令牌随 schema 变更前进）；codex三审 P1-4：
        expected_updated_at 原子校验。"""
        self._assert_expected(artifact_id, expected_updated_at)
        cols = self.columns.setdefault(artifact_id, [])
        for col in cols:
            if col["column_id"] == column_id:
                col.update(fields)
                if rename_from is not None:
                    for row in self.rev0_rows.get(artifact_id, []):
                        if rename_from in row["data"]:
                            row["data"][fields["column_name"]] = \
                                row["data"].pop(rename_from)
                self._touch(artifact_id)
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
        self._touch(artifact_id)
        return dict(row)

    async def drop_column(self, artifact_id: str, column_id: str) -> None:
        for col in self.columns.get(artifact_id, []):
            if col["column_id"] == column_id:
                col["status"] = "dropped"
                col["is_primary_key"] = False

    async def set_primary_key(
        self, artifact_id: str, column_ids: list[str], expected_updated_at=None,
    ) -> dict:
        """对齐真库（对抗审查修复②）：按 column_name 重算 rev0 全部
        row_key；两行折到同键 → primary_key_conflict。"""
        self.calls.append("set_primary_key")
        if artifact_id not in self.artifacts:
            raise ArtifactRepoError("artifact_not_found")
        self._assert_expected(artifact_id, expected_updated_at)
        active = [
            c for c in self.columns.get(artifact_id, [])
            if c.get("status") != "dropped"
        ]
        by_id = {str(c["column_id"]): c for c in active}
        if any(cid not in by_id for cid in column_ids):
            raise ArtifactRepoError("unknown_column")
        pk_names = [str(by_id[cid]["column_name"]) for cid in column_ids]
        rekeyed = 0
        seen: set[str] = set()
        for row in self.rev0_rows.get(artifact_id, []):
            new_key = _row_key_value(row["data"], pk_names)
            if new_key in seen:
                raise ArtifactRepoError("primary_key_conflict")
            seen.add(new_key)
            if new_key != row["row_key"]:
                row["row_key"] = new_key
                rekeyed += 1
        for col in self.columns.get(artifact_id, []):
            col["is_primary_key"] = col["column_id"] in column_ids
        return {"artifact_id": artifact_id, "primary_key": list(column_ids),
                "rekeyed_rows": rekeyed}

    async def replace_columns_for_artifact(
        self, artifact_id: str, columns: list[dict], expected_updated_at=None,
    ) -> None:
        self.calls.append("replace_columns_for_artifact")
        self._assert_expected(artifact_id, expected_updated_at)
        self.columns[artifact_id] = [dict(c) for c in columns]
        self._touch(artifact_id)   # 对齐真库：schema 变更推进乐观锁令牌

    async def insert_rows_upsert(
        self, artifact_id: str, revision: int, rows: list[dict], updated_by: str = "",
        expected_updated_at=None,
    ) -> dict:
        self.calls.append("insert_rows_upsert")
        self._assert_expected(artifact_id, expected_updated_at)
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
        expected_updated_at=None,
    ) -> dict | None:
        """对齐真库（codex二审批号⑥b）：原子乐观锁 + 写 + touch 一步完成。"""
        self.calls.append("update_cell")
        self._assert_expected(artifact_id, expected_updated_at)
        for row in self.rev0_rows.get(artifact_id, []):
            if row["row_key"] == row_key:
                row["data"] = {**row["data"], column: value}
                self._touch(artifact_id)
                return dict(row)
        return None

    async def get_row(
        self, artifact_id: str, revision: int, row_key: str,
    ) -> dict | None:
        if revision == 0:
            rows = self.rev0_rows.get(artifact_id, [])
        else:
            rows = self.revision_rows.get(artifact_id, {}).get(revision, [])
        for row in rows:
            if row["row_key"] == row_key:
                return dict(row)
        return None

    async def update_cell_conflict(
        self, artifact_id: str, row_key: str, column: str,
        marker: str, updated_by: str, expected_updated_at=None,
    ) -> dict | None:
        """类型校验失败的单格更新：值置 null + conflicts[column]=marker。"""
        self.calls.append("update_cell_conflict")
        self._assert_expected(artifact_id, expected_updated_at)
        for row in self.rev0_rows.get(artifact_id, []):
            if row["row_key"] == row_key:
                row["data"] = {**row["data"], column: None}
                row["conflicts"] = {**row.get("conflicts", {}), column: marker}
                self._touch(artifact_id)
                return dict(row)
        return None

    async def batch_assign(
        self, artifact_id: str, column: str, value, where_clauses: list, updated_by: str,
        expected_updated_at=None,
    ) -> int:
        self.calls.append("batch_assign")
        self._assert_expected(artifact_id, expected_updated_at)
        n = 0
        for row in self.rev0_rows.get(artifact_id, []):
            if self._match(row["data"], where_clauses):
                row["data"] = {**row["data"], column: value}
                n += 1
        self._touch(artifact_id)
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

    async def delete_row(
        self, artifact_id: str, row_key: str, expected_updated_at=None,
    ) -> bool:
        self.calls.append("delete_row")
        self._assert_expected(artifact_id, expected_updated_at)
        rows = self.rev0_rows.get(artifact_id, [])
        kept = [r for r in rows if r["row_key"] != row_key]
        if len(kept) == len(rows):
            return False
        self.rev0_rows[artifact_id] = kept
        self._touch(artifact_id)
        return True

    async def list_rows(
        self, artifact_id: str, revision: int, page: int = 1, size: int = 50,
        sort_ordinal: bool = True,
    ) -> dict:
        """codex三审 P1-2b 对齐真库：返回前做 alias 旧名→当前名读取投影
        （当前名键缺失且旧名键存在——仅视图改名，不改存储）。"""
        if revision == 0:
            rows = self.rev0_rows.get(artifact_id, [])
        else:
            rows = self.revision_rows.get(artifact_id, {}).get(revision, [])
        alias_map = {
            str(old): str(col["column_name"])
            for col in self.columns.get(artifact_id, [])
            if col.get("status") != "dropped"
            for old in (col.get("alias") or [])
            if str(old) and str(old) != str(col["column_name"])
        }
        out: list[dict] = []
        for r in rows:
            row = dict(r)
            data = row.get("data")
            if isinstance(data, dict) and alias_map:
                for old_name, current_name in alias_map.items():
                    if old_name in data and current_name not in data:
                        data = {
                            **{k: v for k, v in data.items() if k != old_name},
                            current_name: data[old_name],
                        }
                row["data"] = data
            out.append(row)
        return {"rows": out, "total": len(out)}

    async def count_rows(self, artifact_id: str, revision: int) -> int:
        if revision == 0:
            return len(self.rev0_rows.get(artifact_id, []))
        return len(self.revision_rows.get(artifact_id, {}).get(revision, []))

    async def diff_draft_vs_current(self, artifact_id: str) -> dict:
        """对齐真库（对抗审查修复③）：draft_open=FALSE 且 current>0 = 干净态
        → 零差异投影（与 list_rows 缺省 draft 视图同口径）。"""
        self.calls.append("diff_draft_vs_current")
        art = self.artifacts[artifact_id]
        cur = art["current_revision"]
        if not art.get("draft_open") and cur > 0:
            return {
                "added": [], "removed": [], "changed": [],
                "added_count": 0, "removed_count": 0, "changed_count": 0,
            }
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
    # codex二审批号⑧：非 admin 成员路径需当前域绑定——成员 fixture 用户全部
    # 绑 plant-a（u-stranger 也绑定：保持「同域非成员 404」的锐利口径——
    # 404 来自成员检查而非域绑定缺失）。
    db.domain_bindings.update(
        (uid, "plant-a") for uid in
        ("u-owner", "u-editor", "u-viewer", "u-stranger", "u-maker")
    )
    # 成员治理（修复⑪）预检底料：常见 fixture 用户均 active
    db.users.update({
        uid: "active" for uid in (
            "u-owner", "u-editor", "u-viewer", "u-new", "u-domadmin",
            "u-siteadmin", "u-owner2",
        )
    })
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
    db.artifacts[AID]["status"] = "deprecated"  # 删除门禁：先停用才可删（008）
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
    # 本域：全权（发布/成员/删除路径都放行到 db）——发布前先检出 draft
    # （对抗审查修复③：干净态 current>0 发布 → no_pending_changes）
    await db.ensure_draft(AID)
    await svc.publish(user=domadmin, artifact_id=AID, change_summary="域管理员发布")
    await svc.upsert_member(
        user=domadmin, artifact_id=AID, member_user_id="u-new", role="editor",
    )
    db.artifacts[AID]["status"] = "deprecated"  # 删除门禁：先停用才可删（008）
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
    db.artifacts[AID]["status"] = "deprecated"  # 删除门禁：先停用才可删（008）
    await svc.delete_artifact(user=owner, artifact_id=AID)


# ------------------------------------------------ 008：删除门禁与可见性模型

@pytest.mark.asyncio
async def test_delete_requires_deprecated_gate() -> None:
    """删除=级联硬删不可恢复：draft/published 状态一律 409 口径拒绝
    （artifact_not_deprecated），停用后才放行。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    with pytest.raises(InvalidOperation) as ei:
        await svc.delete_artifact(user=owner, artifact_id=AID)
    assert str(ei.value) == "artifact_not_deprecated"
    assert await db.get_artifact(AID) is not None  # 未删
    db.artifacts[AID]["status"] = "deprecated"
    await svc.delete_artifact(user=owner, artifact_id=AID)
    assert await db.get_artifact(AID) is None


@pytest.mark.asyncio
async def test_public_visibility_read_only_for_domain_user() -> None:
    """008 可见性：private 非成员 404；public 本域绑定用户合成 viewer——
    可读详情/行，写（编辑/翻转可见性）403；owner 可翻 public/private。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    stranger = _user("u-stranger")  # 域内绑定（_seeded_db 绑定面）但非成员
    with pytest.raises(ArtifactNotFound):
        await svc.get_artifact(user=stranger, artifact_id=AID)

    db.artifacts[AID]["visibility"] = "public"
    detail = await svc.get_artifact(user=stranger, artifact_id=AID)
    assert detail["my_role"] == "viewer"
    # 读面放行（运行态行）
    assert (await svc.list_rows(user=stranger, artifact_id=AID))["total"] == 2
    # 写面拒绝：编辑格 / 改元信息（含翻转可见性——owner 专属）
    with pytest.raises(ArtifactForbidden):
        await svc.update_cell(
            user=stranger, artifact_id=AID, row_key="MOP-1",
            column="L1", value="越权",
        )
    with pytest.raises(ArtifactForbidden):
        await svc.update_meta(
            user=stranger, artifact_id=AID, fields={"title": "改名"})

    # editor 也不能翻转可见性（治理决策 owner 专属），但可改标题
    db.members.setdefault(AID, {})["u-editor"] = "editor"
    editor = _user("u-editor")
    with pytest.raises(ArtifactForbidden):
        await svc.update_meta(
            user=editor, artifact_id=AID, fields={"visibility": "private"})
    updated = await svc.update_meta(
        user=editor, artifact_id=AID, fields={"title": "编辑可改名"})
    assert updated["title"] == "编辑可改名"

    # owner 翻回 private → 非成员立刻 404（读时校验，无清理窗口）
    owner = _user("u-owner")
    await svc.update_meta(user=owner, artifact_id=AID,
                          fields={"visibility": "private"})
    with pytest.raises(ArtifactNotFound):
        await svc.get_artifact(user=stranger, artifact_id=AID)


@pytest.mark.asyncio
async def test_readable_artifact_domain_drives_key_candidates() -> None:
    """钥匙候选=读可见面：public/成员/owner 三来源可读；撤域后即使 public
    也不可读（与列表/详情口径一致）。"""
    db = await _seeded_db()
    assert await db.readable_artifact_domain(AID, "u-owner") == "plant-a"
    db.artifacts[AID]["visibility"] = "public"
    assert await db.readable_artifact_domain(AID, "u-stranger") == "plant-a"
    db.artifacts[AID]["visibility"] = "private"
    assert await db.readable_artifact_domain(AID, "u-stranger") is None
    # 撤域：owner 也不可读（域绑定是可见性前置）
    db.domain_bindings.discard(("u-owner", "plant-a"))
    assert await db.readable_artifact_domain(AID, "u-owner") is None


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
    # codex二审批号⑥a：全精度比较——令牌必须逐微秒匹配（秒截断容差已除）
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练",
        expected_updated_at=NOW.isoformat(),
    )
    fresh = db.artifacts[AID]["updated_at"].isoformat()
    # 不传 → 放行
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练2",
    )
    assert "update_cell" in db.calls
    assert fresh != NOW_ISO   # 写路径推进了令牌（真时间）


@pytest.mark.asyncio
async def test_same_second_different_microsecond_token_rejected() -> None:
    """codex二审批号⑥a回归：秒截断容差已除——同秒不同微秒的旧 token 拒绝
    （NOW 带 .5 微秒，秒截断版 = 不同令牌）。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    with pytest.raises(StaleArtifact):
        await svc.update_cell(
            user=_user("u-owner"), artifact_id=AID, row_key="MOP-1",
            column="L1", value="x", expected_updated_at=NOW_ISO,
        )
    assert "update_cell" not in db.calls


@pytest.mark.asyncio
async def test_write_advances_token_old_token_rejected_on_second_edit() -> None:
    """codex二审批号⑥回归：写路径推进乐观锁令牌——第一次编辑后用编辑前的
    token 再写 → StaleArtifact（409）。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    token_before = db.artifacts[AID]["updated_at"].isoformat()
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="第一次",
    )
    assert db.artifacts[AID]["updated_at"].isoformat() != token_before
    with pytest.raises(StaleArtifact):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="L1",
            value="第二次", expected_updated_at=token_before,
        )


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
    """对抗审查修复②回归：改主键按 column_name 重算键——两行折到同键 →
    409 PrimaryKeyConflict；组合主键重算键值来自列名而非 column_id。"""
    db = await _seeded_db()
    await db.ensure_draft(AID)   # current(rev1) → rev0，行编辑才有效
    # 制造折叠：两行 L1 同值，主键换成 c002（L1）
    db.rev0_rows[AID][0]["data"]["L1"] = "部署"
    db.rev0_rows[AID][1]["data"]["L1"] = "部署"
    svc = ArtifactService(db)
    with pytest.raises(PrimaryKeyConflict):
        await svc.set_primary_key(
            user=_user("u-owner"), artifact_id=AID, column_ids=["c002"],
        )
    # 组合主键 c001+c002：键=MOP值+L1值 hex 编码 '.' 连接（列名取数），不折叠
    db.rev0_rows[AID][1]["data"]["L1"] = "扩容"
    result = await svc.set_primary_key(
        user=_user("u-owner"), artifact_id=AID, column_ids=["c001", "c002"],
    )
    assert result["rekeyed_rows"] == 2
    assert {r["row_key"] for r in db.rev0_rows[AID]} \
        == {"4d4f502d31.e983a8e7bdb2", "4d4f502d32.e689a9e5aeb9"}
    # 未知列 → 400 InvalidOperation
    with pytest.raises(InvalidOperation):
        await svc.set_primary_key(
            user=_user("u-owner"), artifact_id=AID, column_ids=["c999"],
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


# ------------------------------------------------------- update_cell 类型校验

@pytest.mark.asyncio
async def test_update_cell_int_float_cast_marks_conflict() -> None:
    """int/float 列 cast 失败 → 值置 null + conflicts 标记 "type_error:值"
    （与导入 tolerant 路径对称），不阻断；可 cast 字符串存 cast 值；
    text / 无列定义的裸列不设卡；行不存在仍 400。"""
    db = await _seeded_db()
    await db.upsert_column(AID, "c003", {"column_name": "数量", "column_type": "int"})
    await db.upsert_column(AID, "c004", {"column_name": "单价", "column_type": "float"})
    await db.upsert_column(AID, "c007", {"column_name": "备注", "column_type": "text"})
    svc = ArtifactService(db)
    owner = _user("u-owner")

    # 可 cast 字符串 → 数值落库
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="数量", value=" 3 ")
    assert row["data"]["数量"] == 3
    # 置空放行（无 conflicts 标记）；空白串与导入 _is_blank 同口径=合法置空
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="数量", value=None)
    assert row["data"]["数量"] is None
    assert "数量" not in row["conflicts"]
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="数量", value="  ")
    assert row["data"]["数量"] is None
    assert "数量" not in row["conflicts"]
    # int cast 失败 → null + 标记，不抛错
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="数量", value="abc")
    assert row["data"]["数量"] is None
    assert row["conflicts"]["数量"] == "type_error:abc"
    # float cast 失败同口径
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="单价", value="3.5x")
    assert row["data"]["单价"] is None
    assert row["conflicts"]["单价"] == "type_error:3.5x"
    # text 列不设卡（登记列直通；主键列 MOP 已被批号⑦拦截，用非主键 text 列）；
    # 未登记列 → 400 unknown_column（修复⑩）
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="备注", value="x")
    assert row["data"]["备注"] == "x"
    with pytest.raises(InvalidOperation, match="unknown_column"):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="自由列", value="x")
    # 行不存在仍 row_not_found（400），类型校验不掩盖
    with pytest.raises(InvalidOperation):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-404",
            column="数量", value="abc")


# --------------------------------------------- 对抗审查修复回归（61号 21项）

@pytest.mark.asyncio
async def test_row_write_defenses_formula_and_unknown_column() -> None:
    """修复⑩回归：三个行写入口（update_cell/batch_assign/insert_single_row）
    统一防线——公式注入（=+-@ 前缀）400 / 未知列 400。"""
    db = await _seeded_db()
    await db.upsert_column(AID, "c003", {"column_name": "数量", "column_type": "int"})
    svc = ArtifactService(db)
    owner = _user("u-owner")

    # update_cell：公式 400（= + - @ 四前缀）
    for formula in ("=SUM(A1)", "+1", " -1+1 ", "@x"):
        with pytest.raises(InvalidOperation, match="formula_not_allowed"):
            await svc.update_cell(
                user=owner, artifact_id=AID, row_key="MOP-1",
                column="L1", value=formula)
    # update_cell：未知列 400（不再照写影子键）
    with pytest.raises(InvalidOperation, match="unknown_column"):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="不存在列", value="x")
    # batch_assign：公式 400 / 未知列 400 / int 列 cast 失败 strict 400
    with pytest.raises(InvalidOperation, match="formula_not_allowed"):
        await svc.batch_assign(
            user=owner, artifact_id=AID, column="L1", value="@cmd", where_clauses=[])
    with pytest.raises(InvalidOperation, match="unknown_column"):
        await svc.batch_assign(
            user=owner, artifact_id=AID, column="不存在列", value="x", where_clauses=[])
    with pytest.raises(InvalidOperation, match="type_error"):
        await svc.batch_assign(
            user=owner, artifact_id=AID, column="数量", value="abc", where_clauses=[])
    # insert_single_row：公式 400 / 未知键 400
    with pytest.raises(InvalidOperation, match="formula_not_allowed"):
        await svc.insert_single_row(
            user=owner, artifact_id=AID, data={"MOP": "=1+1"})
    with pytest.raises(InvalidOperation, match="unknown_column"):
        await svc.insert_single_row(
            user=owner, artifact_id=AID, data={"幽灵列": "x"})


@pytest.mark.asyncio
async def test_insert_single_row_alias_canonical_and_tolerant_cast() -> None:
    """修复⑨⑩回归：alias 旧名归一到规范列名落库（不写影子键，row_key 用
    主键列名拼接）；int cast 失败置 null+conflicts（与 update_cell 同口径）。"""
    db = await _seeded_db()
    # 改名 c001 MOP→MOP单号，alias 留旧名（§7.1）
    await db.upsert_column(AID, "c001", {"column_name": "MOP单号", "alias": ["MOP"]})
    await db.upsert_column(AID, "c003", {"column_name": "数量", "column_type": "int"})
    svc = ArtifactService(db)
    owner = _user("u-owner")

    row = await svc.insert_single_row(
        user=owner, artifact_id=AID,
        data={"MOP": "NC-NEW", "L1": "部署", "数量": "abc"},
    )
    assert "MOP" not in row["data"]          # 旧名不落影子键
    assert row["data"]["MOP单号"] == "NC-NEW"  # 归一到规范名
    assert row["row_key"] == "4e432d4e4557"   # 主键拼接用列名取数（hex 编码）
    assert row["data"]["数量"] is None         # tolerant：置 null + 标记
    assert row["conflicts"]["数量"] == "type_error:abc"


@pytest.mark.asyncio
async def test_update_cell_date_bool_cast_marks_conflict() -> None:
    """修复⑫回归：_cast_cell 扩展 date/bool 分支（与导入 _cast_value 同源）——
    可解归一存储，失败置 null+conflicts。"""
    db = await _seeded_db()
    await db.upsert_column(AID, "c005", {"column_name": "日期", "column_type": "date"})
    await db.upsert_column(AID, "c006", {"column_name": "开关", "column_type": "bool"})
    svc = ArtifactService(db)
    owner = _user("u-owner")

    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="日期", value="2026-06-01")
    assert row["data"]["日期"] == "2026-06-01"
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="日期", value="not-a-date")
    assert row["data"]["日期"] is None
    assert row["conflicts"]["日期"] == "type_error:not-a-date"

    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="开关", value="true")
    assert row["data"]["开关"] is True
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="开关", value=1)
    assert row["data"]["开关"] is True
    row = await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="开关", value="maybe")
    assert row["data"]["开关"] is None
    assert row["conflicts"]["开关"] == "type_error:maybe"


@pytest.mark.asyncio
async def test_publish_clean_state_raises_no_pending_changes() -> None:
    """修复③回归：draft_open=FALSE 且 current>0（干净态）→ NoPendingChanges；
    干净态 diff 零差异投影；检出草稿后放行；从未发布（current=0）不受门禁。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")

    with pytest.raises(NoPendingChanges):
        await svc.publish(user=owner, artifact_id=AID, change_summary="x")
    diff = await svc.diff_draft_vs_current(user=owner, artifact_id=AID)
    assert diff == {"added": [], "removed": [], "changed": [],
                    "added_count": 0, "removed_count": 0, "changed_count": 0}

    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="演练")
    result = await svc.publish(user=owner, artifact_id=AID, change_summary="r2")
    assert result["revision"] == 2

    # 从未发布制品首次发布不受门禁（rev0 即首发工作区）
    await db.create_artifact(
        domain_id="plant-a", title="新制品", owner_user_id="u-owner",
        artifact_id="art_new", usage_note="u",
    )
    result = await svc.publish(user=owner, artifact_id="art_new", change_summary="首版")
    assert result["revision"] == 1


@pytest.mark.asyncio
async def test_rollback_beyond_current_allowed_when_revision_exists() -> None:
    """修复④回归：拨回 > current 的历史版本放行（「数据不动」语义下拨任何
    历史版都安全）；不存在的 revision 仍 400。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")

    await db.ensure_draft(AID)
    await svc.publish(user=owner, artifact_id=AID, change_summary="r2")   # → rev2
    rolled = await svc.rollback_to(user=owner, artifact_id=AID, revision=1)
    assert rolled["current_revision"] == 1
    rolled = await svc.rollback_to(user=owner, artifact_id=AID, revision=2)  # >current
    assert rolled["current_revision"] == 2
    with pytest.raises(InvalidOperation, match="invalid_revision"):
        await svc.rollback_to(user=owner, artifact_id=AID, revision=99)


@pytest.mark.asyncio
async def test_upsert_member_user_status_and_last_owner_governance() -> None:
    """修复⑪回归：不存在/非 active 用户 400；last-owner 降级 403；
    第二个 owner 存在时降级放行。"""
    db = await _seeded_db()
    db.users["u-disabled"] = "disabled"
    svc = ArtifactService(db)
    owner = _user("u-owner")

    with pytest.raises(InvalidOperation, match="invalid_member_user"):
        await svc.upsert_member(
            user=owner, artifact_id=AID, member_user_id="u-ghost", role="viewer")
    with pytest.raises(InvalidOperation, match="invalid_member_user"):
        await svc.upsert_member(
            user=owner, artifact_id=AID, member_user_id="u-disabled", role="viewer")
    # last-owner 降级 → 403
    with pytest.raises(ArtifactForbidden):
        await svc.upsert_member(
            user=owner, artifact_id=AID, member_user_id="u-owner", role="editor")
    # 加第二个 owner 后降级放行
    db.users["u-owner2"] = "active"
    await svc.upsert_member(
        user=owner, artifact_id=AID, member_user_id="u-owner2", role="owner")
    await svc.upsert_member(
        user=owner, artifact_id=AID, member_user_id="u-owner2", role="editor")
    assert db.members[AID]["u-owner2"] == "editor"


@pytest.mark.asyncio
async def test_upsert_column_rejects_duplicate_active_name() -> None:
    """修复⑳回归：新建/改名撞既有 active 列名 → 400（DB 部分唯一索引兜底）；
    自身保持原名（幂等 PATCH）放行。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")

    with pytest.raises(InvalidOperation, match="duplicate_column_name"):
        await svc.upsert_column(
            user=owner, artifact_id=AID, column_id="c009",
            fields={"column_name": "MOP"})
    with pytest.raises(InvalidOperation, match="duplicate_column_name"):
        await svc.upsert_column(
            user=owner, artifact_id=AID, column_id="c002",
            fields={"column_name": "MOP"})
    col = await svc.upsert_column(
        user=owner, artifact_id=AID, column_id="c002", fields={"column_name": "L1"})
    assert col["column_name"] == "L1"


@pytest.mark.asyncio
async def test_replace_columns_rejects_in_plan_duplicate_names() -> None:
    """修复⑳回归：replace_columns（首次导入建列）计划内列名重复 → 400。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    with pytest.raises(InvalidOperation, match="duplicate_column_name"):
        await svc.replace_columns(
            user=_user("u-owner"), artifact_id=AID, columns=[
                {"column_id": "c001", "column_name": "同名列", "column_type": "text"},
                {"column_id": "c002", "column_name": "同名列", "column_type": "text"},
            ])


# ------------------------------------------- codex 二审九项回归

@pytest.mark.asyncio
async def test_rename_column_appends_alias_and_migrates_data_keys() -> None:
    """codex二审批号②回归（用例⑨闭环）：service 层列改名 → alias 追加旧名 +
    rev0 data 键同事务迁移（rename_from 透传）——新键有值、旧键无残留。
    Java/serving 读取侧经 alias 映射旧名→新名定位列，行 data 已是新键，
    读取闭环无需 Java 改动。"""
    db = await _seeded_db()
    await db.ensure_draft(AID)   # current(rev1) → rev0，行带 MOP/L1 键
    svc = ArtifactService(db)
    col = await svc.upsert_column(
        user=_user("u-owner"), artifact_id=AID, column_id="c002",
        fields={"column_name": "一级分类"},
    )
    assert col["column_name"] == "一级分类"
    assert col["alias"] == ["L1"]      # 读回 alias==[旧名]（批号①序列化闭环）
    rows = {r["row_key"]: r for r in db.rev0_rows[AID]}
    assert rows["MOP-1"]["data"]["一级分类"] == "部署"
    assert "L1" not in rows["MOP-1"]["data"]
    listed = (await svc.list_rows(
        user=_user("u-owner"), artifact_id=AID, draft=True))["rows"]
    assert all("一级分类" in r["data"] and "L1" not in r["data"]
               for r in listed)


@pytest.mark.asyncio
async def test_primary_key_column_edits_rejected() -> None:
    """codex二审批号⑦回归：update_cell/batch_assign 改主键列 → 400
    primary_key_immutable（row_key 按主键值编码，改值=行地址脱钩；
    改主键走「设置主键」整体重算行键）。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    with pytest.raises(InvalidOperation, match="primary_key_immutable"):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="MOP", value="X")
    with pytest.raises(InvalidOperation, match="primary_key_immutable"):
        await svc.batch_assign(
            user=owner, artifact_id=AID, column="MOP", value="X", where_clauses=[])
    assert "update_cell" not in db.calls
    assert "batch_assign" not in db.calls


@pytest.mark.asyncio
async def test_member_without_domain_binding_gets_404() -> None:
    """codex二审批号⑧回归：成员行在、当前域绑定已撤 → ArtifactNotFound
    （404 反探测，域外不可见，与 KB 面硬边界对齐）；域绑定在 → 正常。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    viewer = _user("u-viewer")
    assert (await svc.get_artifact(user=viewer, artifact_id=AID))["artifact_id"] == AID
    db.domain_bindings.discard(("u-viewer", "plant-a"))
    with pytest.raises(ArtifactNotFound):
        await svc.get_artifact(user=viewer, artifact_id=AID)
    with pytest.raises(ArtifactNotFound):
        await svc.update_cell(
            user=viewer, artifact_id=AID, row_key="MOP-1", column="L1", value="x")


def test_row_key_value_hex_encoding_gateway_safe() -> None:
    """codex三审 P1-1 回归：row_key 改纯 URL 安全 ASCII（hex）——网关
    （main_control_service/auth.py _has_unsafe_proxy_path）拒绝代理路径一切
    % 编码字节，旧长度前缀 ``3:ABC`` 经 encodeURIComponent 即 ``3%3AABC`` 被
    400，中文主键值同理必炸。hex 输出恒匹配 ``^[0-9a-f.]+$``（encodeURIComponent
    不转义），且 '.' 连接无歧义（hex 字符集不含 '.'）。"""
    import re

    # 无歧义：'/'、':'、'%'、中文值的两两组合不同键；输出恒网关安全
    safe = re.compile(r"^[0-9a-f.]*$")
    samples = [
        ({"x": "A/B", "y": "C"}, "412f42.43"),
        ({"x": "A", "y": "B/C"}, "41.422f43"),
        ({"x": "a:b%c", "y": "d"}, "613a622563.64"),
        ({"x": "旧名"}, "e697a7e5908d"),            # 中文主键不再必炸
        ({"x": "ABC"}, "414243"),                    # 单列同构
        ({"x": None, "y": "C"}, ".43"),              # None → 空串 hex 段
    ]
    for data, expected in samples:
        key = _row_key_value(data, list(data))
        assert key == expected
        assert safe.match(key), key
    assert _row_key_value({"x": "A/B", "y": "C"}, ["x", "y"]) \
        != _row_key_value({"x": "A", "y": "B/C"}, ["x", "y"])
    # 分隔无歧义：hex 输出不含 '.'，'.' 连接可逆不碰撞
    assert _encode_key_parts(["a.b", "c"]) == "612e62.63"
    assert _encode_key_parts(["a", "b.c"]) == "61.622e63"
    assert _encode_key_parts(["a.b", "c"]) != _encode_key_parts(["a", "b.c"])


# ------------------------------------------- 仓储 SQL 静态守卫（非门控）

class _RecordingCursor:
    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows if rows is not None else [{"_inserted": True}]

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self) -> int:
        return len(self._rows)


class _TransactionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingConnection:
    def __init__(self, log: list, rows: list | None = None) -> None:
        self._log = log
        self._rows = rows

    async def execute(self, query, params=()):
        if hasattr(query, "as_string"):   # psycopg sql.Composed/SQL → 渲染真 SQL
            query = query.as_string(None)
        self._log.append((str(query), params))
        return _RecordingCursor(self._rows)

    def transaction(self) -> _TransactionContext:
        return _TransactionContext()


class _PoolContext:
    def __init__(self, conn: _RecordingConnection) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _RecordingPool:
    """最小 psycopg AsyncConnectionPool 形态——只够被测仓储方法走通。"""

    def __init__(self, rows: list | None = None) -> None:
        self.log: list = []
        self._rows = rows

    def connection(self) -> _PoolContext:
        return _PoolContext(_RecordingConnection(self.log, self._rows))


@pytest.mark.asyncio
async def test_insert_rows_upsert_placeholder_count_matches_params() -> None:
    """对抗审查修复①回归（★非门控静态测试）：解析发往真库的 SQL——VALUES
    占位符数必须等于参数数（曾 6≠8，真库必炸）。"""
    from knowledge_mining.mining.kb.db_artifacts import ArtifactDB

    pool = _RecordingPool()
    db = ArtifactDB(pool)
    result = await db.insert_rows_upsert(
        "art_x", 0,
        [{"row_key": "k1", "data": {"a": 1}, "provenance": [], "conflicts": {}}],
        updated_by="u1",
    )
    assert result == {"inserted": 1, "updated": 0}
    sql, params = pool.log[0]
    assert sql.count("%s") == len(params)
    assert len(params) == 8   # 8 列 × 1 行（artifact_id..updated_by)


@pytest.mark.asyncio
async def test_upsert_column_json_fields_passed_as_json_strings() -> None:
    """codex二审批号①回归（静态）：enum_values/alias 走 ::jsonb 占位——参数
    必须是 JSON 字符串（原生 list 被 psycopg 适配成 text[]，'{old}'::jsonb
    非法）；改名路径同事务发迁键 UPDATE（批号②）。"""
    from knowledge_mining.mining.kb.db_artifacts import ArtifactDB

    pool = _RecordingPool()
    db = ArtifactDB(pool)
    await db.upsert_column(
        "art_x", "c1",
        {"column_name": "新列", "alias": ["旧名"], "enum_values": ["a", "b"]},
        rename_from="旧列",
    )
    updates = [p for q, p in pool.log
               if isinstance(p, dict) and "UPDATE kb_table_columns" in q]
    assert updates
    json_values = sorted(
        str(v) for k, v in updates[0].items() if str(k).startswith("f")
    )
    assert json.loads(json_values[0]) == ["a", "b"]
    assert json.loads(json_values[1]) == ["旧名"]
    # 同事务迁键：rev0 data 键旧→新（jsonb_build_object 形态，只迁 rev0）；
    # codex三审 P1-2a：三处参数显式 ::text（psycopg3 unknown 类型在
    # ``-``/``->`` text-int 重载与 build_object key 推断下必炸/歧义）
    renames = [q for q, _ in pool.log if "jsonb_build_object" in q]
    assert renames
    assert "revision = 0" in renames[0]
    assert "%(old)s::text" in renames[0] and "%(new)s::text" in renames[0]
    assert "data ? %(old)s::text" in renames[0]


@pytest.mark.asyncio
async def test_expression_index_ddl_has_artifact_where_clause() -> None:
    """codex二审批号⑤ + 三审 P1-3 回归（静态）：CREATE INDEX 部分索引限定
    WHERE artifact_id = <字面量> AND revision = <发布版>——无界表达式索引对
    kb_table_rows 全表求 cast，其他制品同名列脏值会当场炸掉索引创建（连带
    炸发布）；限定 revision 后历史版本行（可能含旧类型文本）与 rev0 草稿
    写入都不参与 cast 求值。"""
    from knowledge_mining.mining.kb.db_artifacts import ArtifactDB

    pool = _RecordingPool()
    db = ArtifactDB(pool)
    created = await db.create_expression_indexes("art_x", 7, [
        {"column_id": "c1", "column_name": "端口", "column_type": "int"},
        {"column_id": "c2", "column_name": "名称", "column_type": "text"},
    ])
    assert created == ["idx_kbtr_art_x_c1", "idx_kbtr_art_x_c2"]
    int_ddl, text_ddl = pool.log[0][0], pool.log[1][0]
    assert "WHERE artifact_id = 'art_x' AND revision = 7" in int_ddl
    assert "(revision," in int_ddl and "::numeric" in int_ddl
    assert "WHERE artifact_id = 'art_x' AND revision = 7" in text_ddl


@pytest.mark.asyncio
async def test_drop_index_cleanup_pattern_keeps_trailing_wildcard() -> None:
    """codex二审批号⑤回归（静态）：清理 LIKE pattern 以未转义 % 结尾——旧
    写法连通配符一起转义成字面 %，清理恒空匹配=旧索引永不回收。"""
    from knowledge_mining.mining.kb.db_artifacts import ArtifactDB

    pool = _RecordingPool(rows=[])
    db = ArtifactDB(pool)
    await db.drop_draft_expression_indexes("art_un_der")
    select_sql, params = pool.log[0]
    assert "pg_indexes" in select_sql
    pattern = params[0]
    assert pattern == "idx\\_kbtr\\_art\\_un\\_der\\_%"   # 前缀字面 + 通配尾
    assert pattern.endswith("%") and not pattern.endswith("\\%")


# ------------------------------------------- codex 三审七项回归

@pytest.mark.asyncio
async def test_publish_normalizes_stale_alias_data_keys() -> None:
    """codex三审 P1-2b 回归：发布→改名→discard→编辑另一列→再发布 → 新版本
    数据键=当前列名（发布=键与 schema 对齐的定格点）；draft 期 list_rows 经
    alias 投影显示新名列有值（读取投影兜底，不改存储）。

    断裂场景：discard 只清 rev0 行不清 schema——改名保留、行键回到旧版快照
    （旧键名）。编辑后行可能同时含新旧键（update_cell 按新规范名写）。
    """
    db = await _seeded_db()
    svc = ArtifactService(db)
    owner = _user("u-owner")
    # 改名 L1→一级分类（自动检出 draft、rev0 迁键），随后 discard——schema
    # 仍改名，rev0 清空回到干净态
    col = await svc.upsert_column(
        user=owner, artifact_id=AID, column_id="c002",
        fields={"column_name": "一级分类"},
    )
    assert col["alias"] == ["L1"]
    await svc.discard_draft(user=owner, artifact_id=AID)

    # draft 期（checkout 旧行=旧键 L1）：list_rows 投影到新名列有值
    rows = (await svc.list_rows(user=owner, artifact_id=AID, draft=True))["rows"]
    assert rows and all(r["data"].get("一级分类") for r in rows)
    assert all("L1" not in r["data"] for r in rows)

    # 编辑该列（规范名写入；存储侧旧行为会残留旧键）→ 再发布 → rev2 归一
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="一级分类", value="演练")
    result = await svc.publish(user=owner, artifact_id=AID, change_summary="归一")
    assert result["revision"] == 2
    rev2 = db.revision_rows[AID][2]
    assert all("一级分类" in r["data"] for r in rev2)
    assert all("L1" not in r["data"] for r in rev2)          # 旧键不残留
    edited = next(r for r in rev2 if r["row_key"] == "MOP-1")
    assert edited["data"]["一级分类"] == "演练"              # 新键值不被旧键倒灌


@pytest.mark.asyncio
async def test_stale_token_rejected_on_publish_and_schema_ops() -> None:
    """codex三审 P1-4 回归（service 层）：并发编辑推进令牌后，旧 token 的
    发布/改列/改主键/回滚/弃稿/停用全部被拒（StaleArtifact→409）；当前
    token 发布放行（透传 db 层全精度比对）。"""
    db = await _seeded_db()
    await db.ensure_draft(AID)
    svc = ArtifactService(db)
    owner = _user("u-owner")
    token_before = db.artifacts[AID]["updated_at"].isoformat()
    await svc.update_cell(
        user=owner, artifact_id=AID, row_key="MOP-1", column="L1", value="他人编辑")
    assert db.artifacts[AID]["updated_at"].isoformat() != token_before
    with pytest.raises(StaleArtifact):
        await svc.publish(
            user=owner, artifact_id=AID, change_summary="x",
            expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.upsert_column(
            user=owner, artifact_id=AID, column_id="c002",
            fields={"is_indexed": True}, expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.set_primary_key(
            user=owner, artifact_id=AID, column_ids=["c001"],
            expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.rollback_to(
            user=owner, artifact_id=AID, revision=1,
            expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.discard_draft(
            user=owner, artifact_id=AID, expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.deprecate(
            user=owner, artifact_id=AID, expected_updated_at=token_before)
    with pytest.raises(StaleArtifact):
        await svc.republish(
            user=owner, artifact_id=AID, expected_updated_at=token_before)
    # 当前 token：发布放行（db 层全精度比对通过）
    fresh = db.artifacts[AID]["updated_at"].isoformat()
    result = await svc.publish(
        user=owner, artifact_id=AID, change_summary="r2",
        expected_updated_at=fresh)
    assert result["revision"] == 2


@pytest.mark.asyncio
async def test_mutation_ops_check_expected_updated_at_atomically() -> None:
    """codex三审 P1-4 回归（静态，db 层）：变更类操作全部在事务内 FOR UPDATE
    比对 updated_at——失配 stale_artifact（service 前置快检与写之间的竞态
    由这里封死）。"""
    from knowledge_mining.mining.kb.db_artifacts import (
        ArtifactDB,
        ArtifactError as RepoError,
    )

    art_row = {"artifact_id": "art_x", "status": "draft", "usage_note": "u",
               "draft_open": True, "current_revision": 0, "updated_at": NOW}
    stale = datetime(2026, 10, 15, 10, 0, 0, 500000, tzinfo=timezone.utc)

    async def expect_stale(call) -> None:
        pool = _RecordingPool(rows=[dict(art_row)])
        db = ArtifactDB(pool)
        with pytest.raises(RepoError, match="stale_artifact"):
            await call(db)

    await expect_stale(lambda db: db.publish(
        "art_x", "s", "u", expected_updated_at=stale))
    await expect_stale(lambda db: db.rollback_to(
        "art_x", 1, expected_updated_at=stale))
    await expect_stale(lambda db: db.deprecate(
        "art_x", expected_updated_at=stale))
    await expect_stale(lambda db: db.republish(
        "art_x", expected_updated_at=stale))
    await expect_stale(lambda db: db.discard_draft(
        "art_x", expected_updated_at=stale))
    await expect_stale(lambda db: db.upsert_column(
        "art_x", "c1", {"column_name": "n"}, expected_updated_at=stale))
    await expect_stale(lambda db: db.replace_columns_for_artifact(
        "art_x", [{"column_id": "c1", "column_name": "n",
                   "column_type": "text"}],
        expected_updated_at=stale))
    await expect_stale(lambda db: db.set_primary_key(
        "art_x", ["c1"], expected_updated_at=stale))
    await expect_stale(lambda db: db.insert_rows_upsert(
        "art_x", 0, [{"row_key": "k", "data": {}, "provenance": [],
                      "conflicts": {}, "ordinal": 1}],
        expected_updated_at=stale))


@pytest.mark.asyncio
async def test_update_cell_repo_stale_translated_not_500() -> None:
    """codex三审 P2-1 回归（service 层）：service 前置快检通过（token 当前）
    但仓储层抛 stale（并发窗口）→ 翻译成 StaleArtifact（409），不再以
    ArtifactRepoError 裸抛（路由 500）。update_cell 与 conflict 分支双查。"""
    db = await _seeded_db()
    await db.ensure_draft(AID)
    await db.upsert_column(AID, "c003", {"column_name": "数量", "column_type": "int"})
    svc = ArtifactService(db)
    owner = _user("u-owner")
    fresh = db.artifacts[AID]["updated_at"].isoformat()

    async def raise_stale(*args, **kwargs):
        raise ArtifactRepoError("stale_artifact")

    db.update_cell = raise_stale
    with pytest.raises(StaleArtifact):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="L1",
            value="x", expected_updated_at=fresh)
    db.update_cell_conflict = raise_stale
    with pytest.raises(StaleArtifact):
        await svc.update_cell(
            user=owner, artifact_id=AID, row_key="MOP-1", column="数量",
            value="abc", expected_updated_at=fresh)   # int cast 失败→conflict 分支


@pytest.mark.asyncio
async def test_list_artifacts_excludes_unbound_domain_members() -> None:
    """codex三审 P2-2 回归：成员行在但当前域绑定已撤 → 列表不含该制品
    （详情已是 404 反探测——列表口径对齐）；域绑定恢复后回归列表。"""
    db = await _seeded_db()
    svc = ArtifactService(db)
    viewer = _user("u-viewer")
    assert [a["artifact_id"] for a in await svc.list_artifacts(user=viewer)] \
        == [AID]
    db.domain_bindings.discard(("u-viewer", "plant-a"))
    assert await svc.list_artifacts(user=viewer) == []
    with pytest.raises(ArtifactNotFound):   # 详情口径不变：404 反探测
        await svc.get_artifact(user=viewer, artifact_id=AID)
    # admin 全量不过滤
    assert AID in {
        a["artifact_id"]
        for a in await svc.list_artifacts(user=_user("u-siteadmin", site_role="admin"))
    }
