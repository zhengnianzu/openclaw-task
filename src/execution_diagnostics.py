"""把本次执行的代理和 OpenClaw 会话错误提取到主日志，不输出请求正文。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path


logger = logging.getLogger("harness_automation")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def _tool_error(message):
    details = message.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    text = _text(message.get("content"))
    # 命令退出码为 0 也可能返回 API 错误 JSON，例如 curl 得到 401。
    status = None
    for line in text.splitlines():
        match = re.search(r"\b(4\d\d|5\d\d)\b", line)
        nearby_status = match[1] if match else status
        status = match[1] if match else None
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("error"):
            error = value["error"]
            reason = str(error.get("message") or error) if isinstance(error, dict) else str(error)
            return f"HTTP {nearby_status} {reason}" if nearby_status else reason
    if message.get("isError") or details.get("status") in ("error", "failed") or details.get("exitCode") not in (None, 0):
        reason = str(details.get("error") or text or "工具返回失败")
        # 保留异常末行，不把堆栈或抓取内容的安全提示当成错误摘要。
        errors = re.findall(r"(?m)^.*(?:\b\w*Error:|\b\w*Exception:|Bad substitution).*$", reason)
        if errors:
            return errors[-1]
        return reason.split("SECURITY NOTICE:", 1)[0].strip()
    return ""


class ExecutionDiagnostics:
    def __init__(self, bridge, agent, query, project_dir=None, state_dir=None):
        self.bridge = bridge
        self.query = str(query).strip()
        self.root = Path(project_dir or Path.cwd()) / "logs_proxy" / "traj"
        self.index = self.root / "index.jsonl"
        self.offsets = {}
        self.issues = {}
        self.tools = {}
        self.task = None
        self.session_key = str(getattr(agent, "session_key", "")).lower()
        agent_name = str(getattr(agent, "agent_name", bridge.agent))
        state = Path(state_dir or os.getenv("OPENCLAW_STATE_DIR") or
                     Path(os.getenv("OPENCLAW_CONFIG_PATH", str(Path.home() / ".openclaw" / "openclaw.json"))).parent).expanduser()
        self.sessions = state / "agents" / Path(agent_name).name / "sessions"
        self._skip_existing(self.index)
        session = self._session_path()
        if session:
            self._skip_existing(session)

    def _skip_existing(self, path):
        try:
            self.offsets[path] = path.stat().st_size
        except OSError:
            pass

    def _session_path(self):
        if self.bridge.harness != "openclaw" or not self.session_key:
            return None
        try:
            sessions = json.loads((self.sessions / "sessions.json").read_text(encoding="utf-8"))
            entry = next((v for k, v in sessions.items() if k.lower() == self.session_key), None)
            if entry and entry.get("sessionId"):
                return self.sessions / (Path(str(entry["sessionId"])).name + ".jsonl")
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _rows(self, path):
        try:
            with path.open("rb") as stream:
                offset = self.offsets.get(path, 0)
                stream.seek(0 if path.stat().st_size < offset else offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.offsets[path] = stream.tell()
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            return

    def _report(self, kind, detail, source):
        detail = " ".join(self.bridge.redact(detail).split())[:450]
        key = kind, detail
        self.issues[key] = self.issues.get(key, 0) + 1
        if self.issues[key] == 1:
            logger.warning("%s：%s；证据=%s", kind, detail, source)

    def poll(self):
        for index in self._rows(self.index):
            if index.get("request_kind", "model") != "model":
                continue
            if index.get("success") is not False and index.get("total_attempts", 1) <= 1:
                continue
            try:
                path = (self.root / index["file"]).resolve()
                if not path.is_relative_to(self.root.resolve()):
                    continue
                with path.open("rb") as stream:
                    stream.seek(index["offset"])
                    trace = json.loads(stream.read(index["length"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            # 共享代理也接收 evaluator 等请求，仅归集包含本次用户输入的请求。
            request = trace.get("req") or {}
            messages = request.get("messages", []) if isinstance(request, dict) else []
            if not self.query or not any(isinstance(m, dict) and m.get("role") == "user" and
                                         self.query in _text(m.get("content")) for m in messages):
                continue
            source = f"{path} request={index.get('id', '')}"
            response = trace.get("res") or {}
            if index.get("success"):
                logger.info("模型调用重试后恢复：尝试=%s；证据=%s", index.get("total_attempts"), source)
                continue
            payload = response.get("json") or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if not error:
                for chunk in response.get("chunks") or []:
                    if isinstance(chunk, dict) and chunk.get("error"):
                        error = chunk["error"]
                        break
            error = error if isinstance(error, dict) else {"message": error or response.get("error") or "未返回完整响应"}
            detail = (f"上游HTTP={response.get('upstream_status_code', response.get('status_code'))} "
                      f"客户端HTTP={response.get('status_code')} 错误码={error.get('code') or error.get('type') or '未知'} "
                      f"原因={error.get('message', '')}")
            self._report("模型调用失败", detail, source)
        session = self._session_path()
        if session:
            for row in self._rows(session):
                message = row.get("message") or {}
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "assistant":
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "toolCall":
                            arguments = json.dumps(block.get("arguments", {}), ensure_ascii=False)
                            context = " 服务=serper" if "serper" in arguments.lower() else ""
                            if "SERPER_API_KEY" in arguments:
                                context += " 凭据变量=SERPER_API_KEY"
                            self.tools[block.get("id")] = context
                    continue
                if message.get("role") not in ("toolResult", "tool"):
                    continue
                error = _tool_error(message)
                if error:
                    hint = "（接口拒绝凭据，请检查有效期和访问权限）" if "invalid token" in error.lower() else ""
                    context = self.tools.pop(message.get("toolCallId"), "")
                    detail = f"tool={message.get('toolName') or message.get('name') or '未知'}{context} 原因={error}{hint}"
                    self._report("工具调用失败", detail, f"{session} message={row.get('id', '')}")
                else:
                    self.tools.pop(message.get("toolCallId"), None)

    def collect(self):
        try:
            self.poll()
        except Exception as error:
            # 诊断失败不得改变任务返回值，也不得覆盖原始异常。
            self.bridge.native("错误摘要采集异常", type(error).__name__)

    async def _watch(self):
        while True:
            self.collect()
            await asyncio.sleep(2)

    def start(self):
        self.task = asyncio.create_task(self._watch())

    async def finish(self, success):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.collect()
        if self.issues:
            models = [item for item in self.issues.items() if item[0][0] == "模型调用失败"]
            tools = [item for item in self.issues.items() if item[0][0] == "工具调用失败"]
            summary = "；".join(f"{kind}：{detail}（{count}次）" for (kind, detail), count in models[-2:] + tools[-3:])
            logger.log(logging.INFO if success else logging.ERROR, "%s：%s",
                       "执行已返回有效结果，期间的问题" if success else "执行失败诊断", summary)
