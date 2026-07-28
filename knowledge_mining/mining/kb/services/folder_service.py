"""KB 文件夹管理 —— 一等文件夹（kb_folders）的磁盘 + DB 协调。

kb_folders 是文件夹结构的唯一真相源；磁盘目录与之镜像（建→mkdir，删空→rmdir）。
权限复用 KbService._assert_write/_assert_read。document_key/id 在所有文件夹操作中
不变（G3 的移动/改名只动位置）。mining 的 rglob 会自然 walk 出层级。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from knowledge_mining.mining.infra.upload_config import UploadConfig
from knowledge_mining.mining.kb.db import KbDB
from knowledge_mining.mining.kb.services.kb_service import Duplicate, KbService, NotFound
from knowledge_mining.mining.kb.storage import (
    build_folder_dir, join_path, normalize_folder_name,
)


class FolderService:
    def __init__(self, db: KbDB, upload_root: Path | None = None) -> None:
        self._db = db
        self._svc = KbService(db)
        self._upload_root = Path(upload_root) if upload_root else UploadConfig().upload_root_path

    async def list_folders(self, *, kb_id: str, user_id: str) -> list[dict[str, Any]]:
        await self._svc._assert_read(kb_id, user_id)
        return await self._db.list_folders(kb_id)

    async def get_folder(self, *, folder_id: str, user_id: str) -> dict[str, Any]:
        folder = await self._db.get_folder(folder_id)
        if folder is None:
            raise NotFound(folder_id)
        await self._svc._assert_read(folder["kb_id"], user_id)
        return folder

    async def create_folder(
        self, *, kb_id: str, parent_id: str | None, name: str, user_id: str,
    ) -> dict[str, Any]:
        kb = await self._db.get_kb(kb_id)
        if kb is None:
            raise NotFound(kb_id)
        await self._svc._assert_write(kb_id, user_id)
        name = normalize_folder_name(name)
        parent_path = ""
        if parent_id is not None:
            parent = await self._db.get_folder(parent_id)
            if parent is None or parent["kb_id"] != kb_id:
                raise NotFound(parent_id)
            parent_path = parent["path"]
        if await self._db.find_folder_by_parent(kb_id=kb_id, parent_id=parent_id, name=name):
            raise Duplicate(f"{kb_id}/{join_path(parent_path, name)}")
        path = join_path(parent_path, name)
        build_folder_dir(self._upload_root, kb_id, path).mkdir(parents=True, exist_ok=True)
        return await self._db.insert_folder(
            folder_id=uuid.uuid4().hex, kb_id=kb_id, parent_id=parent_id, name=name,
            path=path, created_by=user_id,
        )

    async def delete_folder(self, *, folder_id: str, user_id: str) -> None:
        """仅删空文件夹（无子文件夹、无文档）。非空 → ValueError（路由映射 409）。"""
        folder = await self._db.get_folder(folder_id)
        if folder is None:
            raise NotFound(folder_id)
        kb_id = folder["kb_id"]
        await self._svc._assert_write(kb_id, user_id)
        children = await self._db.count_child_folders(kb_id=kb_id, parent_id=folder_id)
        docs = await self._db.count_docs_under_path(kb_id=kb_id, path=folder["path"])
        if children or docs:
            raise ValueError(f"folder not empty: {children} subfolder(s), {docs} doc(s)")
        d = build_folder_dir(self._upload_root, kb_id, folder["path"])
        if d.is_dir():
            d.rmdir()  # 仅空目录可删；非空会 OSError（双保险）
        await self._db.delete_folder_row(folder_id)
