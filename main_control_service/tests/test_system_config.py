"""系统配置（system/*.yaml）读写通路测试。

聚焦 ``site`` 品牌块的 GET(JSON) / GET(raw) / PUT(raw) 通路。这些路由是通用系统配置
CRUD，``site`` 只是 ``ui.yaml`` 里的内容，因此后端零代码改动即支持品牌可配。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main_control_service.main import create_app

_UI_YAML = """\
site:
  title: CoreMasterKB
  name: CoreMaster
  badge: Knowledge Base
  logo_text: KB
  icon: ""
mining_api_base: http://localhost:8901
"""


def _client(tmp_path: Path) -> TestClient:
    """构造一个指向临时 config 目录的 main_control 测试客户端。"""
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "ui.yaml").write_text(_UI_YAML, encoding="utf-8")
    (tmp_path / "system" / "auth.yaml").write_text(
        "enabled: false\njwt_secret: test\ninternal_verify_secret: test-internal\n",
        encoding="utf-8",
    )
    # 用上下文管理器触发 lifespan（建/拆 proxy client）。
    return TestClient(create_app(config_dir=tmp_path))


def test_get_system_ui_returns_site_block(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/api/v1/system/ui")
        assert r.status_code == 200
        site = r.json()["site"]
        assert site["title"] == "CoreMasterKB"
        assert site["name"] == "CoreMaster"
        assert site["badge"] == "Knowledge Base"
        assert site["logo_text"] == "KB"
        assert site["icon"] == ""
        # 其余键被通用通路原样返回（不被 site 影响）。
        assert r.json()["mining_api_base"] == "http://localhost:8901"


def test_put_system_ui_raw_updates_site_and_preserves_other_keys(tmp_path: Path) -> None:
    new_yaml = (
        "site:\n"
        "  title: 我的知识库\n"
        "  name: MyKB\n"
        "  badge: KB\n"
        "  logo_text: MK\n"
        '  icon: "data:image/svg+xml;base64,e30="\n'
        "mining_api_base: http://localhost:8901\n"
    )
    with _client(tmp_path) as c:
        r = c.put(
            "/api/v1/system/ui/raw",
            content=new_yaml,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text
        site = c.get("/api/v1/system/ui").json()["site"]
        assert site["title"] == "我的知识库"
        assert site["name"] == "MyKB"
        assert site["icon"].startswith("data:image/svg+xml")
        # 其它键保留（PUT 是整文件覆写，由调用方保证完整；这里验证通路不丢非 site 键）。
        assert c.get("/api/v1/system/ui").json()["mining_api_base"] == "http://localhost:8901"


def test_get_system_config_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/api/v1/system/does-not-exist")
        assert r.status_code == 404


def test_obsolete_sync_and_reload_endpoints_are_removed(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        paths = {route.path for route in c.app.routes}
        assert "/api/v1/code-sync" not in paths
        assert "/api/v1/admin/reload-serving" not in paths


# ---------------------------------------------------------------------------
# 62号第一期：LLM 域覆盖（chat+rerank 按域覆盖，embedding 锁全局）
# ---------------------------------------------------------------------------

_LLM_YAML = """\
provider:
  active_model: g-active
  models:
    g-active:
      model: g-active
      base_url: https://global.example/v1
      api_key: gk1
rerank:
  base_url: https://global.example/rerank
  api_key: rk-global
  model: rerank-global
"""

_REGISTRY_YAML = """\
domains:
  odn:
    display_name: ODN
    scenario_pack: odn
"""


def _llm_client(tmp_path: Path) -> TestClient:
    """指向临时 config 目录的 main_control 测试客户端（带 llm_service.yaml 与域注册）。"""
    (tmp_path / "system").mkdir(exist_ok=True)
    (tmp_path / "system" / "llm_service.yaml").write_text(_LLM_YAML, encoding="utf-8")
    (tmp_path / "system" / "auth.yaml").write_text(
        "enabled: false\njwt_secret: test\ninternal_verify_secret: test-internal\n",
        encoding="utf-8",
    )
    (tmp_path / "domain_registry.yaml").write_text(_REGISTRY_YAML, encoding="utf-8")
    return TestClient(create_app(config_dir=tmp_path))


def test_llm_domain_override_crud_roundtrip(tmp_path: Path) -> None:
    override = (
        "provider:\n"
        "  active_model: odn-chat\n"
        "  models:\n"
        "    odn-chat:\n"
        "      model: odn-chat\n"
        "      base_url: https://odn.example/v1\n"
        "      api_key: odn-k2\n"
        "rerank:\n"
        "  model: rerank-odn\n"
    )
    with _llm_client(tmp_path) as c:
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content=override, headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text

        items = c.get("/api/v1/system/llm_service/domains").json()["items"]
        assert items == [{"domain_id": "odn", "active_model": "odn-chat",
                          "rerank_model": "rerank-odn"}]

        raw = c.get("/api/v1/system/llm_service/domains/odn/raw")
        assert raw.status_code == 200
        assert "odn-chat" in raw.text

        eff = c.get("/api/v1/system/llm_service/effective/odn").json()
        # 域差异生效 + 全局模型仍可用（merge 按键合并）+ embedding 锁全局
        assert eff["provider"]["active_model"] == "odn-chat"
        assert "g-active" in eff["provider"]["models"]
        assert "odn-chat" in eff["provider"]["models"]
        assert eff["rerank"]["model"] == "rerank-odn"
        assert eff["rerank"]["api_key"] == "rk-global"   # 未覆盖字段继承全局

        r = c.delete("/api/v1/system/llm_service/domains/odn/raw")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert c.delete("/api/v1/system/llm_service/domains/odn/raw").json()["deleted"] is False
        eff2 = c.get("/api/v1/system/llm_service/effective/odn").json()
        assert eff2["provider"]["active_model"] == "g-active"   # 回落全局


def test_llm_domain_override_effective_without_override_is_global(tmp_path: Path) -> None:
    with _llm_client(tmp_path) as c:
        eff = c.get("/api/v1/system/llm_service/effective/odn").json()
        assert eff["provider"]["active_model"] == "g-active"


def test_llm_domain_override_validation_matrix(tmp_path: Path) -> None:
    cases = {
        # embedding 任何覆盖 → 400（62号锁全局）
        "embedding": "embedding:\n  model: other-emb\n",
        # 未知顶层键 → 400
        "worker": "worker:\n  concurrency: 99\n",
        # active_model 在 merge 后 models 里不可解析 → 400
        "active": "provider:\n  active_model: no-such-model\n",
        # 新增模型条目缺三要素（merge 后仍缺）→ 400
        "partial-entry": (
            "provider:\n"
            "  models:\n"
            "    odn-chat:\n"
            "      model: odn-chat\n"
            "      api_key: k\n"
        ),
        # provider 段不允许的键 → 400
        "bad-provider-key": "provider:\n  unknown_key: x\n",
        # 非映射根 → 400
        "bad-root": "- a\n- b\n",
    }
    with _llm_client(tmp_path) as c:
        for name, body in cases.items():
            r = c.put(
                "/api/v1/system/llm_service/domains/odn/raw",
                content=body, headers={"Content-Type": "text/yaml"},
            )
            assert r.status_code == 400, f"{name}: {r.text}"

        # 同名条目部分覆盖（只换 api_key）→ merge 完整 → 200
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content="provider:\n  models:\n    g-active:\n      api_key: odn-key\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text
        eff = c.get("/api/v1/system/llm_service/effective/odn").json()
        entry = eff["provider"]["models"]["g-active"]
        assert entry["api_key"] == "odn-key"
        assert entry["base_url"] == "https://global.example/v1"   # 继承全局


def test_llm_domain_override_unknown_domain_404(tmp_path: Path) -> None:
    with _llm_client(tmp_path) as c:
        r = c.put(
            "/api/v1/system/llm_service/domains/ghost/raw",
            content="rerank:\n  model: r\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 404
        assert c.get("/api/v1/system/llm_service/domains/ghost/raw").status_code == 404


def test_llm_domain_override_disk_edited_file_rejected_at_effective(tmp_path: Path) -> None:
    """绕过 PUT 直接改盘上文件 → effective 端点校验兜底（62号 §五 B2 的前置闸）。"""
    with _llm_client(tmp_path) as c:
        bad_dir = tmp_path / "system" / "llm_service.domains"
        bad_dir.mkdir(parents=True)
        (bad_dir / "odn.yaml").write_text("embedding:\n  model: hacked\n", encoding="utf-8")
        r = c.get("/api/v1/system/llm_service/effective/odn")
        assert r.status_code == 400
