import copy

import pytest

from knowledge_mining.mining.kb.services.artifact_pipeline_publish import prepare
from knowledge_mining.mining.kb.services.artifact_service import InvalidOperation
from knowledge_mining.mining.workflow.artifact_table_ops import transform, selection, nested_records


@pytest.fixture
def table():
    return {"columns": [{"key": "name", "name": "名称", "type": "text"}, {"key": "n", "name": "数量", "type": "int"}],
            "rows": [{"row_id": "a", "name": "A", "n": 0, "provenance": {"file": "x"}},
                     {"row_id": "b", "name": "A", "n": 2}, {"row_id": "c", "name": "B", "n": 3}], "issues": [], "documents": {}}


def test_filter_works_on_all_rows_keeps_order_and_does_not_mutate(table):
    original = copy.deepcopy(table)
    selected = selection(table, {"filters": [{"column": "n", "op": "eq", "value": "0"}], "keep_columns": ["n"]})
    assert selected["rows"] == [{"row_id": "a", "n": 0, "provenance": {"file": "x"}}]
    assert table == original
    assert [c["key"] for c in selection(table, {})["columns"]] == ["name", "n"]
    with pytest.raises(InvalidOperation): selection(table, {"filters": [{"column": "missing", "op": "eq"}]})


def test_fields_strict_cast_dedup_and_manual_edit(table):
    out = transform(table, "fields", {"fields": [{"source": "n", "key": "number", "type": "float"}]})
    assert out["rows"][1]["number"] == 2.0
    assert [r["row_id"] for r in transform(table, "deduplicate", {"keys": ["name"]})["rows"]] == ["a", "c"]
    out = transform(table, "edit", {"changes": [{"row_id": "b", "column": "n", "value": "4"}]})
    assert out["rows"][1]["n"] == 4
    assert table["rows"][1]["n"] == 2
    with pytest.raises(InvalidOperation): transform(table, "fields", {"fields": [{"source": "name", "key": "n", "type": "int"}]})


def test_drop_columns_preserves_rows_provenance_and_history(table):
    original = copy.deepcopy(table)
    out = transform(table, "drop_columns", {"drop_columns": ["name"]})
    assert [c["key"] for c in out["columns"]] == ["n"]
    assert len(out["rows"]) == 3
    assert all("name" not in row for row in out["rows"])
    assert out["rows"][0] == {"row_id": "a", "n": 0, "provenance": {"file": "x"}}
    assert table == original
    for names in ([], ["unknown"], ["row_id"], ["name", "n"]):
        with pytest.raises(InvalidOperation):
            transform(table, "drop_columns", {"drop_columns": names})


def test_classify_merge_expand_preserves_sources_and_field_union():
    snapshot = {"columns": [{"key": "table_name", "name": "表名", "type": "text"}, {"key": "table_content", "name": "内容", "type": "json"}],
                "rows": [{"row_id": "a", "table_name": "命令参数", "table_content": {"rows": [{"命令": "show"}]}, "provenance": {"doc": "a"}},
                         {"row_id": "b", "table_name": "命令-参数", "table_content": {"rows": [{"参数": "x"}]}, "provenance": {"doc": "b"}}], "issues": [], "documents": {}}
    classified = transform(snapshot, "classify", {})
    assert classified["rows"][0]["表格分类"] == classified["rows"][1]["表格分类"]
    merged = transform(classified, "merge", {"group_column": "表格分类"})
    assert len(merged["rows"]) == 1
    assert len(merged["rows"][0]["provenance"]) == 2
    expanded = transform(merged, "expand", {})
    assert len(expanded["rows"]) == 2
    assert [c["key"] for c in expanded["columns"]][-2:] == ["数据.命令", "数据.参数"]


def test_nested_header_and_duplicate_column_titles():
    assert nested_records({"cells": [
        {"row_index": 0, "column_index": 0, "column_name": "参数", "is_header": True, "value": "参数"},
        {"row_index": 1, "column_index": 0, "column_name": "参数", "value": "a"},
        {"row_index": 1, "column_index": 1, "column_name": "参数", "value": "b"},
    ]}) == [{"参数": "a", "参数_2": "b"}]


def test_left_join_no_row_loss_and_rejects_ambiguous_lookup(table):
    lookup = {"columns": [{"key": "k", "name": "k", "type": "text"}, {"key": "label", "name": "label", "type": "text"}], "rows": [{"k": "A", "label": "ok"}]}
    out = transform(table, "join", {"left_key": "name", "right_key": "k"}, lookup)
    assert [r["对照.label"] for r in out["rows"]] == ["ok", "ok", None]
    lookup["rows"].append({"k": "A", "label": "ambiguous"})
    with pytest.raises(InvalidOperation, match="重复"): transform(table, "join", {"left_key": "name", "right_key": "k"}, lookup)


def test_publication_mapping_strict_cast_and_primary_keys(table):
    plan = {"columns": [{"source": "name", "target_name": "名称", "column_type": "text"},
                        {"source": "n", "target_name": "数量", "column_type": "int", "is_primary_key": True}]}
    rows, specs = prepare(table, plan, [], "run")
    assert len(rows) == 3 and specs[1]["is_primary_key"]
    assert rows[0]["data"]["数量"] == 0
    assert rows[0]["provenance"][0]["run_id"] == "run"
    plan["columns"][0]["is_primary_key"] = True
    table["rows"][1]["n"] = 0
    with pytest.raises(InvalidOperation, match="重复"): prepare(table, plan, [], "run")


def test_publication_requires_complete_existing_mapping_and_explicit_json(table):
    active = [{"column_id": "c1", "column_name": "名称", "column_type": "text"}, {"column_id": "c2", "column_name": "数量", "column_type": "int"}]
    plan = {"columns": [{"source": "name", "action": "map_to", "target_column_id": "c1"}]}
    with pytest.raises(InvalidOperation, match="全部字段"): prepare(table, plan, active, "run")
    table["rows"][0]["name"] = {"nested": True}
    plan = {"columns": [{"source": "name", "target_name": "内容", "column_type": "text"}]}
    with pytest.raises(InvalidOperation, match="JSON"): prepare(table, plan, [], "run")
    assert prepare(table, plan | {"json_as_text": True}, [], "run")[0][0]["data"]["内容"] == '{"nested": true}'
