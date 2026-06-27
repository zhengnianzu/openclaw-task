"""
Hermes Agent 进程内客户端封装 (Python 库模式)

本项目此前用 HTTP 短连接调 ``hermes gateway`` 的 API server,本版本改为
**直接 import** hermes-agent 的 ``AIAgent`` 类,在同一个 Python 进程里跑
agent。优点:

  * 不再需要拉起 gateway 子进程、不需要端口/Bearer token/健康探活
  * 不再有"long run -> HTTP 超时 -> 从 /messages 兜底拉历史"这一坨补丁
  * 调用栈 / 异常 / 多 agent 之间天然清晰

公开 API (调用方拿到的对象,签名兼容老版本):

  HermesClient                       — async ctx mgr, 持有 (agent, session) -> AIAgent 池
  client.get_agent(name, session)    → HermesAgent
  HermesAgent.execute(query, opts)   → ExecutionResult
  ExecutionResult                    — dataclass(success/content/stop_reason/...)
  ExecutionOptions(timeout_seconds=) — 单轮超时
  HermesError                        — 通用错误类型 (旧名 HermesGatewayError 已 alias)
  build_hermes_client()              — 工厂

多 agent 隔离策略 (本次重写):
  * **不**为每个 agent 起独立 profile / gateway (强隔离已删)
  * 同一进程里, ``(agent_name, session_name)`` 作为联合键 ——
    每个键持有一个独立的 ``AIAgent`` 实例 + 独立的 conversation_history
  * 所有 AIAgent 共享 ``~/.hermes/config.yaml`` 的 model / provider / api_key 设置
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime import — see _import_AIAgent for the real load
    from run_agent import AIAgent as _AIAgent  # noqa: F401

logger = logging.getLogger("hermes_automation")


# ---------------------------------------------------------------------------
# AIAgent 延迟加载
# ---------------------------------------------------------------------------
#
# 历史上这里有一段 ``sys.modules.pop("utils")`` 的 hack —— 因为本项目最初
# 也叫 ``utils/`` 包,跟 hermes-agent 顶层 ``utils.py`` 同名,直接 import
# run_agent 会触发 hermes-agent 内部的 ``from utils import ...`` 解析到
# 我们的包,然后 ImportError。
#
# 现在本文件已经搬到独立包 ``hermes_utils/`` 下,命名空间不再跟 hermes-agent
# 冲突,那段 hack 已被删除 —— 只剩一次普通的 lazy import + sys.path 注入。
# ---------------------------------------------------------------------------

_HERMES_AGENT_ROOT_ENV = "HERMES_AGENT_ROOT"
_HERMES_AGENT_ROOT_DEFAULT = "/home/ma-user/.hermes/hermes-agent"
_AIAgent_cls = None  # type: ignore[assignment]


def _hermes_agent_root() -> str:
    """允许通过环境变量覆盖 hermes-agent 源码路径,默认 /home/ma-user/.hermes/hermes-agent。"""
    return os.environ.get(_HERMES_AGENT_ROOT_ENV, _HERMES_AGENT_ROOT_DEFAULT)


def _import_AIAgent():
    """Return the AIAgent class, importing it on first call.

    Idempotent — subsequent calls return the cached class.
    """
    global _AIAgent_cls
    if _AIAgent_cls is not None:
        return _AIAgent_cls

    hermes_path = _hermes_agent_root()
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)

    try:
        run_agent_mod = importlib.import_module("run_agent")
    except Exception as e:
        raise HermesError(
            f"无法 import hermes-agent.run_agent (检查 {hermes_path} 是否存在,"
            f"以及 pip 依赖是否齐全): {e}"
        ) from e

    _AIAgent_cls = getattr(run_agent_mod, "AIAgent")
    logger.debug("AIAgent imported from %s", hermes_path)
    return _AIAgent_cls


# ============================================================================
# 异常类型
# ============================================================================

class HermesError(RuntimeError):
    """Hermes agent 调用失败。"""


# 兼容旧调用方代码里的 except HermesGatewayError
HermesGatewayError = HermesError


# ============================================================================
# config.yaml 读取 — 为 AIAgent(...) 准备显式 kwargs
# ============================================================================

def _hermes_home_path() -> Path:
    """当前生效的 ``HERMES_HOME``  (优先看 env, 默认 ~/.hermes)。"""
    home = os.environ.get("HERMES_HOME")
    return Path(home).expanduser() if home else Path.home() / ".hermes"


def _global_hermes_home() -> Path:
    """**不**受当前 ``HERMES_HOME`` 覆盖影响的全局家目录 (~/.hermes)。

    Per-agent HERMES_HOME 切换的时候,我们仍然要从 global 那份读
    ``config.yaml`` (模型/provider/api_key 全局共享, 见 README 的"软隔离"约定)。
    """
    return Path.home() / ".hermes"


def _load_aiagent_kwargs_from_config(
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """从 ~/.hermes/config.yaml 抽取 ``model`` 段, 拼成 AIAgent(...) 的 kwargs。

    AIAgent.__init__ 形参里 ``model`` 默认是空字符串,且**不会主动**去读
    ``config.yaml``;那段配置只有走 hermes CLI 时才被解析后传进来。库
    模式下,我们必须自己读、自己显式喂,否则 ``agent.model = ""``,
    OpenAI 兼容请求体里 ``"model": ""`` 会被网关一律 503。

    支持两种风格:
      A) 扁平 (``model.default`` + ``model.provider`` + ``model.base_url`` + ``model.api_key``)
      B) custom_providers (``model.provider = "custom:NAME"`` + ``custom_providers.NAME.{...}``)
    """
    cfg_path = config_path or (_global_hermes_home() / "config.yaml")
    if not cfg_path.is_file():
        logger.warning(
            "config.yaml 不存在: %s — AIAgent 将以全空参数初始化, "
            "请求大概率会 503", cfg_path,
        )
        return {}

    try:
        import yaml  # PyYAML
    except ImportError as e:
        raise HermesError(
            "需要 PyYAML 才能读 ~/.hermes/config.yaml: pip install pyyaml"
        ) from e

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise HermesError(f"读 config.yaml 失败 ({cfg_path}): {e}") from e

    model_section = data.get("model") or {}
    if not isinstance(model_section, dict):
        return {}

    # 模型名: 同时支持 ``model: ...`` 和 ``default: ...`` 两种写法。
    model_name = (
        model_section.get("model")
        or model_section.get("default")
        or ""
    )
    provider = model_section.get("provider") or None
    base_url = model_section.get("base_url") or None
    api_key = model_section.get("api_key") or None

    # custom_providers 风格: provider="custom:foo" 取 custom_providers.foo
    if isinstance(provider, str) and provider.startswith("custom:"):
        pname = provider.split(":", 1)[1].strip()
        cps = data.get("custom_providers") or {}
        cp = cps.get(pname) if isinstance(cps, dict) else None
        if isinstance(cp, dict):
            base_url = base_url or cp.get("base_url")
            api_key = api_key or cp.get("api_key")
            if not model_name:
                ms = cp.get("models")
                if isinstance(ms, list) and ms:
                    model_name = ms[0]

    out: Dict[str, Any] = {}
    if model_name:
        out["model"] = model_name
    if provider:
        out["provider"] = provider
    if base_url:
        out["base_url"] = base_url
    if api_key:
        out["api_key"] = api_key
    return out


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ExecutionResult:
    """单轮 agent 调用的结果。"""

    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


# ============================================================================
# HermesAgent — 一个 (agent_name, session_name) 句柄,内含独立 AIAgent 实例
# ============================================================================

class HermesAgent:
    """对应 openclaw_sdk 的 agent 概念。

    每个 HermesAgent 实例**独占一个 AIAgent**,持有自己的 conversation_history,
    所以多 agent / 多 session 之间天然隔离 (即"软隔离" —— 共享 ~/.hermes
    底下的 skills/memories/SOUL.md,但运行时 history 不互相污染)。

    AIAgent.run_conversation 是同步阻塞 API,我们用 ``asyncio.to_thread``
    包成 async,跟旧 HTTP 客户端保持同样的 await 接口。
    """

    def __init__(
        self,
        client: "HermesClient",
        agent_name: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        hermes_home: Optional[Path] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        # 旧代码里有把 session_id / session_key 当展示用的地方,保留 alias 避免破坏
        self.session_id = session_name
        self.session_key = session_name
        self._system_prompt = system_prompt
        # Per-agent HERMES_HOME: ``None`` 表示用全局 (~/.hermes); 否则在每次
        # ``execute()`` 期间临时把 env + ContextVar override 切到这个目录,
        # AIAgent 内部的 ``get_hermes_home()`` 就会读到这个 agent 自己的
        # SOUL.md / memories/ / skills/ 。
        self.hermes_home: Optional[Path] = Path(hermes_home).expanduser() if hermes_home else None
        self._agent: Optional[Any] = None  # AIAgent instance, lazily created
        # conversation_history 由我们维护 (list[{role, content}]),每次 execute
        # 把它传给 run_conversation,再把新一轮 user/assistant 追加进去。
        # 这是隔离不同 session 的关键:每个 (agent, session) 一份 history。
        self._history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 内部: HERMES_HOME 切换 (env + ContextVar override 双管齐下)
    # ------------------------------------------------------------------

    def _enter_hermes_home(self):
        """临时把进程的 HERMES_HOME 指向本 agent 的家目录,返回还原句柄。

        - 同时设置 ``os.environ['HERMES_HOME']`` (兼容子进程 / 旧代码路径)
          和 hermes_constants.set_hermes_home_override (ContextVar, 线程/asyncio 友好)。
        - 调用方拿到的 token 用于 ``_exit_hermes_home(token)`` 恢复原状。

        ``self.hermes_home is None`` 时是 no-op,直接返回 None。
        """
        if self.hermes_home is None:
            return None
        # 1) ContextVar override (hermes-agent 内部 get_hermes_home 优先看它)
        token = None
        try:
            sys.path.insert(0, _hermes_agent_root())  # 确保 hermes_constants 可 import
            from hermes_constants import set_hermes_home_override  # type: ignore
            token = set_hermes_home_override(self.hermes_home)
        except Exception as e:
            logger.debug("set_hermes_home_override 不可用 (忽略): %s", e)
        # 2) os.environ (兜底, 兼容子进程 / 没用 override 的代码路径)
        prev_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        return (token, prev_env)

    def _exit_hermes_home(self, handle) -> None:
        if handle is None:
            return
        token, prev_env = handle
        if token is not None:
            try:
                from hermes_constants import reset_hermes_home_override  # type: ignore
                reset_hermes_home_override(token)
            except Exception as e:
                logger.debug("reset_hermes_home_override 失败 (忽略): %s", e)
        if prev_env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_env

    # ------------------------------------------------------------------
    # 内部: 惰性建 AIAgent
    # ------------------------------------------------------------------

    def _ensure_agent(self):
        if self._agent is not None:
            return self._agent
        AIAgent = _import_AIAgent()
        # AIAgent.__init__ 不会主动读 ~/.hermes/config.yaml 的 model 段 ——
        # 那段配置只有 hermes CLI 入口处解析后传进来。库模式下我们必须自己
        # 显式喂给构造函数,否则 agent.model 是空字符串、请求体里 "model": ""
        # 会被网关一律 503。
        # 注意: model/provider/api_key 总是从 **全局** ~/.hermes/config.yaml 读,
        # 跟 per-agent HERMES_HOME 解耦 —— per-agent 家目录是为了让 SOUL.md /
        # memories/ / skills/ 隔离,模型本身全局共享(软隔离精神)。
        ctor_kwargs = _load_aiagent_kwargs_from_config()
        try:
            self._agent = AIAgent(**ctor_kwargs)
        except Exception as e:
            raise HermesError(
                f"AIAgent 初始化失败 (检查 ~/.hermes/config.yaml 的 model 段): {e}"
            ) from e
        logger.debug(
            "AIAgent created for agent=%s session=%s hermes_home=%s "
            "(model=%r provider=%r base_url=%r)",
            self.agent_name, self.session_name, self.hermes_home,
            ctor_kwargs.get("model"), ctor_kwargs.get("provider"),
            ctor_kwargs.get("base_url"),
        )
        return self._agent

    # ------------------------------------------------------------------
    # 公开: 单轮 execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        """发一轮对话。等待 AIAgent 跑完 tool-calling loop,返回最终文本。

        options.timeout_seconds 当作整体超时;超时返回 success=False 而不是抛错,
        跟旧 HTTP 版本保持一致。
        """
        # ★ 在切到本 agent 的 HERMES_HOME 之后再 ensure_agent / 跑 ——
        # 这样首次构造时 AIAgent.__init__ 内部任何读 get_hermes_home() 的代码
        # (skills 扫描、SOUL.md/MEMORY.md 加载) 都能拿到正确的 per-agent 家目录。
        handle = self._enter_hermes_home()
        try:
            agent = self._ensure_agent()

            def _call() -> Dict[str, Any]:
                return agent.run_conversation(
                    user_message=query,
                    system_message=self._system_prompt,
                    conversation_history=self._history,
                )

            timeout = (
                float(options.timeout_seconds)
                if options and options.timeout_seconds
                else None
            )

            try:
                if timeout is not None:
                    result_dict = await asyncio.wait_for(
                        asyncio.to_thread(_call), timeout=timeout
                    )
                else:
                    result_dict = await asyncio.to_thread(_call)
            except asyncio.TimeoutError:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="timeout",
                    error_message=f"AIAgent run timed out after {timeout}s",
                )
            except Exception as e:
                logger.exception(
                    "AIAgent.run_conversation 异常 (agent=%s session=%s)",
                    self.agent_name, self.session_name,
                )
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=str(e),
                )

            if not isinstance(result_dict, dict):
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=f"AIAgent returned non-dict: {type(result_dict).__name__}",
                )

            final_text = result_dict.get("final_response", "") or ""

            # 把这一轮追加到本地 history,下次 execute 会带上,从而实现多轮上下文。
            # 注意:不带 tool messages,只追加最终的 user / assistant 文本对。
            if final_text:
                self._history.append({"role": "user", "content": query})
                self._history.append({"role": "assistant", "content": final_text})

            return ExecutionResult(
                success=True,
                content=final_text,
                stop_reason=result_dict.get("stop_reason") or "complete",
                usage=result_dict.get("usage"),
            )
        finally:
            self._exit_hermes_home(handle)


# ============================================================================
# HermesClient — 主入口, async context manager
# ============================================================================

class HermesClient:
    """进程内 Hermes 客户端。

    使用模式::

        async with await build_hermes_client() as client:
            agent = client.get_agent("paper_reader", "session_abc")
            result = await agent.execute("Hello")

    同一个 (agent_name, session_name) 复用同一个 AIAgent + history;
    不同 (agent_name, session_name) 持有各自独立的 AIAgent。
    """

    def __init__(self):
        # 缓存已创建过的 agent,避免同 (name, session) 多次新建 AIAgent。
        self._agents: Dict[tuple, HermesAgent] = {}

    # --------------- async ctx mgr ---------------

    async def __aenter__(self) -> "HermesClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        for ag in self._agents.values():
            if ag._agent is not None:
                try:
                    # AIAgent.close 是同步的,丢到线程池避免阻塞 event loop
                    await asyncio.to_thread(ag._agent.close)
                except Exception as e:
                    logger.debug("AIAgent.close 异常 (忽略): %s", e)
        self._agents.clear()

    # --------------- 工厂 ---------------

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        system_prompt: Optional[str] = None,
        hermes_home: Optional[Path] = None,
    ) -> HermesAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = HermesAgent(
                client=self,
                agent_name=agent_name,
                session_name=session_name,
                system_prompt=system_prompt,
                hermes_home=hermes_home,
            )
        # 同 (name, session) 复用同一个 AIAgent + history;system_prompt /
        # hermes_home 以第一次为准 (跟 OpenClaw 的语义保持一致)。
        return self._agents[key]


# ============================================================================
# 工厂函数 — 与旧版同签 (旧的 api_base/api_key/timeout/per_agent_api_base 参数
# 都已删除,因为现在是进程内调用,不再有这些概念)
# ============================================================================

async def build_hermes_client(**_ignored_legacy_kwargs: Any) -> HermesClient:
    """构造 HermesClient。

    模型 / provider / api_key / base_url 全部由 ``~/.hermes/config.yaml``
    决定,本函数不接受任何后端连接参数。

    为了不破坏既有调用方,仍然 **接受并丢弃** 一切旧的关键字参数
    (如 ``api_base`` / ``api_key`` / ``timeout`` / ``per_agent_api_base``)。
    """
    if _ignored_legacy_kwargs:
        logger.debug(
            "build_hermes_client: 忽略以下旧的 HTTP 参数: %s",
            sorted(_ignored_legacy_kwargs.keys()),
        )
    client = HermesClient()
    logger.info(
        "Hermes 客户端 (进程内 AIAgent 模式) 就绪;模型与 provider 由 "
        "~/.hermes/config.yaml 决定。"
    )
    return client


# ============================================================================
# 通用重试装饰器 — 兼容性保留 (旧 hermes_automation.py 里 import 过)
# ============================================================================

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.5


async def with_backoff_retry(
    coro_fn,
    *args,
    max_attempts: int = 5,
    initial_delay: float = _BACKOFF_INITIAL,
    max_delay: float = _BACKOFF_MAX,
    exceptions: tuple = (HermesError, asyncio.TimeoutError),
    **kwargs,
):
    """对 async 函数加指数退避重试 (API 兼容性 stub)。"""
    delay = initial_delay
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except exceptions as e:
            last_exc = e
            if attempt >= max_attempts:
                raise
            jitter = random.uniform(-_BACKOFF_JITTER * delay, _BACKOFF_JITTER * delay)
            wait = min(delay + jitter, max_delay)
            logger.warning(
                "调用失败 (第 %d/%d 次): %s; %.1fs 后重试",
                attempt, max_attempts, e, wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, max_delay)
    if last_exc is not None:
        raise last_exc
