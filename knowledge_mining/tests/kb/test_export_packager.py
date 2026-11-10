"""ExportPackager 单测 — STORED/ZIP64/arcname 净化/中文 UTF-8/manifest 末尾/sha256.

纯逻辑零 DB：直接构造 packer，流式喂字节，读回 zip 断言结构。
"""
from __future__ import annotations

import hashlib
import json
import struct
import zipfile

import pytest

from knowledge_mining.mining.kb.services.export_packager import (
    ExportPackager, sanitize_segment,
)


async def _chunks(data: bytes, n: int = 3):
    step = max(1, len(data) // n)
    for i in range(0, len(data), step):
        yield data[i:i + step]


async def _empty():
    return
    yield  # pragma: no cover - 使其成为 async generator


# ------------------------------------------------------------ sanitize_segment

def test_sanitize_strips_illegal_and_control_chars():
    assert sanitize_segment('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert sanitize_segment("dir\nname\x00") == "dir_name_"
    assert sanitize_segment("  spaced  ") == "spaced"


def test_sanitize_windows_reserved_names_prefixed():
    assert sanitize_segment("CON") == "_CON"
    assert sanitize_segment("aux.txt") == "_aux.txt"   # 有扩展名的保留词同样防
    assert sanitize_segment("com1") == "_com1"
    assert sanitize_segment("console") == "console"    # 仅精确词命中


def test_sanitize_trailing_dots_and_spaces():
    # Windows 会剥掉尾部点/空格 → 条目名失配，必须先剥
    assert sanitize_segment("report.. ") == "report"
    assert sanitize_segment("...") == "_"


def test_sanitize_empty_falls_back_to_placeholder():
    assert sanitize_segment("") == "_"
    assert sanitize_segment(None) == "_"


# ------------------------------------------------------------- reserve_arcname

def test_reserve_arcname_joins_directory_segments():
    p = ExportPackager.__new__(ExportPackager)
    p._names = set()
    assert p.reserve_arcname("5G/核心报告", "a.md") == "5G/核心报告/a.md"
    # 空段 / . / .. 全部跳过（防相对路径穿越）
    assert p.reserve_arcname("/./x/../y/", "b.md") == "x/y/b.md"
    assert p.reserve_arcname(None, "c.md") == "c.md"


def test_reserve_arcname_dedupes_in_filename_segment_only():
    p = ExportPackager.__new__(ExportPackager)
    p._names = set()
    first = p.reserve_arcname("docs", "a.pdf")
    second = p.reserve_arcname("docs", "a.pdf")
    third = p.reserve_arcname("docs", "a.pdf")
    assert (first, second, third) == (
        "docs/a.pdf", "docs/a (2).pdf", "docs/a (3).pdf",
    )
    # 不同目录同名不冲突（目录段不动）
    assert p.reserve_arcname("other", "a.pdf") == "other/a.pdf"
    # 无扩展名的编号走尾部
    assert p.reserve_arcname("docs", "README") == "docs/README"
    assert p.reserve_arcname("docs", "README") == "docs/README (2)"


# ------------------------------------------------------------------ 打包主体

@pytest.mark.asyncio
async def test_packager_stored_zip_and_sha_roundtrip(tmp_path):
    out = tmp_path / "x.zip"
    doc_a = b"hello export" * 100
    doc_b = bytes(range(256)) * 40
    legacy = b"legacy local bytes"

    p = ExportPackager(out)
    p.open()
    arc_a = p.reserve_arcname("5G", "a.md")
    n_a = await p.add_stream(arc_a, _chunks(doc_a))
    arc_b = p.reserve_arcname(None, "b.bin")
    n_b = await p.add_stream(arc_b, _chunks(doc_b))
    arc_c = p.reserve_arcname("legacy", "c.dat")
    local = tmp_path / "c.dat"
    local.write_bytes(legacy)
    n_c = p.add_file(arc_c, local)
    manifest = {"format": "coremasterkb-kb-export", "documents": [arc_a, arc_b, arc_c]}
    p.write_manifest(manifest)
    p.close()

    assert (n_a, n_b, n_c) == (len(doc_a), len(doc_b), len(legacy))
    # sha/size 覆盖全部条目字节（文档 + manifest payload）
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    expect_sha = hashlib.sha256(doc_a + doc_b + legacy + payload).hexdigest()
    assert p.sha256 == expect_sha
    assert p.size == len(doc_a) + len(doc_b) + len(legacy) + len(payload)

    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == [
            "5G/a.md", "b.bin", "legacy/c.dat", "manifest.json",
        ]
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read("5G/a.md") == doc_a
        assert zf.read("b.bin") == doc_b
        assert zf.read("legacy/c.dat") == legacy
        assert json.loads(zf.read("manifest.json")) == manifest


@pytest.mark.asyncio
async def test_packager_chinese_arcname_utf8_flag(tmp_path):
    """中文条目名必须带 UTF-8 flag（0x800），否则 zipfile 用 cp437 编码名."""
    out = tmp_path / "cn.zip"
    p = ExportPackager(out)
    p.open()
    arc = p.reserve_arcname("核心网/规格书", "中文 报告#1.pdf")
    await p.add_stream(arc, _chunks(b"chinese content"))
    p.write_manifest({})
    p.close()

    assert arc == "核心网/规格书/中文 报告#1.pdf"
    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo(arc)
        assert info.flag_bits & 0x800, "zip entry missing UTF-8 filename flag"
        assert zf.read(arc) == b"chinese content"


@pytest.mark.asyncio
async def test_packager_zip64_local_header_forced(tmp_path):
    """force_zip64：条目本地头是 zip64（v1/v2 字段 0xFFFFFFFF），数万条目/单 4GB 可扩."""
    out = tmp_path / "z64.zip"
    p = ExportPackager(out)
    p.open()
    arc = p.reserve_arcname(None, "big.bin")
    await p.add_stream(arc, _chunks(b"x" * 1024))
    p.write_manifest({})
    p.close()

    raw = out.read_bytes()
    # local file header: signature PK\x03\x04, compressed size @ offset 18,
    # uncompressed @ 22 —— force_zip64 时两字段均为占位 0xFFFFFFFF。
    assert raw[0:4] == b"PK\x03\x04"
    csize, usize = struct.unpack_from("<II", raw, 18)
    assert csize == 0xFFFFFFFF and usize == 0xFFFFFFFF


@pytest.mark.asyncio
async def test_packager_zip64_extends_for_many_entries(tmp_path):
    """超 65535 条目上限时 allowZip64 让 central directory 走 zip64 计数."""
    out = tmp_path / "many.zip"
    p = ExportPackager(out)
    p.open()
    n = 70_000
    for i in range(n):
        await p.add_stream(p.reserve_arcname("bulk", f"f{i}.txt"), _empty())
    p.close()

    with zipfile.ZipFile(out) as zf:
        assert len(zf.namelist()) == n


@pytest.mark.asyncio
async def test_packager_manifest_written_last(tmp_path):
    """manifest 必须在包末尾（warnings/stats 收尾才齐；读回只看最后一条）."""
    out = tmp_path / "m.zip"
    p = ExportPackager(out)
    p.open()
    seed = tmp_path / ".tiny-seed"
    seed.write_bytes(b"t")
    p.add_file(p.reserve_arcname(None, "a.txt"), seed)
    p.add_file(p.reserve_arcname(None, "b.txt"), seed)
    p.write_manifest({"stats": {"packed_documents": 2}})
    p.close()

    with zipfile.ZipFile(out) as zf:
        infos = zf.infolist()
        assert infos[-1].filename == "manifest.json"
        assert json.loads(zf.read("manifest.json"))["stats"]["packed_documents"] == 2
