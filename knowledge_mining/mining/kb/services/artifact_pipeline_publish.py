"""Publish an online snapshot through existing artifact schema/row/revision services."""
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb

from knowledge_mining.mining.kb.db_artifacts import ArtifactDB
from knowledge_mining.mining.kb.services.artifact_service import ArtifactService, InvalidOperation, _is_formula
from knowledge_mining.mining.kb.services.artifact_import_service import ArtifactImportService, _row_key
from knowledge_mining.mining.workflow.artifact_table_ops import materialize, cast
from knowledge_mining.mining.workflow.manifest import value_hash


class ConnectionPool:
    """Keep nested existing repository transactions inside one publication transaction."""
    def __init__(self, conn): self.conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self.conn


def prepare(snapshot, plan, active, run_id):
    columns = plan.get("columns", [])
    keys = {c["key"] for c in snapshot["columns"]}
    if not columns or any(c.get("source") not in keys for c in columns):
        raise InvalidOperation("请选择要发布的字段")
    mode = "append" if active else "create"
    mapped, specs, additions = ArtifactImportService._build_column_specs(columns, active, mode)
    if additions: raise InvalidOperation("合并已有资产时，请映射到现有列；需要新字段请先在字段算子中整理")
    names = [target for _, target, _ in mapped]
    if not names or len(names) != len(set(names)): raise InvalidOperation("发布字段不可重名")
    if active and set(names) != {c["column_name"] for c in active}:
        raise InvalidOperation("合并需要映射目标资产的全部字段，避免覆盖时丢失数据")
    pk = ArtifactImportService._primary_key_names(columns, active, mode)
    schema = active or specs
    enums = {c["column_name"]: c.get("enum_values") or [] for c in schema if c["column_type"] == "enum"}
    rows, seen = [], set()
    for i, row in enumerate(materialize(snapshot)["rows"]):
        data = {}
        for source, target, kind in mapped:
            value = row.get(source)
            if isinstance(value, (dict, list)) and not plan.get("json_as_text"):
                raise InvalidOperation("JSON 字段请先展开，或确认以 JSON 文本发布")
            try: data[target] = cast(value, kind)
            except (ValueError, TypeError): raise InvalidOperation(f"第 {i + 1} 行字段 {target} 类型不匹配") from None
            if _is_formula(data[target]): raise InvalidOperation(f"第 {i + 1} 行字段 {target} 含公式，请先调整")
            if target in enums and data[target] is not None and str(data[target]) not in enums[target]:
                raise InvalidOperation(f"第 {i + 1} 行字段 {target} 不在目标枚举范围")
        if any(data.get(k) is None or data[k] == "" for k in pk): raise InvalidOperation(f"第 {i + 1} 行主键为空")
        key, _ = _row_key(data, pk)
        if not pk: key = value_hash([run_id, row["row_id"], names])
        if key in seen: raise InvalidOperation(f"第 {i + 1} 行主键重复，请先去重")
        seen.add(key)
        rows.append({"row_key": key, "data": data, "ordinal": i + 1,
                     "provenance": [{"channel": "pipeline", "run_id": run_id, "row_id": row["row_id"],
                                     "source": row.get("provenance"), "llm": row.get("_llm"), "llm_history": row.get("_llm_history", [])}], "conflicts": {}})
    if not rows: raise InvalidOperation("没有可发布的数据")
    return rows, specs


async def publish_snapshot(pipeline, user, artifact_id, run_id, plan, confirm=False):
    run = await pipeline.read_run(user, artifact_id, run_id)
    if run["status"] != "completed": raise InvalidOperation("只能发布已完成步骤")
    snapshot = await pipeline.read_json(run["metadata_json"]["output"]["storage_object_id"])
    if snapshot.get("preview"): raise InvalidOperation("试跑样本不可发布，请对完整输入执行")
    if any(r.get("_llm", {}).get("status") == "failed" for r in snapshot["rows"]):
        raise InvalidOperation("存在模型失败行，请重试或筛选移除失败行后发布")
    if not plan.get("usage_note", "").strip(): raise InvalidOperation("请填写使用说明")
    if snapshot.get("issues") and not plan.get("acknowledge_issues"):
        raise InvalidOperation("来源包含未成功抽取的文件，请确认处理情况后发布")
    signature = value_hash({k: v for k, v in plan.items() if k != "preview_token"})
    async with pipeline.repo.pool.connection() as conn:
        async with conn.transaction():
            # Serialize double-clicks, including publication to a newly created artifact.
            cursor = await conn.execute("SELECT metadata_json FROM mining_workflows WHERE id=%s FOR UPDATE", (run["workflow_id"],))
            workflow = await cursor.fetchone()
            metadata = dict(workflow["metadata_json"])
            published = metadata.get("publications", {}).get(signature)
            if published:
                await pipeline.authorize(user, published["artifact_id"])
                return published | {"already_published": True}
            service = ArtifactService(ArtifactDB(ConnectionPool(conn)))
            target_id = plan.get("target_artifact_id")
            target, active = None, []
            if target_id:
                cursor = await conn.execute("SELECT artifact_id FROM kb_artifacts WHERE artifact_id=%s FOR UPDATE", (target_id,))
                await cursor.fetchone()
                target, _ = await service._assert_role(user, target_id, "owner", "editor")
                if target["domain_id"] != run["metadata_json"]["domain"] or target["kind"] != "table":
                    raise InvalidOperation("只能合并到同域表格资产")
                active = await service._db.list_columns(target_id)
                if target.get("draft_open") and (active or target.get("current_revision")):
                    raise InvalidOperation("目标资产已有未发布草稿，请先处理该草稿")
            elif not plan.get("title", "").strip(): raise InvalidOperation("请填写新资产名称")
            rows, specs = prepare(snapshot, plan, active, run_id)
            for row in rows:
                row["provenance"][0]["snapshot"] = run["metadata_json"]["output"]
            existing = set()
            if target:
                cursor = await conn.execute("SELECT row_key FROM kb_table_rows WHERE artifact_id=%s AND revision=%s AND row_key=ANY(%s)",
                                            (target_id, target["current_revision"], [r["row_key"] for r in rows]))
                existing = {r["row_key"] for r in await cursor.fetchall()}
            if existing and plan.get("conflict_policy", "block") != "overwrite":
                raise InvalidOperation(f"有 {len(existing)} 行主键与目标重复，请选择覆盖或调整主键")
            token = value_hash([signature, str(target["updated_at"]) if target else None, run["metadata_json"]["output"]["sha256"]])
            preview = {"preview_token": token, "input_rows": len(rows), "inserted": len(rows) - len(existing),
                       "updated": len(existing), "sample": [r["data"] for r in rows[:5]]}
            if not confirm: return preview
            if plan.get("preview_token") != token: raise InvalidOperation("目标或发布配置已变化，请重新预览")
            if not target:
                target = await service.create_artifact(user=user, domain_id=run["metadata_json"]["domain"], title=plan["title"])
                target_id = target["artifact_id"]
            await service.update_meta(user=user, artifact_id=target_id, fields={"usage_note": plan["usage_note"]})
            if specs: await service.replace_columns(user=user, artifact_id=target_id, columns=specs)
            await service.insert_rows(user=user, artifact_id=target_id, rows=rows)
            result = await service.publish(user=user, artifact_id=target_id, change_summary=f"Pipeline {run_id} 定版")
            result["artifact_id"] = target_id
            metadata.setdefault("publications", {})[signature] = result
            await conn.execute("UPDATE mining_workflows SET metadata_json=%s WHERE id=%s", (Jsonb(metadata), run["workflow_id"]))
            return result
