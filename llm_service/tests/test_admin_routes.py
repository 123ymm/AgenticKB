"""LLM 管理面路由清单测试。

旧 reload-config（aaaad19 删除的全运行时热切换）曾在此断言不存在；62号 B3
刻意重新引入了**收窄版** reload-config（只换 DomainConfigs + 清 provider
缓存，运行时旋钮仍需重启），本文件随之改写为断言三个端点齐备。
语义与闸门的测试在 test_domain_config.py。
"""

from llm_service.api.admin import router


def test_admin_route_inventory():
    paths = {route.path for route in router.routes}
    assert "/api/v1/admin/worker-status" in paths
    assert "/api/v1/admin/cleanup" in paths
    assert "/api/v1/admin/config-status" in paths    # 62号 D1
    assert "/api/v1/admin/reload-config" in paths    # 62号 B3（收窄版热加载）
