"""
utils/http_client.py — 按上游 base_url 缓存的共享 httpx.AsyncClient

背景：转发热路径原先每个请求都新建 AsyncClient，每次都要重做 TLS 握手 +
TCP 建连，且连接数无上限。改为按 base_url 缓存长连接 client：
  - 同一上游复用连接池（keep-alive），省掉握手，降低首字节延迟
  - 不同上游各自隔离，互不影响
  - 设置连接池上限，避免并发下连接爆炸

同一进程只加载一个 env，因此 verify / trust_env 在进程内固定，可安全共享。
缓存 key 只需 base_url（外加 verify，以防同进程出现混用的极端情况）。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Dict, Tuple

import httpx

# 默认超时：读取给足（等 LLM 生成），连接快速失败进重试
DEFAULT_TIMEOUT = httpx.Timeout(500.0, connect=10.0)
DEFAULT_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=30.0,
)

# key: (base_url, verify) -> AsyncClient
_clients: Dict[Tuple[str, bool], httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def _make_client(verify: bool, trust_env: bool) -> httpx.AsyncClient:
    """按连接池和超时配置创建上游客户端，禁用跨请求保存 Cookie。"""
    return httpx.AsyncClient(
        verify=verify,
        trust_env=trust_env,
        timeout=DEFAULT_TIMEOUT,
        limits=DEFAULT_LIMITS,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    )


async def get_client(base_url: str, verify: bool, trust_env: bool) -> httpx.AsyncClient:
    """返回该 base_url 对应的共享 client，不存在则创建并缓存。

    base_url 仅用作缓存 key，请求仍传完整 URL（保持调用点原有写法不变）。
    """
    key = (base_url, verify)
    client = _clients.get(key)
    if client is not None and not client.is_closed:
        return client
    async with _lock:
        # double-check：可能在等锁期间已被其它协程创建
        client = _clients.get(key)
        if client is not None and not client.is_closed:
            return client
        client = _make_client(verify, trust_env)
        _clients[key] = client
        logging.info("http_client: created shared client for base_url=%s (verify=%s)", base_url, verify)
        return client


@asynccontextmanager
async def shared_client(base_url: str, verify: bool, trust_env: bool):
    """`async with shared_client(...) as client:` 的封装。

    返回缓存的共享 client，且 **退出时不关闭**（连接池要留给后续请求复用）。
    这样调用点只需把 `httpx.AsyncClient(...)` 换成 `shared_client(...)`，其余
    代码（重试循环、client.post / client.stream）完全不变。
    """
    client = await get_client(base_url, verify, trust_env)
    yield client


async def close_all() -> None:
    """关闭所有缓存的 client（进程退出时调用）。"""
    clients = list(_clients.values())
    _clients.clear()
    for c in clients:
        try:
            await c.aclose()
        except Exception as ex:
            logging.warning("http_client: error closing client: %s", ex)
