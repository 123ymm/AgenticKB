"""Wiki 制品 web 检索转发客户端（63号批次4 Task 4）。

网页阅读器搜索框 → mining 转发端点（routes/wiki.py GET /wiki/search）→
本客户端 POST serving ``/api/internal/artifact-wiki-search``（三通道检索
dense ANN/keyword/fusion，业务与权限谓词整段在 Java
ArtifactWikiSearchService——mining 侧零检索逻辑，纯转发）。

同容器 supervisor 直连（与 mcp_server/tools.py 的 _post_serving 同模式）：
- URL：env ``SERVING_INTERNAL_URL``（缺省 http://127.0.0.1:8081——compose 单
  app 服务，serving/mining/mcp 同容器容器级共享 env）；
- 鉴权：X-Internal-Auth = env ``SERVING_INTERNAL_AUTH_SECRET``；本侧未配 →
  本地即拦 serving_unavailable（与 serving 侧 secret 未配 503 同口径，
  不空跑网络）。

错误翻译（serving HTTP 状态 → WikiError.code，路由层 _map_wiki_error 再翻
HTTP 状态 + 中文文案）：
- 404 → artifact_not_found（serving 反探测口径透传——rev0 不可见/跨域等与
  不存在同响应；mining 侧 _assert_role 已拦非成员，此处兜底两侧成员模型
  差异，如未发布制品对 serving 不可见）；
- 400 → invalid_query（q 空白/mode 非法）；503 → serving_unavailable；
- 其余 ≥400（含 401 密钥失配/5xx）→ serving_error；
- httpx.TimeoutException → serving_timeout；其余网络异常（连接拒绝等）
  → serving_unavailable；200 非 JSON 体 → serving_error。

web 会话用户按普通成员走 serving 成员谓词（is_admin=False / key_id=None
——serving 侧统一 404 反探测，任务定稿口径）。模块级单例
（get_client——httpx.AsyncClient 连接池跨请求复用，避免每请求新建泄漏）；
测试以 transport= 注入 httpx.MockTransport 桩 serving，不碰网络。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from knowledge_mining.mining.kb.db_wiki import WikiError

logger = logging.getLogger(__name__)

#: 同容器 supervisor 直连缺省（compose：serving 与 mining 同 app 容器）。
DEFAULT_SERVING_INTERNAL_URL = "http://127.0.0.1:8081"

#: wiki 检索 internal 端点（批次4 Task 2 定稿契约）。
SEARCH_PATH = "/api/internal/artifact-wiki-search"


class WikiSearchClient:
    """serving wiki 三通道检索转发（异步薄客户端——单次 POST，无状态）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # env 只在构造时读取（单例一次成型；测试显式传参覆盖、不读环境）。
        self._base_url = (
            base_url if base_url is not None else
            (os.environ.get("SERVING_INTERNAL_URL", "").strip()
             or DEFAULT_SERVING_INTERNAL_URL)
        ).rstrip("/")
        self._secret = (
            secret if secret is not None
            else os.environ.get("SERVING_INTERNAL_AUTH_SECRET", "")
        ).strip()
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        """惰性建共享 AsyncClient（进程单事件循环；关闭后可重建）。"""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                trust_env=False,   # 同容器直连不走代理 env（对齐 mcp 先例）
                transport=self._transport,
            )
        return self._http

    async def search(
        self,
        artifact_ref: str,
        q: str,
        mode: str | None,
        top_k: int | None,
        domain: str,
        username: str,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """转发三通道检索：成功 → serving 响应 dict 原样透传；失败 → WikiError。"""
        if not self._secret:
            logger.error("SERVING_INTERNAL_AUTH_SECRET 未配置——wiki web 检索不可用")
            raise WikiError("serving_unavailable")
        payload: dict[str, Any] = {
            "artifact_ref": artifact_ref,
            "q": q,
            "domain": domain,
            "username": username,
            "is_admin": False,
            "key_id": None,
        }
        if mode is not None:
            payload["mode"] = mode
        if top_k is not None:
            payload["top_k"] = top_k
        try:
            resp = await self._client().post(
                SEARCH_PATH,
                json=payload,
                headers={"X-Internal-Auth": self._secret},
                timeout=timeout,
            )
        except httpx.TimeoutException:
            logger.warning(
                "wiki-search 转发超时（%s %s）", self._base_url, artifact_ref)
            raise WikiError("serving_timeout") from None
        except httpx.HTTPError as exc:
            logger.warning(
                "wiki-search 转发不可达（%s）：%s", self._base_url, exc)
            raise WikiError("serving_unavailable") from None
        if resp.status_code == 404:
            raise WikiError("artifact_not_found")
        if resp.status_code == 400:
            raise WikiError("invalid_query")
        if resp.status_code == 503:
            raise WikiError("serving_unavailable")
        if resp.status_code >= 400:
            logger.warning("wiki-search 转发 HTTP %s：%s",
                           resp.status_code, resp.text[:160])
            raise WikiError("serving_error")
        try:
            return resp.json()
        except ValueError:
            logger.warning("wiki-search 转发响应非 JSON：%s", resp.text[:160])
            raise WikiError("serving_error") from None


#: 模块级单例（deps.get_wiki_search_client 取用；测试自建实例不走此入口）。
_SINGLETON: WikiSearchClient | None = None


def get_client() -> WikiSearchClient:
    """单例懒建（env 首建时读取；httpx.AsyncClient 连接池跨请求复用）。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = WikiSearchClient()
    return _SINGLETON
