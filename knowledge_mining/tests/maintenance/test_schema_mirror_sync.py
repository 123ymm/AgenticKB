# -*- coding: utf-8 -*-
"""迁移↔启动链镜像 DDL 同文守卫（63号 Task 1.1 质量审查建议）。

启动链快照（databases/kb/schemas/0NN_*.sql）与正式迁移
（databases/migrations/core/*_0NN_*.sql）必须保持结构性 DDL 主体一致：
新建库走启动链、老库走迁移，两者漂移会造成同版本库结构分叉。

归一口径：CRLF→LF、去空白行、去 `--` 注释行；比较自首个 CREATE TABLE
起至文件末尾的 DDL 主体（迁移允许在 CREATE TABLE 之前携带 ALTER 等
前置步骤，如 010 的 batches channel 扩充——那是启动链整体建库时天然
包含的，不属于镜像主体）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# （启动链镜像, 正式迁移）
MIRROR_PAIRS = [
    ("databases/kb/schemas/018_wiki_artifacts.sql",
     "databases/migrations/core/20261021_010_wiki_artifacts.sql"),
    ("databases/kb/schemas/019_wiki_toc.sql",
     "databases/migrations/core/20261103_011_wiki_toc.sql"),
]


def _ddl_body(path: Path) -> list[str]:
    """读 SQL 文件，归一后返回首个 CREATE TABLE 起的行列表。"""
    if not path.is_file():
        pytest.fail(f"镜像文件缺失：{path}")
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = [ln.rstrip() for ln in raw.split("\n")]
    lines = [ln for ln in lines
             if ln.strip() and not ln.strip().startswith("--")]
    for idx, ln in enumerate(lines):
        if "CREATE TABLE" in ln.upper():
            return lines[idx:]
    pytest.fail(f"{path} 中未找到 CREATE TABLE，无法抽取镜像 DDL 主体")


@pytest.mark.parametrize(("startup_path", "migration_path"), MIRROR_PAIRS,
                         ids=[p[0].rsplit("/", 1)[-1] for p in MIRROR_PAIRS])
def test_startup_chain_mirrors_migration_ddl(startup_path: str,
                                             migration_path: str) -> None:
    startup_body = _ddl_body(_REPO_ROOT / startup_path)
    migration_body = _ddl_body(_REPO_ROOT / migration_path)
    assert startup_body == migration_body, (
        f"启动链镜像与迁移 DDL 主体漂移：\n  启动链={startup_path}\n"
        f"  迁移={migration_path}\n改任何一侧后必须同步另一侧"
        f"（迁移侧的 ALTER 前置步骤写在首个 CREATE TABLE 之前即可豁免）。"
    )
