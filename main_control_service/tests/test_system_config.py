"""系统配置（system/*.yaml）读写通路测试。

聚焦 ``site`` 品牌块的 GET(JSON) / GET(raw) / PUT(raw) 通路。这些路由是通用系统配置
CRUD，``site`` 只是 ``ui.yaml`` 里的内容，因此后端零代码改动即支持品牌可配。
"""
from __future__ import annotations

from pathlib import Path

import pytest
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
# 62号：LLM 域覆盖（chat/rerank/embedding 按域覆盖，embedding.dimensions 锁全局）
# ---------------------------------------------------------------------------

_LLM_YAML = """\
provider:
  active_model: g-active
  models:
    g-active:
      model: g-active
      base_url: https://global.example/v1
      api_key: gk1
embedding:
  base_url: https://global.example/embeddings
  api_key: ek-global
  model: embedding-3
  dimensions: 1024
  timeout: 60
  bypass_proxy: false
  headers: {}
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
        "embedding:\n"
        "  model: emb-odn\n"
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
                          "rerank_model": "rerank-odn", "embedding_model": "emb-odn"}]

        raw = c.get("/api/v1/system/llm_service/domains/odn/raw")
        assert raw.status_code == 200
        assert "odn-chat" in raw.text

        eff = c.get("/api/v1/system/llm_service/effective/odn").json()
        # 域差异生效 + 全局模型仍可用（merge 按键合并）+ embedding 同维度覆盖继承全局
        assert eff["provider"]["active_model"] == "odn-chat"
        assert "g-active" in eff["provider"]["models"]
        assert "odn-chat" in eff["provider"]["models"]
        assert eff["rerank"]["model"] == "rerank-odn"
        assert eff["rerank"]["api_key"] == "rk-global"   # 未覆盖字段继承全局
        assert eff["embedding"]["model"] == "emb-odn"     # 62号二期：embedding 按域
        assert eff["embedding"]["api_key"] == "ek-global"  # 未覆盖字段继承全局
        assert eff["embedding"]["dimensions"] == 1024     # dimensions 锁全局

        r = c.delete("/api/v1/system/llm_service/domains/odn/raw")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert c.delete("/api/v1/system/llm_service/domains/odn/raw").json()["deleted"] is False
        eff2 = c.get("/api/v1/system/llm_service/effective/odn").json()
        assert eff2["provider"]["active_model"] == "g-active"   # 回落全局
        assert eff2["embedding"]["model"] == "embedding-3"


def test_llm_domain_override_effective_without_override_is_global(tmp_path: Path) -> None:
    with _llm_client(tmp_path) as c:
        eff = c.get("/api/v1/system/llm_service/effective/odn").json()
        assert eff["provider"]["active_model"] == "g-active"


def test_llm_domain_override_validation_matrix(tmp_path: Path) -> None:
    cases = {
        # 未知顶层键 → 400（运行时旋钮服务级共享）
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
        # embedding 维度漂移 → 400（62号二期：dimensions 锁全局）
        "dims-mismatch": "embedding:\n  dimensions: 2048\n",
        "dims-mismatch-with-model": "embedding:\n  model: emb-odn\n  dimensions: 2048\n",
        # embedding 段不允许的键 → 400
        "bad-embedding-key": "embedding:\n  unknown_key: x\n",
        # embedding 非映射 → 400
        "embedding-not-map": "embedding:\n  - a\n",
        # dimensions 非整数（字符串）→ 400
        "dims-not-int": "embedding:\n  dimensions: '1024'\n",
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

        # embedding 同维度覆盖（只换 model，dimensions 继承全局）→ 200（62号二期）
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content="embedding:\n  model: other-emb\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text


def test_llm_embedding_override_matrix(tmp_path: Path) -> None:
    """embedding 覆盖边界：merge 完整性闸在无 embedding 全局时的行为。"""
    with _llm_client(tmp_path) as c:
        # 全局无 embedding 段时覆盖 embedding.model → merge 后必填字段缺失 → 400
        no_embedding_global = (
            _LLM_YAML[: _LLM_YAML.index("embedding:")]
            + _LLM_YAML[_LLM_YAML.index("rerank:"):]
        )
        (tmp_path / "system" / "llm_service.yaml").write_text(
            no_embedding_global, encoding="utf-8")
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content="embedding:\n  model: x\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 400
        assert "embedding merge 后仍缺必填字段" in r.json()["detail"]

        # 全局无 dimensions、覆盖显式给 dimensions → 与全局 None 不一致 → 400
        (tmp_path / "system" / "llm_service.yaml").write_text(
            _LLM_YAML.replace("  dimensions: 1024\n", ""),
            encoding="utf-8",
        )
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content="embedding:\n  model: embedding-3\n  dimensions: 1024\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 400
        assert "dimensions 锁全局" in r.json()["detail"]

        # 同维度显式写 dimensions（= 全局）→ 200（显式等值不算漂移）
        (tmp_path / "system" / "llm_service.yaml").write_text(_LLM_YAML, encoding="utf-8")
        r = c.put(
            "/api/v1/system/llm_service/domains/odn/raw",
            content="embedding:\n  model: other-emb\n  dimensions: 1024\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text


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
        # 维度漂移是 62号二期的拒绝条件（同维度 embedding 覆盖已合法）
        (bad_dir / "odn.yaml").write_text(
            "embedding:\n  model: hacked\n  dimensions: 2048\n", encoding="utf-8")
        r = c.get("/api/v1/system/llm_service/effective/odn")
        assert r.status_code == 400
        assert "dimensions 锁全局" in r.json()["detail"]

        # 盘改的同维度 embedding 覆盖是合法配置 → effective 照常返回（闸只拦结构破坏）
        (bad_dir / "odn.yaml").write_text(
            "embedding:\n  model: hacked-same-dims\n", encoding="utf-8")
        r = c.get("/api/v1/system/llm_service/effective/odn")
        assert r.status_code == 200
        assert r.json()["embedding"]["model"] == "hacked-same-dims"


# ---------------------------------------------------------------------------
# 保存即生效：LLM 配置保存后自动通知 llm_service 热加载
# ---------------------------------------------------------------------------

@pytest.fixture
def reload_notifier(monkeypatch):
    """拦截 YamlConfigService.notify_llm_reload，记录调用并默认返回成功。"""
    from unittest.mock import AsyncMock
    from main_control_service.service import YamlConfigService

    mock = AsyncMock(return_value={"ok": True, "detail": None})
    monkeypatch.setattr(YamlConfigService, "notify_llm_reload", mock)
    return mock


def test_llm_service_raw_put_notifies_reload(tmp_path: Path, reload_notifier) -> None:
    with _llm_client(tmp_path) as c:
        r = c.put(
            "/api/v1/system/llm_service/raw",
            content=_LLM_YAML, headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["llm_reload"] == {"ok": True, "detail": None}
        reload_notifier.assert_awaited_once_with("test-internal")  # auth.yaml 的 secret


def test_other_system_config_put_does_not_notify(tmp_path: Path, reload_notifier) -> None:
    with _llm_client(tmp_path) as c:
        r = c.put(
            "/api/v1/system/ui/raw",
            content="site:\n  title: t\n", headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200
        assert "llm_reload" not in r.json()
        reload_notifier.assert_not_awaited()


def test_override_put_and_delete_notify_reload(tmp_path: Path, reload_notifier) -> None:
    override = "rerank:\n  model: rerank-odn\n"
    with _llm_client(tmp_path) as c:
        r = c.put("/api/v1/system/llm_service/domains/odn/raw",
                  content=override, headers={"Content-Type": "text/yaml"})
        assert r.status_code == 200
        assert r.json()["llm_reload"]["ok"] is True

        r = c.delete("/api/v1/system/llm_service/domains/odn/raw")
        assert r.json()["llm_reload"]["ok"] is True      # 真删了 → 通知

        r = c.delete("/api/v1/system/llm_service/domains/odn/raw")
        assert "llm_reload" not in r.json()              # 幂等空删 → 配置没变不通知
    assert reload_notifier.await_count == 2


def _registry_with_llm_url(tmp_path: Path) -> None:
    (tmp_path / "domain_registry.yaml").write_text(
        "domains:\n"
        "  odn:\n"
        "    display_name: ODN\n"
        "    services:\n"
        "      llm_url: http://localhost:8900\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_notify_llm_reload_matrix(tmp_path: Path, monkeypatch) -> None:
    """notify_llm_reload 单元矩阵：无地址/密钥缺失/网络错/非200/成功。"""
    import httpx

    from main_control_service.service import YamlConfigService

    svc = YamlConfigService(config_dir=tmp_path)

    # 注册表没有 services 段 → 没有地址
    assert await svc.notify_llm_reload("s") == {
        "ok": False, "detail": "domain_registry 中未找到 llm_url"}

    _registry_with_llm_url(tmp_path)
    # 内部密钥缺失 → 拒发
    assert (await svc.notify_llm_reload(""))["ok"] is False

    calls: dict[str, tuple] = {}

    class _FakeResp:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = ""

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None):
            calls["last"] = (url, headers or {})
            if calls.get("status") == "unreachable":
                raise httpx.ConnectError("boom")
            return _FakeResp(calls.get("status", 200))

    monkeypatch.setattr("main_control_service.service.httpx.AsyncClient", _FakeClient)

    # 成功：打到注册表 llm_url 的 reload-config，带双头
    out = await svc.notify_llm_reload("sec-1")
    assert out == {"ok": True, "detail": None}
    url, headers = calls["last"]
    assert url == "http://localhost:8900/api/v1/admin/reload-config"
    assert headers == {"X-Internal-Auth": "sec-1", "X-KB-Role": "admin"}

    # 非 200
    calls["status"] = 502
    assert (await svc.notify_llm_reload("sec-1"))["detail"] == "reload-config 返回 502"

    # 网络不可达
    calls["status"] = "unreachable"
    assert (await svc.notify_llm_reload("sec-1"))["ok"] is False
