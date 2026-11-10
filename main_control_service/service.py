from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from fastapi import HTTPException


logger = logging.getLogger(__name__)

@dataclass(slots=True)
class YamlConfigService:
    """YAML config reader+writer — config_dir is the single source of truth."""

    config_dir: Path

    # ------------------------------------------------------------------
    # Generic YAML reader / writer
    # ------------------------------------------------------------------

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def _save_yaml(self, path: Path, data: dict[str, Any]) -> None:
        """Atomic write: write to temp file then rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(path))
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save_yaml_text(self, path: Path, text: str) -> None:
        """Atomic write of raw YAML text — validates first."""
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML root must be a mapping")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_yaml_text(self, path: Path) -> str:
        if not path.exists():
            raise HTTPException(status_code=404, detail="config_not_found")
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Domain registry
    # ------------------------------------------------------------------

    def _load_domain_registry(self) -> dict[str, Any]:
        payload = self._load_yaml(self.config_dir / "domain_registry.yaml")
        return payload.get("domains", {})

    def _save_domain_registry(self, domains: dict[str, Any]) -> None:
        self._save_yaml(self.config_dir / "domain_registry.yaml", {"domains": domains})

    def _load_domain_registry_raw(self) -> str:
        return self._read_yaml_text(self.config_dir / "domain_registry.yaml")

    def list_domains(self) -> list[dict[str, Any]]:
        registry = self._load_domain_registry()
        results: list[dict[str, Any]] = []
        for domain_id, entry in registry.items():
            pack = self._load_scenario_pack(domain_id)
            results.append({
                "domain_id": domain_id,
                "display_name": entry.get("display_name") or pack.get("display_name", domain_id.replace("_", " ").title()),
                "enabled": bool(entry.get("enabled", True)),
                "default_channel": entry.get("default_channel", "prod"),
                "scenario_pack_ref": entry.get("scenario_pack", domain_id),
            })
        return results

    def get_domain(self, domain_id: str) -> dict[str, Any]:
        registry = self._load_domain_registry()
        entry = registry.get(domain_id)
        if not entry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        pack = self._load_scenario_pack(domain_id)
        return {
            "domain_id": domain_id,
            "display_name": entry.get("display_name") or pack.get("display_name", domain_id.replace("_", " ").title()),
            "enabled": bool(entry.get("enabled", True)),
            "default_channel": entry.get("default_channel", "prod"),
            "scenario_pack_ref": entry.get("scenario_pack", domain_id),
            **{k: v for k, v in entry.items() if k not in ("display_name", "enabled", "default_channel", "scenario_pack")},
        }

    def get_domain_services(self, domain_id: str) -> dict[str, Any]:
        """Return the services dict for a domain (for proxy routing)."""
        registry = self._load_domain_registry()
        entry = registry.get(domain_id)
        if not entry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        services = entry.get("services")
        if not services:
            raise HTTPException(status_code=502, detail=f"No services configured for domain {domain_id}")
        return services

    def get_domain_yaml(self, domain_id: str) -> str:
        """Return raw YAML text for a single domain entry from the registry."""
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        entry = registry[domain_id]
        # Wrap in domains.{domain_id} for context
        return yaml.dump(
            {domain_id: entry},
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )

    def create_domain(self, domain_id: str, data: dict[str, Any]) -> dict[str, Any]:
        registry = self._load_domain_registry()
        if domain_id in registry:
            raise HTTPException(status_code=409, detail="domain_already_exists")

        # Merge provided data with defaults
        entry = {
            "display_name": data.get("display_name", domain_id.replace("_", " ").title()),
            "enabled": data.get("enabled", True),
            "default_channel": data.get("default_channel", "prod"),
            "scenario_pack": data.get("scenario_pack", domain_id),
        }
        # Copy extra fields (database, services, etc.)
        for key in ("database", "services", "database_url_env"):
            if key in data:
                entry[key] = data[key]

        registry[domain_id] = entry
        self._save_domain_registry(registry)

        # Create scenario pack if it doesn't exist
        pack_ref = entry["scenario_pack"]
        pack_path = self.config_dir / "scenario_packs" / pack_ref / "domain.yaml"
        if not pack_path.exists():
            pack_data = {
                "display_name": entry["display_name"],
                "ontology": {"entity_types": ["concept"], "strong_entity_types": []},
                "mining": {
                    "semantic_roles": ["concept", "parameter", "example", "note", "procedure_step", "constraint"],
                    "retrieval_policy": {
                        "raw_text": "primary",
                        "generated_question": "auxiliary",
                        "max_questions_per_segment": 2,
                    },
                },
                "serving": {"query_understanding": {}, "route_policy": {"default": {"lexical_bm25": {"weight": 1.0, "top_k": 50}, "dense_vector": {"weight": 1.0, "top_k": 50}}}},
            }
            self._save_yaml(pack_path, pack_data)

        return {"domain_id": domain_id, **entry}

    def update_domain_yaml(self, domain_id: str, yaml_text: str) -> None:
        """Update a domain entry in the registry from raw YAML text."""
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc

        # Accept both {domain_id: {...}} and plain {...}}
        if isinstance(parsed, dict) and domain_id in parsed and isinstance(parsed[domain_id], dict):
            entry = parsed[domain_id]
        elif isinstance(parsed, dict):
            entry = parsed
        else:
            raise HTTPException(status_code=400, detail="YAML root must be a mapping")

        registry[domain_id] = entry
        self._save_domain_registry(registry)

    def delete_domain(self, domain_id: str) -> None:
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        entry = registry.pop(domain_id)
        self._save_domain_registry(registry)

        # Optionally remove scenario pack
        pack_ref = entry.get("scenario_pack", domain_id)
        pack_path = self.config_dir / "scenario_packs" / pack_ref / "domain.yaml"
        if pack_path.exists():
            pack_path.unlink()
            # Remove empty dir
            try:
                pack_path.parent.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Scenario packs
    # ------------------------------------------------------------------

    def _load_scenario_pack(self, domain_id: str) -> dict[str, Any]:
        registry = self._load_domain_registry()
        pack_ref = registry.get(domain_id, {}).get("scenario_pack", domain_id)
        return self._load_yaml(self.config_dir / "scenario_packs" / pack_ref / "domain.yaml")

    def get_scenario(self, domain_id: str, section: str | None = None) -> dict[str, Any]:
        self.get_domain(domain_id)  # 404 if not found
        if section:
            return self._load_scenario_pack(domain_id).get(section, {})
        return self._load_scenario_pack(domain_id)

    def get_scenario_yaml(self, domain_id: str) -> str:
        """Return raw YAML text for a domain's scenario pack."""
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        pack_ref = registry[domain_id].get("scenario_pack", domain_id)
        path = self.config_dir / "scenario_packs" / pack_ref / "domain.yaml"
        return self._read_yaml_text(path)

    def update_scenario_yaml(self, domain_id: str, yaml_text: str) -> None:
        """Write raw YAML text to a domain's scenario pack."""
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        pack_ref = registry[domain_id].get("scenario_pack", domain_id)
        path = self.config_dir / "scenario_packs" / pack_ref / "domain.yaml"
        self._save_yaml_text(path, yaml_text)

    # ------------------------------------------------------------------
    # System config
    # ------------------------------------------------------------------

    def get_system_config(self, service_name: str) -> dict[str, Any]:
        path = self.config_dir / "system" / f"{service_name}.yaml"
        result = self._load_yaml(path)
        if not result:
            raise HTTPException(status_code=404, detail="config_not_found")
        return result

    def get_system_config_yaml(self, service_name: str) -> str:
        path = self.config_dir / "system" / f"{service_name}.yaml"
        return self._read_yaml_text(path)

    def update_system_config_yaml(self, service_name: str, yaml_text: str) -> None:
        path = self.config_dir / "system" / f"{service_name}.yaml"
        self._save_yaml_text(path, yaml_text)

    def list_system_configs(self) -> list[str]:
        system_dir = self.config_dir / "system"
        if not system_dir.exists():
            return []
        return sorted(p.stem for p in system_dir.glob("*.yaml"))

    # ------------------------------------------------------------------
    # LLM domain overrides (62号二期：chat/rerank/embedding 按域覆盖，
    # embedding.dimensions 锁全局——所有域共享同一定长向量列)
    # ------------------------------------------------------------------

    def _llm_domains_dir(self) -> Path:
        return self.config_dir / "system" / "llm_service.domains"

    def _llm_global_cfg(self) -> dict[str, Any]:
        cfg = self._load_yaml(self.config_dir / "system" / "llm_service.yaml")
        if not cfg:
            raise HTTPException(
                status_code=500, detail="llm_service.yaml missing in control plane config",
            )
        return cfg

    def list_llm_domain_overrides(self) -> list[dict[str, Any]]:
        """有覆盖文件的域清单 + 摘要（不含密钥）。"""
        items: list[dict[str, Any]] = []
        for p in sorted(self._llm_domains_dir().glob("*.yaml")):
            data = self._load_yaml(p)
            items.append({
                "domain_id": p.stem,
                "active_model": (data.get("provider") or {}).get("active_model"),
                "rerank_model": (data.get("rerank") or {}).get("model"),
                "embedding_model": (data.get("embedding") or {}).get("model"),
            })
        return items

    def get_llm_domain_override_yaml(self, domain_id: str) -> str:
        path = self._llm_domains_dir() / f"{domain_id}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail="config_not_found")
        return path.read_text(encoding="utf-8")

    def update_llm_domain_override_yaml(self, domain_id: str, yaml_text: str) -> None:
        """保存域覆盖：YAML 解析 → 结构校验 → merge 后可解析性校验 → 落盘。

        域必须在 domain_registry 中（防手误给不存在的域配覆盖）。
        """
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        override = _parse_llm_override_yaml(yaml_text)
        _validate_llm_domain_override(override)
        global_cfg = self._llm_global_cfg()
        merged = _deep_merge(global_cfg, override)
        _validate_llm_effective_models(merged)
        _validate_llm_embedding_override(global_cfg, merged, override)
        self._save_yaml_text(self._llm_domains_dir() / f"{domain_id}.yaml", yaml_text)

    def delete_llm_domain_override(self, domain_id: str) -> bool:
        """删除域覆盖文件；不存在返回 False（幂等）。"""
        path = self._llm_domains_dir() / f"{domain_id}.yaml"
        if not path.exists():
            return False
        path.unlink()
        return True

    def llm_effective_config(self, domain_id: str) -> dict[str, Any]:
        """merge 后的完整 effective 配置（llm_service 经 internal auth 拉取）。

        merge 只在此处做（62号裁定一）——绕过保存接口直接改盘上文件时，
        这里的校验仍是一道闸；破了返回 400，llm_service 侧会作废该域回落全局
        （embedding 维度漂移即是作废条件之一，62号二期）。
        """
        override_path = self._llm_domains_dir() / f"{domain_id}.yaml"
        if not override_path.exists():
            return self._llm_global_cfg()
        override = self._load_yaml(override_path)
        if override:
            _validate_llm_domain_override(override)
        global_cfg = self._llm_global_cfg()
        merged = _deep_merge(global_cfg, override)
        _validate_llm_effective_models(merged)
        _validate_llm_embedding_override(global_cfg, merged, override)
        return merged

    def llm_service_base_url(self) -> str | None:
        """llm_service 管理端点地址：注册表里第一个启用域的 llm_url（单容器同址）。"""
        for entry in self.list_domains():
            if not entry.get("enabled", True):
                continue
            did = entry.get("domain_id")
            if not did:
                continue
            try:
                svcs = self.get_domain_services(did)
            except Exception:  # noqa: BLE001
                continue
            url = svcs.get("llm_url")
            if url:
                return str(url).rstrip("/")
        return None

    async def notify_llm_reload(self, internal_secret: str) -> dict[str, Any]:
        """保存 LLM 配置后通知 llm_service 热加载（62号一期收尾：保存即生效）。

        尽力而为——失败不影响保存本身，只把结果带回响应给 UI 回显
        （失败时提示重启后台服务生效）。
        """
        base = self.llm_service_base_url()
        if not base:
            return {"ok": False, "detail": "domain_registry 中未找到 llm_url"}
        if not internal_secret:
            return {"ok": False, "detail": "内部密钥未配置"}
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                resp = await client.post(
                    f"{base}/api/v1/admin/reload-config",
                    headers={"X-Internal-Auth": internal_secret, "X-KB-Role": "admin"},
                )
        except httpx.HTTPError as exc:
            logger.warning("notify_llm_reload unreachable: %s", exc.__class__.__name__)
            return {"ok": False, "detail": f"llm_service 不可达（{exc.__class__.__name__}）"}
        if resp.status_code != 200:
            logger.warning("notify_llm_reload status=%s body=%s", resp.status_code, resp.text[:200])
            return {"ok": False, "detail": f"reload-config 返回 {resp.status_code}"}
        return {"ok": True, "detail": None}

    # ------------------------------------------------------------------
    # Onenet domain overrides（镜像 62号 LLM 域覆盖：一张网凭据按域差异。
    # 平铺 10 键全可覆盖；merge 只在控制面；mining 每请求拉 effective + TTL 缓存）
    # ------------------------------------------------------------------

    def _onenet_domains_dir(self) -> Path:
        return self.config_dir / "system" / "onenet.domains"

    def _onenet_global_cfg(self) -> dict[str, Any]:
        cfg = self._load_yaml(self.config_dir / "system" / "onenet.yaml")
        if not cfg:
            raise HTTPException(
                status_code=500, detail="onenet.yaml missing in control plane config",
            )
        return cfg

    def list_onenet_domain_overrides(self) -> list[dict[str, Any]]:
        """有覆盖文件的域清单 + 摘要（app_id 可示人，static_token 不出）。"""
        items: list[dict[str, Any]] = []
        for p in sorted(self._onenet_domains_dir().glob("*.yaml")):
            data = self._load_yaml(p)
            items.append({"domain_id": p.stem, "app_id": data.get("app_id")})
        return items

    def get_onenet_domain_override_yaml(self, domain_id: str) -> str:
        path = self._onenet_domains_dir() / f"{domain_id}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail="config_not_found")
        return path.read_text(encoding="utf-8")

    def update_onenet_domain_override_yaml(self, domain_id: str, yaml_text: str) -> None:
        """保存域覆盖：解析 → 结构校验 → merge 后凭据完整性 → 落盘。

        域必须在 domain_registry 中（同 LLM 覆盖，防手误）。
        """
        registry = self._load_domain_registry()
        if domain_id not in registry:
            raise HTTPException(status_code=404, detail="domain_not_found")
        override = _parse_onenet_override_yaml(yaml_text)
        _validate_onenet_override(override)
        merged = _deep_merge(self._onenet_global_cfg(), override)
        _validate_onenet_effective(merged, override)
        self._save_yaml_text(self._onenet_domains_dir() / f"{domain_id}.yaml", yaml_text)

    def delete_onenet_domain_override(self, domain_id: str) -> bool:
        """删除域覆盖文件；不存在返回 False（幂等）。"""
        path = self._onenet_domains_dir() / f"{domain_id}.yaml"
        if not path.exists():
            return False
        path.unlink()
        return True

    def onenet_effective_config(self, domain_id: str) -> dict[str, Any]:
        """merge 后的 effective 配置（mining 经 internal auth 每请求拉取）。

        merge 只在此处做；盘改坏文件在这里二次校验兜底 400——
        mining 侧拿到 400 会作废该域回落全局（与 llm_service 同语义）。
        """
        override_path = self._onenet_domains_dir() / f"{domain_id}.yaml"
        if not override_path.exists():
            return self._onenet_global_cfg()
        override = self._load_yaml(override_path)
        if override:
            _validate_onenet_override(override)
        merged = _deep_merge(self._onenet_global_cfg(), override)
        _validate_onenet_effective(merged, override)
        return merged

    def validate_onenet_global_yaml(self, yaml_text: str) -> None:
        """全局 onenet.yaml 保存前结构校验（键白名单 + 类型；不强制凭据非空）。

        修复既有暗病：此前 PUT 盲写，写坏后 mining 侧静默吞错全端点 503。
        未配置凭据是合法中间态（resolve → 503 onenet_not_configured 的宽容语义保留）。
        """
        data = _parse_onenet_override_yaml(yaml_text)
        _validate_onenet_override(data)

    # ------------------------------------------------------------------
    # Serving config snapshot (consumed by agent_serving over HTTP)
    # ------------------------------------------------------------------

    def get_serving_config(self) -> dict[str, Any]:
        """Aggregate per-domain config that agent_serving pulls on startup/reload.

        Returns only what Serving actually consumes: the registry fields it needs
        (enabled / default_channel / inline database) plus the scenario pack's
        ``serving:`` section. The ``ontology:`` / ``mining:`` sections are NOT
        included — Serving never reads them.
        """
        registry = self._load_domain_registry()
        domains: dict[str, Any] = {}
        for domain_id, entry in registry.items():
            pack = self._load_scenario_pack(domain_id)
            serving_section = pack.get("serving", {}) if isinstance(pack, dict) else {}
            domains[domain_id] = {
                "enabled": bool(entry.get("enabled", True)),
                "default_channel": entry.get("default_channel", "prod"),
                "database": entry.get("database"),  # inline block or None → Serving uses default DS
                "serving": serving_section,
            }
        return {"domains": domains}

    def mining_internal_base_url(self) -> str | None:
        """51号批次1：内部查询用 mining 基址（首个 enabled 域的 mining_url）。"""
        registry = self._load_domain_registry()
        seen: set[str] = set()
        for entry in registry.values():
            if not bool(entry.get("enabled", True)):
                continue
            url = (entry.get("services") or {}).get("mining_url")
            base = str(url).rstrip("/") if url else ""
            if base and base not in seen:
                seen.add(base)
                return base
        return None

    async def bound_domains_for(
        self, username: str, internal_secret: str
    ) -> tuple[set[str] | None, str]:
        """Compatibility wrapper around the richer per-domain access contract."""
        access, reason = await self.domain_access_for(username, internal_secret)
        if access is None:
            return None, reason
        return {
            str(grant["domain"])
            for grant in access.get("grants", [])
            if isinstance(grant, dict) and grant.get("domain")
        }, ""

    async def domain_access_for(
        self, username: str, internal_secret: str
    ) -> tuple[dict[str, Any] | None, str]:
        """问 mining 拿用户的域角色/能力；失败时调用方必须 fail closed。

        原因同时进日志与 503 detail——此前静默吞异常，现场只看到 503 无从排查
        （内网 2026-09-21 实发）。
        """
        from main_control_service.proxy import get_proxy_client

        if not username:
            return None, "empty_username"
        if not internal_secret:
            return None, "no_internal_secret"
        base = self.mining_internal_base_url()
        if not base:
            return None, "no_mining_url_in_registry"
        reason = ""
        try:
            resp = await get_proxy_client().get(
                f"{base}/api/kb/internal/users/{quote(username, safe='')}/domain-access",
                headers={"X-Internal-Auth": internal_secret},
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                return None, "invalid_payload"
            return payload, ""
        except httpx.HTTPStatusError as exc:
            reason = f"http_{exc.response.status_code}"
        except httpx.TimeoutException:
            reason = "timeout"
        except Exception as exc:  # noqa: BLE001 — fail-closed 由调用方处理
            reason = type(exc).__name__
        logger.warning(
            "mining domain-access query failed (%s): user=%s base=%s",
            reason, username, base,
        )
        return None, reason

    async def kb_count_for(self, domain_id: str, internal_secret: str) -> int | None:
        """51号批次1：删域保护；不可达返回 None（调用方 503）。"""
        from main_control_service.proxy import get_proxy_client

        if not internal_secret:
            return None
        base = self.mining_internal_base_url()
        if not base:
            return None
        try:
            resp = await get_proxy_client().get(
                f"{base}/api/kb/internal/domains/{domain_id}/kb-count",
                headers={"X-Internal-Auth": internal_secret},
                timeout=10.0,
            )
            resp.raise_for_status()
            return int(resp.json().get("kb_count", 0))
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# LLM domain override helpers (62号，module-level: 纯函数便于单测)
# ---------------------------------------------------------------------------

#: 覆盖文件允许的顶层键（62号二期起 embedding 可按域覆盖，dimensions 锁全局）。
_LLM_OVERRIDE_TOP_KEYS = frozenset({"provider", "rerank", "embedding"})
#: provider 段允许的键。
_LLM_PROVIDER_KEYS = frozenset({
    "active_model", "models", "provider_type",
    "timeout", "headers", "bypass_proxy",
})
#: rerank 段允许的键。
_LLM_RERANK_KEYS = frozenset({
    "base_url", "api_key", "model", "timeout", "bypass_proxy", "headers",
})
#: embedding 段允许的键（62号二期：模型/端点/密钥按域，dimensions 锁全局）。
_LLM_EMBEDDING_KEYS = frozenset({
    "base_url", "api_key", "model", "dimensions",
    "timeout", "bypass_proxy", "headers",
})
#: embedding merge 后必须非空的字段（dimensions 单独比对）。
_LLM_EMBEDDING_REQUIRED = (
    "base_url", "api_key", "model", "timeout", "bypass_proxy", "headers",
)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* into *base* — nested dicts merged, not replaced.

    与 llm_service/config.py 的同名函数语义一致（62号裁定一：merge 只在
    控制面做，故此处持有一份实现，不跨服务 import）。
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _parse_llm_override_yaml(yaml_text: str) -> dict[str, Any]:
    """解析覆盖 YAML；解析失败/根非映射 → 400。空文件视为空覆盖。"""
    if not yaml_text.strip():
        return {}
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="YAML root must be a mapping")
    return parsed


def _validate_llm_domain_override(override: dict[str, Any]) -> None:
    """覆盖文件结构校验（fail-loud 400）。

    只看覆盖自身的结构；merge 后的完整性（模型条目三要素、active_model
    可解析）由 _validate_llm_effective_models 在 merge 结果上校验。
    """
    unknown = set(override) - set(_LLM_OVERRIDE_TOP_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不允许的顶层键: {sorted(unknown)}"
                f"（允许 {sorted(_LLM_OVERRIDE_TOP_KEYS)}"
                "；worker/task/persist_writer/template 等运行时旋钮服务级共享，不可按域覆盖）"
            ),
        )
    prov = override.get("provider")
    if prov is not None:
        if not isinstance(prov, dict):
            raise HTTPException(status_code=400, detail="provider 段必须是映射")
        unknown = set(prov) - set(_LLM_PROVIDER_KEYS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"provider 段不允许的键: {sorted(unknown)}",
            )
        models = prov.get("models")
        if models is not None and not isinstance(models, dict):
            raise HTTPException(status_code=400, detail="provider.models 必须是映射")
        if isinstance(models, dict):
            for key, entry in models.items():
                if not isinstance(entry, dict):
                    raise HTTPException(
                        status_code=400, detail=f"provider.models.{key} 必须是映射",
                    )
    rer = override.get("rerank")
    if rer is not None:
        if not isinstance(rer, dict):
            raise HTTPException(status_code=400, detail="rerank 段必须是映射")
        unknown = set(rer) - set(_LLM_RERANK_KEYS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"rerank 段不允许的键: {sorted(unknown)}",
            )
    emb = override.get("embedding")
    if emb is not None:
        if not isinstance(emb, dict):
            raise HTTPException(status_code=400, detail="embedding 段必须是映射")
        unknown = set(emb) - set(_LLM_EMBEDDING_KEYS)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"embedding 段不允许的键: {sorted(unknown)}（允许 {sorted(_LLM_EMBEDDING_KEYS)}）",
            )
        dims = emb.get("dimensions")
        if dims is not None and (not isinstance(dims, int) or isinstance(dims, bool)):
            raise HTTPException(status_code=400, detail="embedding.dimensions 必须是整数")


def _validate_llm_embedding_override(
    global_cfg: dict[str, Any],
    merged: dict[str, Any],
    override: dict[str, Any],
) -> None:
    """embedding 覆盖的 merge 后校验（62号二期，fail-loud 400）。

    仅当覆盖实际含非空 embedding 段时执行——全局配置本就无 embedding 段的
    部署（或只覆盖 chat/rerank 的域）不受影响。两条闸：
    ① merge 后必填字段齐全（部分覆盖继承全局，但结果必须完整）；
    ② merge 后 dimensions 与全局一致——所有域共享同一定长向量列
    （vector(N)），维度漂移会在入库/检索两侧同时炸，维度差异化属二期后工程。
    """
    if not override.get("embedding"):
        return
    merged_emb = merged.get("embedding") or {}
    # 存在性而非真值：bypass_proxy: false / headers: {} 是合法完整值，
    # 只有缺键、None、空串才算缺。
    missing = [
        f for f in _LLM_EMBEDDING_REQUIRED
        if merged_emb.get(f) is None
        or (isinstance(merged_emb.get(f), str) and not str(merged_emb.get(f)).strip())
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"embedding merge 后仍缺必填字段 {missing}"
                "（同名部分覆盖需保证 merge 完整）"
            ),
        )
    gdim = (global_cfg.get("embedding") or {}).get("dimensions")
    mdim = merged_emb.get("dimensions")
    if mdim != gdim:
        raise HTTPException(
            status_code=400,
            detail=(
                f"embedding.dimensions 锁全局：域覆盖合并后为 {mdim}，全局为 {gdim}"
                f"——所有域共享同一定长向量列（当前 vector({gdim})），维度不可按域改变；"
                "更换嵌入模型请保持 dimensions 与全局一致"
            ),
        )


def _validate_llm_effective_models(effective: dict[str, Any]) -> None:
    """merge 后的可解析性校验（fail-loud 400）。

    ① provider.models 每个条目三要素齐全（base_url/api_key/model）——
    覆盖可部分覆盖同名条目，但 merge 结果必须完整；
    ② active_model 在 merge 后的 models 里可解析（键名或 API model id）。
    """
    prov = effective.get("provider") or {}
    models = prov.get("models") or {}
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        missing = [f for f in ("base_url", "api_key", "model") if not entry.get(f)]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"provider.models.{key} merge 后仍缺必填字段 {missing}（同名部分覆盖需保证 merge 完整）",
            )
    active = prov.get("active_model")
    if models and active:
        resolvable = active in models or any(
            isinstance(e, dict) and e.get("model") == active for e in models.values()
        )
        if not resolvable:
            raise HTTPException(
                status_code=400,
                detail=f"active_model '{active}' 在 merge 后的 provider.models 中不可解析",
            )


# ---------------------------------------------------------------------------
# Onenet domain override helpers（平铺结构，纯函数便于单测）
# ---------------------------------------------------------------------------

#: onenet 覆盖/全局文件允许的键（与 mining OnenetConfig 字段一一对应，全可按域覆盖）。
_ONENET_KEYS = frozenset({
    "app_id", "static_token", "token_url", "search_url",
    "source_type", "timeout", "verify_tls", "chunk_width",
    "page_size", "throttle_seconds",
})
#: 字符串键 / 整数键（bool 不是 int，isinstance(True, int) 为真需排除）。
_ONENET_STR_KEYS = ("app_id", "static_token", "token_url", "search_url")
_ONENET_INT_KEYS = ("source_type", "timeout", "chunk_width", "page_size")
#: 接口硬上限（镜像 OnenetConfig：单查询 from+size ≤ 10000）。
_ONENET_RANGE_MAX = 10000


def _parse_onenet_override_yaml(yaml_text: str) -> dict[str, Any]:
    """解析 onenet 覆盖 YAML（全局文件保存校验同用此函数）。

    解析失败/根非映射 → 400；空文件视为空覆盖。
    """
    if not yaml_text.strip():
        return {}
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="YAML root must be a mapping")
    return parsed


def _validate_onenet_override(override: dict[str, Any]) -> None:
    """覆盖文件结构校验（fail-loud 400）：键白名单 + 类型 + 范围。

    范围与 mining OnenetConfig.__post_init__ 一致（chunk_width/page_size ∈
    [1, 10000]）；不在此处要求凭据非空——merge 后的完整性由
    _validate_onenet_effective 把关（全局基底可能补齐）。
    """
    unknown = set(override) - set(_ONENET_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不允许的键: {sorted(unknown)}（允许 {sorted(_ONENET_KEYS)}）",
        )
    for key in _ONENET_STR_KEYS:
        val = override.get(key)
        if val is not None and not isinstance(val, str):
            raise HTTPException(status_code=400, detail=f"{key} 必须是字符串")
    for key in _ONENET_INT_KEYS:
        val = override.get(key)
        if val is not None and (not isinstance(val, int) or isinstance(val, bool)):
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数")
    if override.get("verify_tls") is not None and not isinstance(override["verify_tls"], bool):
        raise HTTPException(status_code=400, detail="verify_tls 必须是布尔值")
    ts = override.get("throttle_seconds")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float))):
        raise HTTPException(status_code=400, detail="throttle_seconds 必须是数值")
    for key in ("chunk_width", "page_size"):
        val = override.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and not 1 <= val <= _ONENET_RANGE_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"{key} 必须在 [1, {_ONENET_RANGE_MAX}] 内（接口硬上限）",
            )


def _validate_onenet_effective(
    merged: dict[str, Any], override: dict[str, Any],
) -> None:
    """merge 后凭据完整性闸（fail-loud 400，镜像 embedding merge 完整性样式）。

    仅当覆盖非空时执行：部分覆盖（如只改 timeout）继承全局凭据，
    但 merge 结果必须有可用凭据——否则该域配置形同虚设，保存时就该拦下。
    """
    if not override:
        return
    missing = [
        f for f in ("app_id", "static_token")
        if not str(merged.get(f) or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"onenet merge 后仍缺必填凭据 {missing}（部分覆盖需保证 merge 完整）",
        )
