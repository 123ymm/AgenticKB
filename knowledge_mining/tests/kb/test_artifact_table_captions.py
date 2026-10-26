import pytest

from knowledge_mining.mining.workflow.artifact_tables import extract_rows


def extract(elements, assets=None, relations=None):
    tables = [{"table_ref": "e-table", "columns_json": [], "sheet_name": "sheet"}]
    ir = {"elements": elements, "structured_assets": assets or {}, "relations": relations or []}
    return extract_rows({"id": "doc", "snapshot_id": "snap"}, tables, [], ir)


@pytest.mark.parametrize("title", [
    "*表1 PAC3KS54-NE基本属性*",
    r"\*表2 PAC3KS54-NE与产品型号的配套关系\*",
])
def test_linked_markdown_caption_keeps_full_numbered_title(title):
    rows, text = extract(
        [{"element_id": "cap", "text": title}],
        {"e-table": {"caption_element_id": "cap"}},
    )
    assert rows[0]["table_name"] == title.replace("\\", "").strip("*")
    assert rows[0]["provenance"]["name_source"] == "caption"
    assert text == title  # Source body is unchanged.


def test_unbound_caption_adjacent_to_its_table():
    rows, _ = extract([
        {"element_id": "cap", "element_type": "paragraph", "order_index": 0,
         "text": "*表1 PAC3KS54-NE基本属性*", "page_span_ids": ["p1"]},
        {"element_id": "e", "element_type": "table", "order_index": 1, "page_span_ids": ["p1"]},
    ])
    assert rows[0]["table_name"] == "表1 PAC3KS54-NE基本属性"
    assert rows[0]["provenance"]["name_source"] == "adjacent_caption"


@pytest.mark.parametrize("text,pages", [
    ("产品基本属性如下", ["p1"]),
    ("表1 展示了参数", ["other-page"]),
    ("表1展示了参数", ["p1"]),
])
def test_does_not_borrow_unrelated_paragraph_or_cross_page_title(text, pages):
    rows, _ = extract([
        {"element_id": "cap", "element_type": "paragraph", "text": text,
         "order_index": 0, "page_span_ids": pages},
        {"element_id": "e", "element_type": "table", "order_index": 1, "page_span_ids": ["p1"]},
    ])
    assert rows[0]["table_name"] == "sheet"


def test_caption_relation_overrides_adjacent_title():
    rows, _ = extract([
        {"element_id": "bound", "text": "**表2 正式标题**", "order_index": 0},
        {"element_id": "other", "element_type": "paragraph", "text": "表1 其他标题", "order_index": 1},
        {"element_id": "e", "element_type": "table", "order_index": 2},
    ], relations=[{"relation_type": "caption_of", "source_element_id": "bound", "target_element_id": "e"}])
    assert rows[0]["table_name"] == "表2 正式标题"
