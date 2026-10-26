from knowledge_mining.mining.workflow.artifact_tables import readable_table
from knowledge_mining.mining.workflow.artifact_table_ops import nested_records, transform


def test_requested_shape_preserves_code_and_excludes_header_cells():
    values = [("属性", "描述"), ("描述", "3000W交流电源模块(黑色)"),
              ("部件编码", "02131712"), ("型号", "PAC3KS54-NE")]
    cells = [{"row_index": r, "column_index": c, "value": value, "is_header": r == 0}
             for r, row in enumerate(values) for c, value in enumerate(row)]
    result = readable_table(["**属性**", "**描述**"], list(reversed(cells)))
    assert result == {"表头": ["属性", "描述"], "数据": [
        {"属性": a, "描述": b} for a, b in values[1:]]}
    assert nested_records(result) == result["数据"]
    snapshot = {"columns": [{"key": "table_content", "name": "内容", "type": "json"}],
                "rows": [{"row_id": "a", "table_content": result}], "issues": []}
    expanded = transform(snapshot, "expand", {})
    assert len(expanded["rows"]) == 3
    assert expanded["rows"][1]["数据.描述"] == "02131712"


def test_duplicate_headers_and_missing_cells_do_not_lose_values():
    result = readable_table(["**属性**", "属性", "属性_2", ""], [
        {"row_index": 1, "column_index": 0, "value": "a"},
        {"row_index": 1, "column_index": 1, "value": "b"},
        {"row_index": 1, "column_index": 2, "value": "c"},
    ])
    assert len(set(result["表头"])) == 4
    assert list(result["数据"][0].values()) == ["a", "b", "c", None]


def test_empty_table_keeps_headers():
    assert readable_table([{"name": "**属性**"}], []) == {"表头": ["属性"], "数据": []}
