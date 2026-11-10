"""Export configuration — 来源：main_control_service。

配置来自 ``GET /api/v1/system/mining/raw`` 的 ``export`` 段
（main_control_service/config/system/mining.yaml）。与 UploadConfig 同构：
无参 → 控制面；显式 kwargs → 直接构造（测试）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import computed_field

from .control_plane import get_mining_service_config

_DEFAULT_EXPORT = {
    "enabled": True,
    "bucket_class": "staging",
    "tmp_root": "./uploads/_exports",
    "max_concurrent_tasks": 1,
    "batch_size": 500,
    "retention_hours": 72,
    "presign_ttl_seconds": 86400,
    "progress_interval_docs": 200,
    "progress_interval_seconds": 5.0,
    "sweep_interval_hours": 6,
    "max_export_bytes": 0,
}


def _export_from_control_plane() -> dict[str, Any]:
    data = get_mining_service_config().get("export") or {}
    return {
        "export_enabled": bool(data.get("enabled", _DEFAULT_EXPORT["enabled"])),
        "export_bucket_class": str(data.get("bucket_class", _DEFAULT_EXPORT["bucket_class"])),
        "export_tmp_root": str(data.get("tmp_root", _DEFAULT_EXPORT["tmp_root"])),
        "export_max_concurrent_tasks": int(data.get("max_concurrent_tasks", _DEFAULT_EXPORT["max_concurrent_tasks"])),
        "export_batch_size": int(data.get("batch_size", _DEFAULT_EXPORT["batch_size"])),
        "export_retention_hours": int(data.get("retention_hours", _DEFAULT_EXPORT["retention_hours"])),
        "export_presign_ttl_seconds": int(data.get("presign_ttl_seconds", _DEFAULT_EXPORT["presign_ttl_seconds"])),
        "export_progress_interval_docs": int(data.get("progress_interval_docs", _DEFAULT_EXPORT["progress_interval_docs"])),
        "export_progress_interval_seconds": float(data.get("progress_interval_seconds", _DEFAULT_EXPORT["progress_interval_seconds"])),
        "export_sweep_interval_hours": int(data.get("sweep_interval_hours", _DEFAULT_EXPORT["sweep_interval_hours"])),
        "export_max_export_bytes": int(data.get("max_export_bytes", _DEFAULT_EXPORT["max_export_bytes"])),
    }


class ExportConfig:
    """整库导出的限额与产物配置.

    无参 → 控制面 mining.yaml 的 export 段；显式 kwargs → 直接构造（测试）。
    """

    def __init__(self, **fields: Any) -> None:
        if not fields:
            fields = _export_from_control_plane()
        else:
            merged = dict(_DEFAULT_EXPORT)
            # 测试可只传 export_ 前缀字段的子集，其余取默认。
            for key, default in _DEFAULT_EXPORT.items():
                attr = f"export_{key}"
                if attr in fields:
                    merged[key] = fields[attr]
            fields = {
                "export_enabled": bool(merged["enabled"]),
                "export_bucket_class": str(merged["bucket_class"]),
                "export_tmp_root": str(merged["tmp_root"]),
                "export_max_concurrent_tasks": int(merged["max_concurrent_tasks"]),
                "export_batch_size": int(merged["batch_size"]),
                "export_retention_hours": int(merged["retention_hours"]),
                "export_presign_ttl_seconds": int(merged["presign_ttl_seconds"]),
                "export_progress_interval_docs": int(merged["progress_interval_docs"]),
                "export_progress_interval_seconds": float(merged["progress_interval_seconds"]),
                "export_sweep_interval_hours": int(merged["sweep_interval_hours"]),
                "export_max_export_bytes": int(merged["max_export_bytes"]),
            }
        self.__dict__.update(fields)

    @computed_field
    @property
    def tmp_root_path(self) -> Path:
        return Path(self.export_tmp_root).resolve()

    def bucket_for(self, store_config: Any) -> str:
        """产物桶名 = bucket_prefix + bucket_class（SRS §8.1 命名）."""
        return f"{store_config.bucket_prefix}{self.export_bucket_class}"
