"""WikiDB 模块级纯函数测试 —— 无 PG，本地红绿（63号批次2 Task 1）。

覆盖 db_wiki.py 顶部纯函数：normalize_page_key / encode-decode_page_key /
extract_wiki_links / compute_page_edges / chunk_page_for_units（63号 §2.4
链接解析归一、§5.3 检索单元分块口径）。
"""
from __future__ import annotations

import re

import pytest

from knowledge_mining.mining.kb.db_wiki import (
    WikiError,
    chunk_page_for_units,
    compute_page_edges,
    decode_page_key,
    encode_page_key,
    extract_wiki_links,
    normalize_page_key,
)


# ------------------------------------------------------- normalize_page_key

def test_normalize_chinese_title_kept() -> None:
    # 中文是 page_key 存储形态（仅传输层 hex 编码）；空白折叠成连字符
    assert normalize_page_key("网络 集成方案") == "网络-集成方案"
    assert normalize_page_key("已-存在") == "已-存在"


def test_normalize_whitespace_collapse_and_strip() -> None:
    assert normalize_page_key("  Hello   World  ") == "Hello-World"


def test_normalize_strips_illegal_chars() -> None:
    assert normalize_page_key("A?B*C!") == "ABC"


def test_normalize_nfkc_folds_fullwidth() -> None:
    # NFKC 先行：全角字母折半角、带圈数字折普通数字
    assert normalize_page_key("ＡＢ") == "AB"
    assert normalize_page_key("①号") == "1号"


def test_normalize_empty_raises() -> None:
    with pytest.raises(WikiError) as ei:
        normalize_page_key("???")
    assert ei.value.code == "invalid_page_key"
    with pytest.raises(WikiError):
        normalize_page_key("   ")


# ---------------------------------------------------- encode/decode_page_key

def test_encode_decode_roundtrip_chinese() -> None:
    key = "中文-页/键"
    encoded = encode_page_key(key)
    # hex 输出恒匹配 [0-9a-f]——encodeURIComponent 不转义的 URL 安全 ASCII
    assert re.fullmatch(r"[0-9a-f]+", encoded)
    assert decode_page_key(encoded) == key


def test_encode_known_value() -> None:
    assert encode_page_key("中") == "e4b8ad"  # UTF-8 E4 B8 AD


def test_decode_invalid_hex_raises() -> None:
    for bad in ("zz", "abc", "ff", ""):  # 非hex / 奇数位 / 非法UTF-8字节 / 空
        with pytest.raises(WikiError) as ei:
            decode_page_key(bad)
        assert ei.value.code == "invalid_page_key"


# ------------------------------------------------------- extract_wiki_links

def test_extract_plain_and_anchor() -> None:
    assert extract_wiki_links("见 [[目标页]] 与 [[A|锚文本]]") == [
        ("目标页", ""), ("A", "锚文本"),
    ]


def test_extract_dedup_keeps_order() -> None:
    assert extract_wiki_links("[[A]] 重复 [[A]] [[A]]") == [("A", "")]


def test_extract_same_target_distinct_anchors_kept() -> None:
    # 边主键含 anchor_text——同目标不同锚文本是不同边
    assert extract_wiki_links("[[A|x]] 与 [[A|y]]") == [("A", "x"), ("A", "y")]


def test_extract_skips_src_references() -> None:
    # [[src:...]] 是引用标记（角标），不是页面链接
    assert extract_wiki_links("[[src:unit-123]] 引用 [[B]]") == [("B", "")]


def test_extract_no_links() -> None:
    assert extract_wiki_links("无链接正文") == []
    assert extract_wiki_links("") == []


# ------------------------------------------------------- compute_page_edges

def test_compute_edges_valid_broken_and_alias_normalization() -> None:
    pages = [
        {"page_key": "A", "frontmatter": {}, "body_md": "见 [[B]]、[[C别名|看这里]]、[[不存在]]"},
        {"page_key": "B", "frontmatter": {}, "body_md": ""},
        {"page_key": "C", "frontmatter": {"aliases": ["C别名"]}, "body_md": "回 [[A]]"},
    ]
    edges = compute_page_edges(pages)
    assert {"from_page": "A", "to_page": "B", "anchor_text": "",
            "status": "valid"} in edges
    # alias 命中 → to_page 归一到 canonical page_key（反向链接/跳转正确性前提），
    # anchor_text 保留书写原文
    assert {"from_page": "A", "to_page": "C", "anchor_text": "看这里",
            "status": "valid"} in edges
    assert {"from_page": "A", "to_page": "不存在", "anchor_text": "",
            "status": "broken"} in edges
    assert {"from_page": "C", "to_page": "A", "anchor_text": "",
            "status": "valid"} in edges
    assert len(edges) == 4


def test_compute_edges_canonical_key_beats_alias() -> None:
    # 某页 alias 撞别页 canonical page_key：canonical 优先
    pages = [
        {"page_key": "X", "frontmatter": {"aliases": ["B"]}, "body_md": "[[B]]"},
        {"page_key": "B", "frontmatter": {}, "body_md": ""},
    ]
    assert compute_page_edges(pages)[0]["to_page"] == "B"


# ------------------------------------------------------ chunk_page_for_units

def test_chunk_short_page_single_unit() -> None:
    chunks = chunk_page_for_units("短标题", "正文", "短页")
    assert chunks == [("短页", "# 短标题\n\n正文")]


def test_chunk_boundary_exactly_limit_single_unit() -> None:
    body = "戊" * 2000
    chunks = chunk_page_for_units("测试页", body, "测试页")
    assert [k for k, _ in chunks] == ["测试页"]
    assert chunks[0][1] == "# 测试页\n\n" + body


def test_chunk_long_page_sections_by_h2() -> None:
    body = "导语。" + "\n\n## 概述\n\n" + "甲" * 1500 \
        + "\n\n## 细节\n\n" + "乙" * 1500
    chunks = chunk_page_for_units("测试页", body, "测试页")
    assert [k for k, _ in chunks] == ["测试页#s1", "测试页#s2", "测试页#s3"]
    # 每块前置页标题与节标题行（块文本携带页标题上下文）
    assert chunks[0][1].startswith("# 测试页\n\n导语。")
    assert chunks[1][1].startswith("# 测试页\n\n## 概述\n\n")
    assert "甲" * 1500 in chunks[1][1]
    assert chunks[2][1].startswith("# 测试页\n\n## 细节\n\n")
    assert chunks[2][1].endswith("乙" * 1500)


def test_chunk_oversized_section_paragraph_hard_split() -> None:
    para = "丙" * 900
    body = "## 大节\n\n" + para + "\n\n" + para + "\n\n" + para  # 节体 2705 字
    chunks = chunk_page_for_units("测试页", body, "测试页")
    assert [k for k, _ in chunks] == ["测试页#s1", "测试页#s2"]
    prefix = "# 测试页\n\n## 大节\n\n"
    for _, content in chunks:
        assert content.startswith(prefix)
        assert len(content) - len(prefix) <= 2000  # 正文块 ≤ 上限


def test_chunk_single_oversized_paragraph_sliced() -> None:
    body = "## 节\n\n" + "丁" * 4500
    chunks = chunk_page_for_units("测试页", body, "测试页")
    assert [k for k, _ in chunks] == ["测试页#s1", "测试页#s2", "测试页#s3"]
    prefix = "# 测试页\n\n## 节\n\n"
    sizes = [len(content) - len(prefix) for _, content in chunks]
    assert sizes == [2000, 2000, 500]
