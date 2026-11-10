"""presign 契约：expires 必须进 URL；presign_public_base 改写交付地址。

presign 签名本身是本地计算，但 SDK 需要先知道桶的 region：client 未显式传
``region`` 时会发一次 GetBucketLocation 网络请求（minio/api.py ``_get_region``），
传了则纯本地。本文件全部用例显式 ``region="us-east-1"`` 钉死离线签名，
用假凭证即可构造真客户端验证：
- expires 参数漏传时 SDK 默认 604800（7 天）——曾在生产 URL 里实测暴露；
- endpoint 是 docker 网络名（minio:9000）时浏览器不可达，须按
  presign_public_base 改写为 nginx 反代入口（代理保持 Host=endpoint，
  签名与其后的查询串在改写前后逐字节一致）。
"""
from __future__ import annotations

import pytest

from knowledge_mining.mining.contracts.storage.types import ObjectLocation
from knowledge_mining.mining.infra.object_store.config import ObjectStoreConfig
from knowledge_mining.mining.infra.object_store.minio import MinioObjectStore

pytestmark = pytest.mark.asyncio

LOC = ObjectLocation(bucket="agentickb-dev-staging", object_key="export/kb1/t1.zip")


def _store(public_base: str = "") -> MinioObjectStore:
    return MinioObjectStore(ObjectStoreConfig(
        provider="minio", bucket_prefix="agentickb-dev-",
        endpoint="minio:9000", access_key="ak", secret_key="sk",
        region="us-east-1", presign_public_base=public_base,
    ))


async def test_presign_get_expires_reaches_url():
    access = await _store().presign_get(LOC, expires_in_seconds=86400)
    assert "X-Amz-Expires=86400" in access.url
    assert access.url.startswith("http://minio:9000/")  # 未配置改写 → 原样


async def test_presign_get_expires_not_sdk_default():
    """漏传 expires 会得到 SDK 默认 604800——参数必须显式生效。"""
    access = await _store().presign_get(LOC, expires_in_seconds=3600)
    assert "X-Amz-Expires=3600" in access.url
    assert "X-Amz-Expires=604800" not in access.url


async def test_presign_get_filename_disposition():
    access = await _store().presign_get(
        LOC, expires_in_seconds=900, filename="产品库-导出.zip",
    )
    assert "response-content-disposition" in access.url


async def test_presign_put_expires_reaches_url():
    access = await _store().presign_put(LOC, expires_in_seconds=1800)
    assert "X-Amz-Expires=1800" in access.url


async def test_presign_public_base_rewrites_prefix_only():
    """改写只换 scheme://endpoint 前缀；签名与查询串保持逐字节一致。"""
    internal = (await _store().presign_get(LOC, expires_in_seconds=86400)).url
    public = (await _store(
        public_base="http://39.96.13.112:8080/object-store",
    ).presign_get(LOC, expires_in_seconds=86400)).url

    assert public.startswith("http://39.96.13.112:8080/object-store/agentickb-dev-staging/")
    assert public.split("?", 1)[1] == internal.split("?", 1)[1]
    assert "X-Amz-Signature=" in public


async def test_presign_public_base_ignores_foreign_prefix():
    """防御分支：URL 前缀与 endpoint 不一致（如 SDK 虚拟主机风格改写）时
    原样返回，不做破坏性替换。真 SDK 无法构造该分支——prefix 由
    secure+endpoint 拼出，与 SDK 实际产物恒一致——直接测纯函数。"""
    from knowledge_mining.mining.infra.object_store.minio import _presign_delivery_url

    config = ObjectStoreConfig(
        provider="minio", bucket_prefix="agentickb-dev-",
        endpoint="minio:9000", access_key="ak", secret_key="sk",
        region="us-east-1", presign_public_base="http://example.com/object-store",
    )
    foreign = "https://bucket.vhost.example.com/agentickb-dev-staging/k?X-Amz-Signature=s"
    assert _presign_delivery_url(config, foreign) == foreign
