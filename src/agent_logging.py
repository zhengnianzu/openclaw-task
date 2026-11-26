"""各 Agent 共用的执行摘要、工具事件和原生诊断日志。"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import sys
import traceback
from dataclasses import asdict, is_dataclass
import logging
import re
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4


logger = logging.getLogger("harness_automation")
_CURRENT = contextvars.ContextVar("agent_log_bridge", default=None)
_KNOWN_SECRETS: set[str] = set()
_EXECUTION_PREFIX = re.compile(
    r"^\[[^\]\r\n]+ agent=[^\]\r\n]+ session=[^\]\r\n]+ call=\d+\][ \t]*"
)


def _error_detail(result):
    if isinstance(result, str):
        for prefix in ("[error]", "[tool error]"):
            if result.startswith(prefix):
                return result[len(prefix):].strip() or "工具返回失败"
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None
    if not isinstance(result, dict):
        return None
    error = result.get("error") or result.get("error_message")
    if (error or result.get("success") is False or result.get("isError")
            or result.get("is_error") or result.get("status") in ("failed", "error", "cancelled")):
        return str(error or result.get("message") or "工具返回失败")
    exit_code = result.get("exit_code")
    if exit_code not in (None, 0):
        return f"退出码={exit_code} {result.get('output') or ''}"
    return None


def _value(source, key, default=None):
    return source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)


def _secrets(source, depth=0):
    if depth > 3:
        return set()
    values = source if isinstance(source, dict) else getattr(source, "__dict__", {})
    found = set()
    for key, value in values.items():
        if any(part in str(key).lower() for part in ("api_key", "token", "secret", "password", "authorization")):
            if isinstance(value, str) and len(value) > 6:
                found.add(value)
        if isinstance(value, dict) or hasattr(value, "api_key") or key in ("_model_override", "_defaults", "_client", "cfg", "_cfg", "_env"):
            found.update(_secrets(value, depth + 1))
    return found


def register_secrets(source):
    _KNOWN_SECRETS.update(_secrets(source))


def _redact(text, secrets):
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"\bsk-[\w-]{8,}", "[已隐藏]", text)
    return re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [已隐藏]", text)


class _ExecutionFilter(logging.Filter):
    def filter(self, record):
        bridge = _CURRENT.get()
        text = _EXECUTION_PREFIX.sub("", _redact(record.getMessage(), _KNOWN_SECRETS), count=1)
        if bridge is None:
            record.msg, record.args = text, ()
            if record.exc_info:
                detail = logging.Formatter().formatException(record.exc_info)
                detail = _redact(detail, _KNOWN_SECRETS)
                record.exc_text, record.exc_info = detail, None
            return True
        if record.exc_info:
            bridge.native("异常堆栈", text + "\n" + logging.Formatter().formatException(record.exc_info))
            record.exc_info = record.exc_text = None
        if record.levelno == logging.DEBUG:
            bridge.native("项目诊断", text)
            return False
        text = bridge.redact(text)
        record.msg = text
        record.args = ()
        return True


def install_log_filter():
    if not any(isinstance(item, _ExecutionFilter) for item in logger.filters):
        logger.addFilter(_ExecutionFilter())
    logger.propagate = False


def log_native(event, value):
    bridge = _CURRENT.get()
    if bridge is not None:
        bridge.native(event, value)


def log_tool_start(call_id, name, arguments):
    bridge = _CURRENT.get()
    if bridge is not None:
        bridge.tool_start(call_id, name, arguments)


def log_tool_complete(call_id, name, arguments, result):
    bridge = _CURRENT.get()
    if bridge is not None:
        bridge.tool_complete(call_id, name, arguments, result)


def log_tool_record(name, arguments, result):
    bridge = _CURRENT.get()
    if bridge is not None:
        bridge.tool_record({"tool": name, "input": arguments, "output": result})


def logged_execution(harness):
    """保留各接入的返回值和异常，仅统一执行边界的日志。"""
    def decorate(execute):
        @functools.wraps(execute)
        async def run(agent, query, options=None, *args, **kwargs):
            bridge = getattr(agent, "_execution_logs", None) or getattr(agent, "_logs", None)
            if not isinstance(bridge, AgentLogBridge):
                client = getattr(agent, "_client", None)
                kind = getattr(client, "harness_type", None) or harness
                bridge = AgentLogBridge(kind, getattr(agent, "agent_name", "main"),
                                        getattr(agent, "session_name", None) or getattr(agent, "session_key", "main"))
                try:
                    agent._execution_logs = bridge
                except (AttributeError, TypeError, ValueError):
                    pass
            bridge.secrets.update(_KNOWN_SECRETS | _secrets(agent) | _secrets(dict(os.environ)))
            install_log_filter()
            token = _CURRENT.set(bridge)
            started = time.monotonic()
            from src.execution_diagnostics import ExecutionDiagnostics
            diagnostics = ExecutionDiagnostics(bridge, agent, query)
            diagnostics.start()
            success = False
            try:
                bridge.start(getattr(options, "timeout_seconds", None))
                bridge.native("用户输入", query)
                package = {"openclaw": "openclaw_sdk", "claude-code": "claude_agent_sdk",
                           "codex": "openai_codex", "dsh": "deepseek_harness",
                           "openjiuwen": "openjiuwen"}.get(harness)
                sdk_file = getattr(sys.modules.get(package), "__file__", None)
                with ExitStack() as routes:
                    if sdk_file:
                        routes.enter_context(bridge.route_sdk_logging(Path(sdk_file).parent))
                    original = await execute(agent, query, options, *args, **kwargs)
                result = original[0] if isinstance(original, tuple) else original
                bridge.native("执行结果", result)
                bridge.result_tools(result)
                error = _value(result, "error_message") or _value(result, "error")
                success = bool(result is not None and _value(result, "success", True) and not error)
                success = success and _value(result, "stop_reason") not in ("error", "timeout", "failed", "aborted", "interrupted")
                bridge.finish(success, time.monotonic() - started, str(error) if error else None)
                return original
            except BaseException as error:
                bridge.native("异常堆栈", traceback.format_exc())
                reason = "执行被取消" if isinstance(error, asyncio.CancelledError) else str(error)
                bridge.finish(False, time.monotonic() - started, reason)
                raise
            finally:
                await diagnostics.finish(success)
                _CURRENT.reset(token)
                bridge.close()
        return run
    return decorate


class _SDKConsoleFilter(logging.Filter):
    def __init__(self, bridge, sdk_root: Path):
        super().__init__()
        self.bridge = bridge
        self.sdk_root = sdk_root.resolve()

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT.get() is not self.bridge:
            return True
        if not Path(record.pathname).resolve().is_relative_to(self.sdk_root):
            return True
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        self.bridge.native(f"SDK {record.levelname} {record.name}", text)
        return False


class AgentLogBridge:
    """使用实例级回调接收原生输出，不重定向进程共享的 stdout。"""

    def __init__(self, harness: str, agent: str, session: str, log_dir: Path | None = None):
        self.harness = harness
        self.agent = agent
        self.session = session
        self.call = 0
        self.secrets: set[str] = set()
        self._tools: dict[str, float] = {}
        self._completed_tools = 0
        self._failed_tools = 0
        self._unknown_tools = 0
        safe = lambda value: re.sub(r"[^\w-]", "_", value)[:80] or "default"
        root = log_dir or Path("logs") / "agents" / safe(harness)
        self.path = (root / safe(agent) / f"{safe(session)}.log").resolve()
        self._native = logging.getLogger(f"hermes_native.{uuid4().hex}")
        self._native.setLevel(logging.DEBUG)
        self._native.propagate = False
        self._handler: logging.FileHandler | None = None

    @property
    def prefix(self) -> str:
        return f"[{self.harness} agent={self.agent} session={self.session} call={self.call}]"

    def redact(self, text: str) -> str:
        return _redact(text, self.secrets)

    def native(self, event: str, value) -> None:
        if self._handler is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = logging.FileHandler(self.path, encoding="utf-8")
            self._handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._native.addHandler(self._handler)
        if is_dataclass(value):
            value = asdict(value)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        self._native.debug("%s %s %s", self.prefix, event, self.redact(text))

    def native_print(self, *args, sep=" ", end="\n", **kwargs) -> None:
        self.native("原生输出", sep.join(str(arg) for arg in args))

    @contextmanager
    def route_sdk_logging(self, sdk_root):
        """仅分流指定 SDK 源码产生的控制台记录，不改其他库或文件 handler。"""
        sdk_filter = _SDKConsoleFilter(self, Path(sdk_root))
        owners = [logging.getLogger(), *(owner for owner in logging.Logger.manager.loggerDict.values()
                                         if isinstance(owner, logging.Logger))]
        handlers = {handler for owner in owners for handler in owner.handlers
                    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)}
        token = _CURRENT.set(self) if _CURRENT.get() is None else None
        for handler in handlers:
            handler.addFilter(sdk_filter)
        try:
            yield
        finally:
            for handler in handlers:
                handler.removeFilter(sdk_filter)
            if token is not None:
                _CURRENT.reset(token)

    def start(self, timeout) -> None:
        self.call += 1
        self._tools.clear()
        self._completed_tools = 0
        self._failed_tools = 0
        self._unknown_tools = 0
        logger.info("%s 开始执行 timeout=%s秒", self.prefix, timeout or "未设置")
        self.native("调用开始", {"timeout": timeout})

    def tool_start(self, call_id, name, arguments, **kwargs) -> None:
        self._tools[str(call_id)] = time.monotonic()
        self.native("工具开始", {"id": call_id, "tool": name, "arguments": arguments})
        keys = ",".join(str(key) for key in arguments) if isinstance(arguments, dict) else "未知"
        logger.info("%s 工具开始 tool=%s 参数=%s", self.prefix, name, keys)

    def tool_complete(self, call_id, name, arguments, result, **kwargs) -> None:
        started = self._tools.pop(str(call_id), None)
        elapsed = time.monotonic() - started if started is not None else None
        self._completed_tools += 1
        self.native("工具结束", {"id": call_id, "tool": name, "result": result})
        error = _error_detail(result)
        if error:
            self._failed_tools += 1
            logger.warning("%s 工具失败 tool=%s 耗时=%s秒 原因=%s", self.prefix, name,
                           f"{elapsed:.2f}" if elapsed is not None else "未知",
                           " ".join(self.redact(str(error)).split())[:240])
            return
        # 工具失败允许 Agent 自行恢复，不将工具错误直接等同于整题失败。
        logger.info("%s 工具结束 tool=%s 耗时=%s秒", self.prefix, name,
                    f"{elapsed:.2f}" if elapsed is not None else "未知")

    def result_tools(self, result):
        """无实时事件的接入，在返回结果后汇总工具记录，避免重复计数。"""
        if self._completed_tools or self._unknown_tools or self._tools:
            return
        calls = _value(result, "tool_calls") or []
        if not calls:
            messages = _value(result, "messages") or []
            pending = {}
            for message in messages:
                for call in _value(message, "tool_calls", []) or []:
                    pending[_value(call, "id")] = _value(call, "function", {})
                if _value(message, "role") == "tool":
                    call = pending.get(_value(message, "tool_call_id"), {})
                    calls.append({"tool": _value(call, "name", "unknown"),
                                  "input": _value(call, "arguments"), "output": _value(message, "content")})
        for call in calls:
            self.tool_record(call)

    def tool_record(self, call):
        self.native("工具记录", call)
        name = _value(call, "tool") or _value(call, "name", "unknown")
        output = _value(call, "output")
        error = _error_detail(output)
        if output is None:
            self._unknown_tools += 1
        else:
            self._completed_tools += 1
        self._failed_tools += bool(error)
        duration = _value(call, "duration_ms")
        elapsed = f"{duration / 1000:.2f}" if isinstance(duration, (int, float)) else "未知"
        state = "失败" if error else ("无结果" if output is None else "成功")
        logger.log(logging.WARNING if error else logging.INFO,
                   "%s 工具记录 tool=%s 状态=%s 耗时=%s秒%s", self.prefix, name, state, elapsed,
                   f" 原因={' '.join(self.redact(error).split())[:240]}" if error else "")

    def status(self, kind, message, **kwargs) -> None:
        self.native("原生状态", {"kind": kind, "message": message})
        level = logging.WARNING if kind in ("warn", "warning", "error") else logging.INFO
        logger.log(level, "%s 状态=%s %s", self.prefix, kind,
                   " ".join(self.redact(str(message)).split())[:240])

    def finish(self, success: bool, elapsed: float, error: str | None = None) -> None:
        if success:
            logger.info("%s 执行完成 耗时=%.2f秒 工具完成=%d 工具失败=%d 工具无结果=%d",
                        self.prefix, elapsed, self._completed_tools, self._failed_tools, self._unknown_tools)
        else:
            self.native("执行失败", error or "未返回有效结果")
            logger.error("%s 执行失败 耗时=%.2f秒 原因=%s", self.prefix, elapsed,
                         " ".join(self.redact(error or "未返回有效结果").split())[:240])
        logger.info("%s 原生日志=%s", self.prefix, self.path)

    def close(self) -> None:
        if self._handler is not None:
            self._native.removeHandler(self._handler)
            self._handler.close()
            self._handler = None
