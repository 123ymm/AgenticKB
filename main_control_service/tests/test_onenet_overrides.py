# -*- coding: utf-8 -*-
"""onenet 域覆盖测试（62号 LLM 域覆盖的镜像通路）.

覆盖：CRUD 回路 / 覆盖校验矩阵 / merge 凭据完整性闸 / effective 合并与
盘改坏文件兜底 / 未知域 404 / 域删除级联 / 全局 PUT 结构校验（修盲写暗病）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main_control_service.main import create_app

_ONENET_YAML = """\
app_id: global-app
static_token: global-tok
token_url: http://global.example/token
search_url: http://global.example/search
chunk_width: 8000
"""

_REGISTRY_YAML = """\
domains:
  odn:
    display_name: ODN
    scenario_pack: odn
"""


def _onenet_client(tmp_path: Path) -> TestClient:
    """带 onenet.yaml 全局基底与域注册的临时控制面客户端。"""
    (tmp_path / "system").mkdir(exist_ok=True)
    (tmp_path / "system" / "onenet.yaml").write_text(_ONENET_YAML, encoding="utf-8")
    (tmp_path / "system" / "auth.yaml").write_text(
        "enabled: false\njwt_secret: test\ninternal_verify_secret: test-internal\n",
        encoding="utf-8",
    )
    (tmp_path / "domain_registry.yaml").write_text(_REGISTRY_YAML, encoding="utf-8")
    return TestClient(create_app(config_dir=tmp_path))


def _put_override(c: TestClient, body: str, domain: str = "odn"):
    return c.put(
        f"/api/v1/system/onenet/domains/{domain}/raw",
        content=body, headers={"Content-Type": "text/yaml"},
    )


# ---------------------------------------------------------------- CRUD 回路


def test_onenet_override_crud_roundtrip(tmp_path: Path) -> None:
    with _onenet_client(tmp_path) as c:
        r = _put_override(c, "app_id: odn-app\ntoken_url: http://odn.example/token\n")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}          # 无 reload 字段（无 notify 机制）

        items = c.get("/api/v1/system/onenet/domains").json()["items"]
        assert items == [{"domain_id": "odn", "app_id": "odn-app"}]  # token 不出

        raw = c.get("/api/v1/system/onenet/domains/odn/raw")
        assert raw.status_code == 200
        assert "odn-app" in raw.text

        eff = c.get("/api/v1/system/onenet/effective/odn").json()
        assert eff["app_id"] == "odn-app"          # 覆盖生效
        assert eff["token_url"] == "http://odn.example/token"
        assert eff["static_token"] == "global-tok"  # 未覆盖键继承全局
        assert eff["chunk_width"] == 8000

        r = c.delete("/api/v1/system/onenet/domains/odn/raw")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert c.delete("/api/v1/system/onenet/domains/odn/raw").json()["deleted"] is False
        eff2 = c.get("/api/v1/system/onenet/effective/odn").json()
        assert eff2["app_id"] == "global-app"      # 删除后回落全局


def test_onenet_effective_without_override_is_global(tmp_path: Path) -> None:
    with _onenet_client(tmp_path) as c:
        eff = c.get("/api/v1/system/onenet/effective/odn").json()
        assert eff["app_id"] == "global-app"
        assert eff["static_token"] == "global-tok"


# ---------------------------------------------------------------- 校验矩阵


def test_onenet_override_validation_matrix(tmp_path: Path) -> None:
    cases = {
        # 未知键（平铺白名单 10 键之外）→ 400
        "unknown-key": "hack: 1\n",
        # str 键给 int → 400
        "str-type": "app_id: 123\n",
        # int 键给字符串 / 布尔 → 400（bool 是 int 子集，须显式排除）
        "int-type": "timeout: '60'\n",
        "bool-as-int": "timeout: true\n",
        # verify_tls 非布尔 → 400
        "verify-tls": "verify_tls: 'true'\n",
        # throttle_seconds 非数值 → 400
        "throttle": "throttle_seconds: fast\n",
        # 接口硬上限越界 → 400
        "chunk-over": "chunk_width: 10001\n",
        "page-under": "page_size: 0\n",
        # 根非映射 / 坏 YAML → 400
        "not-map": "- a\n- b\n",
        "bad-yaml": "a: [unclosed\n",
    }
    with _onenet_client(tmp_path) as c:
        for name, body in cases.items():
            r = _put_override(c, body)
            assert r.status_code == 400, f"{name}: {r.text}"

        # 空文档不落盘（与 LLM 覆盖同守卫；「什么都不盖」用 DELETE 表达）
        assert _put_override(c, "\n").status_code == 400

        # 只盖调优参数（凭据继承全局）→ 200
        r = _put_override(c, "timeout: 60\nverify_tls: true\nthrottle_seconds: 0.5\n")
        assert r.status_code == 200, r.text
        eff = c.get("/api/v1/system/onenet/effective/odn").json()
        assert eff["timeout"] == 60 and eff["verify_tls"] is True


def test_onenet_merge_missing_credentials_rejected(tmp_path: Path) -> None:
    """merge 后凭据完整性闸：部分覆盖不得留下不可用配置。"""
    with _onenet_client(tmp_path) as c:
        # 全局缺 static_token + 覆盖只写 app_id → merge 仍缺 → 400
        (tmp_path / "system" / "onenet.yaml").write_text(
            "app_id: global-app\n", encoding="utf-8")
        r = _put_override(c, "app_id: odn-app\n")
        assert r.status_code == 400
        assert "merge 后仍缺必填凭据" in r.json()["detail"]

        # 覆盖自带全套凭据 → merge 完整 → 200
        r = _put_override(c, "app_id: odn-app\nstatic_token: odn-tok\n")
        assert r.status_code == 200, r.text


def test_onenet_override_unknown_domain_404(tmp_path: Path) -> None:
    with _onenet_client(tmp_path) as c:
        r = _put_override(c, "app_id: x\nstatic_token: y\n", domain="ghost")
        assert r.status_code == 404
        assert c.get("/api/v1/system/onenet/domains/ghost/raw").status_code == 404


def test_onenet_disk_edited_file_rejected_at_effective(tmp_path: Path) -> None:
    """绕过 PUT 改盘上覆盖文件 → effective 端点二次校验兜底 400。"""
    with _onenet_client(tmp_path) as c:
        bad_dir = tmp_path / "system" / "onenet.domains"
        bad_dir.mkdir(parents=True)
        (bad_dir / "odn.yaml").write_text("hack: 1\n", encoding="utf-8")
        r = c.get("/api/v1/system/onenet/effective/odn")
        assert r.status_code == 400

        # 盘改的合法覆盖照常生效（兜底只拦结构破坏）
        (bad_dir / "odn.yaml").write_text("app_id: disk-app\n", encoding="utf-8")
        r = c.get("/api/v1/system/onenet/effective/odn")
        assert r.status_code == 200
        assert r.json()["app_id"] == "disk-app"


# ---------------------------------------------------------------- 全局基底 / 级联


def test_onenet_global_put_structure_validation(tmp_path: Path) -> None:
    """全局 onenet.yaml 保存前结构校验（修既有盲写暗病）。"""
    with _onenet_client(tmp_path) as c:
        # 未知键 / 坏 YAML → 400 拒写
        for bad in ("unknown_key: 1\n", "a: [unclosed\n"):
            r = c.put(
                "/api/v1/system/onenet/raw",
                content=bad, headers={"Content-Type": "text/yaml"},
            )
            assert r.status_code == 400, r.text

        # 合法部分文件（凭据为空是中间态）→ 200 落盘
        r = c.put(
            "/api/v1/system/onenet/raw",
            content="app_id: new-app\n",
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200, r.text
        assert c.get("/api/v1/system/onenet").json()["app_id"] == "new-app"

        # 全局基底轮换后，部分覆盖 merge 到新基底（未覆盖凭据继承新值）
        c.put(
            "/api/v1/system/onenet/raw",
            content="app_id: global-app\nstatic_token: rotated-tok\n",
            headers={"Content-Type": "text/yaml"},
        )
        r = _put_override(c, "app_id: odn-app\n")
        assert r.status_code == 200, r.text
        eff = c.get("/api/v1/system/onenet/effective/odn").json()
        assert eff["app_id"] == "odn-app"
        assert eff["static_token"] == "rotated-tok"


def test_domain_deletion_cascades_onenet_override(
    tmp_path: Path, monkeypatch,
) -> None:
    """删域级联清理 onenet 覆盖文件（62号 LLM 覆盖同规则）。"""
    from main_control_service.service import YamlConfigService

    async def no_kbs(self, domain_id, internal_secret):
        return 0                       # 删域保护：域下无 KB

    monkeypatch.setattr(YamlConfigService, "kb_count_for", no_kbs)

    with _onenet_client(tmp_path) as c:
        assert _put_override(c, "app_id: odn-app\nstatic_token: t\n").status_code == 200
        r = c.delete("/api/v1/domains/odn")
        assert r.status_code == 200, r.text
        assert c.get("/api/v1/system/onenet/domains").json()["items"] == []
