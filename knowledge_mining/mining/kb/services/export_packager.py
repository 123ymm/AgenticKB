"""ExportPackager — 整库导出 zip 打包器（纯逻辑，零 DB 依赖，便于单测）.

设计要点：
- ``ZIP_STORED`` 不压缩：源文档（PDF/Office/图片）本身已压缩，几十 GB 走
  DEFLATE 纯烧 CPU 不缩体积；STORED 还让解压侧零开销。
- 强制 ``allowZip64`` + 每条目 ``force_zip64``：数万文档远超 65535 条目上限，
  单文件也可能超 4GB（legacy 大文件）。
- 流式写入（``zf.open(name, 'w')``）：文档字节从对象存储流进来直接落盘，
  内存峰值 = 单块大小，与包体积无关。
- 边写边算整包 sha256：上传后与 put_stream 服务端复核值比对。
- arcname 用 ``{directory_path}/{document_name}`` 而非 document_key——后者
  含 ``doc:/`` 前缀（冒号是 Windows 路径非法字符）。
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

_READ_CHUNK = 1024 * 1024  # 1MB

# Windows 文件名非法字符 + 控制字符（zip 条目名要跨平台可解压）。
_ILLEGAL_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名（不区分大小写，不能单独成文件名）。
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_segment(segment: str) -> str:
    """净化单个路径段：去非法字符/控制字符、防 Windows 保留名、去尾点空格."""
    name = _ILLEGAL_FN.sub("_", (segment or "").strip())
    # 尾部点/空格在 Windows 上会被剥掉导致条目名失配，统一去掉。
    name = name.rstrip(". ")
    stem = name.split(".")[0].upper() if name else ""
    if stem in _WIN_RESERVED:
        name = f"_{name}"
    return name or "_"


class ExportPackager:
    """流式 zip 写入器：STORED + ZIP64 + 边写边算 sha256."""

    def __init__(self, out_path: Path):
        self._out_path = Path(out_path)
        self._zf: zipfile.ZipFile | None = None
        self._sha = hashlib.sha256()
        self._size = 0
        self._names: set[str] = set()

    # -- 生命周期 ----------------------------------------------------------

    def open(self) -> None:
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._zf = zipfile.ZipFile(
            self._out_path, mode="w", compression=zipfile.ZIP_STORED,
            allowZip64=True,
        )

    def close(self) -> None:
        if self._zf is not None:
            self._zf.close()
            self._zf = None

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    @property
    def size(self) -> int:
        return self._size

    # -- 条目写入 ----------------------------------------------------------

    def reserve_arcname(self, directory_path: str | None,
                        document_name: str) -> str:
        """预留唯一 zip 条目名：``{净化目录}/{净化文件名}``，冲突在文件名段追加 " (2)"."""
        parts = [
            sanitize_segment(seg)
            for seg in (directory_path or "").split("/")
            if seg and seg not in (".", "..")
        ]
        base = "/".join([*parts, sanitize_segment(document_name)])
        arcname, n = base, 2
        while arcname in self._names:
            arcname = self._numbered(base, n)
            n += 1
        self._names.add(arcname)
        return arcname

    @staticmethod
    def _numbered(base: str, n: int) -> str:
        """在**文件名段**（不动目录）的扩展名前插序号：``a/b.pdf`` → ``a/b (2).pdf``."""
        dir_part, _, fname = base.rpartition("/")
        stem, dot, ext = fname.rpartition(".")
        if dot and stem:
            fname = f"{stem} ({n}).{ext}"
        else:
            fname = f"{fname} ({n})"
        return f"{dir_part}/{fname}" if dir_part else fname

    async def add_stream(self, arcname: str,
                         stream: AsyncIterator[bytes]) -> int:
        """流式写入一条 zip 条目，返回字节数（同步缓冲写亚毫秒级，不值得 to_thread）."""
        assert self._zf is not None, "packager not open"
        written = 0
        with self._zf.open(arcname, mode="w", force_zip64=True) as entry:
            async for chunk in stream:
                if not chunk:
                    continue
                entry.write(chunk)
                self._sha.update(chunk)
                self._size += len(chunk)
                written += len(chunk)
        return written

    def add_file(self, arcname: str, path: Path) -> int:
        """本地文件写入一条 zip 条目（legacy 文档回落），返回字节数."""
        assert self._zf is not None, "packager not open"
        written = 0
        with self._zf.open(arcname, mode="w", force_zip64=True) as entry:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_READ_CHUNK)
                    if not chunk:
                        break
                    entry.write(chunk)
                    self._sha.update(chunk)
                    self._size += len(chunk)
                    written += len(chunk)
        return written

    def write_manifest(self, manifest: dict) -> None:
        """manifest.json 写在包末尾（warnings/stats 只有收尾时才齐）."""
        assert self._zf is not None, "packager not open"
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        # writestr 不走流式管道（manifest 是元数据，量级 KB~MB，可整体物化）。
        self._zf.writestr(
            zipfile.ZipInfo("manifest.json"), payload,
            compress_type=zipfile.ZIP_STORED,
        )
        self._sha.update(payload)
        self._size += len(payload)


__all__ = ["ExportPackager", "sanitize_segment"]
