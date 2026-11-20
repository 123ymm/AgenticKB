"""Wiki 原文分页游标。游标只定位，不授权；每页必须重新校验资源权限。"""
from __future__ import annotations

import base64
import binascii
import json

from knowledge_mining.mining.kb.db_wiki import WikiError


def encode_cursor(artifact_id: str, revision: int, unit_id: str,
                  row: dict, offset: int) -> str:
    data = [1, artifact_id, revision, unit_id, str(row["kb_id"]),
            str(row["snapshot_id"]), row["content_hash"], offset]
    return base64.urlsafe_b64encode(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def decode_cursor(cursor: str, artifact_id: str, revision: int,
                  unit_id: str) -> dict:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError
        data = json.loads(base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if (not isinstance(data, list) or len(data) != 8
                or type(data[0]) is not int or data[0] != 1
                or type(data[2]) is not int or data[2] < 1
                or any(not isinstance(data[i], str) or not data[i]
                       for i in (1, 3, 4, 5, 6))
                or type(data[7]) is not int or not 0 < data[7] < 2**31 - 1
                or data[1] != artifact_id or data[3] != unit_id):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise WikiError("invalid_source_cursor") from None
    if data[2] != revision:
        raise WikiError("expired_source_cursor")
    return {"kb_id": data[4], "snapshot_id": data[5],
            "content_hash": data[6], "offset": data[7]}
