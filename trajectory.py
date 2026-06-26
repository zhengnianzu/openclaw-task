"""逐轮轨迹证据捕获(能力: trajectory-capture)。

在多轮对话执行中,为每个 turn 留存 agent 的可核验证据:
- `tool_calls`(内存直取,免费)
- 生成文件(以 `agents.files.get(被测 agentId)` 读取的**磁盘真相**为准,而非采信
  `ExecutionResult.files` 自报)
并标注证据完整性:经 `history_fallback` 兜底、只剩文本的 turn 标 `evidence_incomplete`。

本模块不依赖 openclaw_automation,避免循环导入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from openclaw_sdk.core.types import AgentFileContent, ExecutionResult

logger = logging.getLogger("openclaw_automation")

# OpenClaw 新建 agent 时铺设的脚手架文件;发现工作区新产物时排除这些,
# 以便把 agent 本轮真正"创建"的文件 surface 给 evaluator。
SCAFFOLDING_FILES = {
    "AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md", "USER.md",
    "HEARTBEAT.md", "BOOTSTRAP.md", "MEMORY.md", "CLAUDE.md",
}


# ============================================================================
# 证据 / 轨迹模型
# ============================================================================

class ToolCallEvidence(BaseModel):
    """一次工具调用的证据(含入参与返回值)。"""
    tool: str
    input: str = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


class FileEvidence(BaseModel):
    """一个被声称生成的文件,经磁盘真相校验后的证据。

    exists=True/False 表示 `agents.files.get` 在被测工作区是否真的取到该文件;
    checked=False 表示本轮未做磁盘核验(如 evaluator 未启用),仅记录声称的文件名。
    """
    name: str
    checked: bool = False
    exists: bool = False
    size: Optional[int] = None
    content: Optional[str] = None
    path: Optional[str] = None  # 产物在被测工作区的路径,供"指针投喂"(filename + workspace_path)
    error: Optional[str] = None  # 取证失败原因(如路径不可达)→ 降级,不当负面证据
    discovered: bool = False  # True=经工作区清点主动发现(非 agent 自报)


class TurnRecord(BaseModel):
    """单个 turn 的执行记录。"""
    turn: int
    user_input: str
    agent_content: str
    tool_calls: list[ToolCallEvidence] = Field(default_factory=list)
    files: list[FileEvidence] = Field(default_factory=list)
    stop_reason: Optional[str] = None
    evidence_incomplete: bool = False


class Trajectory(BaseModel):
    """一个 query 的完整运行记录。"""
    query: str
    agent_name: str
    turns: list[TurnRecord] = Field(default_factory=list)
    outcome: Optional[str] = None  # "done" | "failed" | "max_turn"

    def render_full(self, exclude_last: bool = False) -> str:
        """渲染历轮全文,供 evaluator 审阅(D4: 全文,不压缩摘要)。"""
        turns = self.turns[:-1] if exclude_last else self.turns
        if not turns:
            return "（暂无历史轮次）"
        return "\n\n".join(_render_turn(t) for t in turns)

    def render_recent(self, window: int) -> str:
        """渲染最近 window 轮的轨迹(压缩版:保留 tool_calls,文件仅给指针不内联全文)。
        建议window = eval_step, 正好覆盖两次评审之间的全部 turn,不留上下文空洞。
        """
        turns = self.turns[-window:] if window and window > 0 else self.turns
        if not turns:
            return "（暂无轮次）"
        return "\n\n".join(_render_turn_compact(t) for t in turns)

    def generated_file_pointers(self) -> list[dict]:
        """累积全部产物的指针 {filename, workspace_path}(去重按 name,后出现覆盖)。

        产物指针覆盖整段对话(非仅最近 X 轮)——避免 reset 后窗口外早期产物丢失;
        evaluator 据指针用自身工具打开核验,而非凭文件名臆断。
        """
        seen: dict[str, Optional[str]] = {}
        for t in self.turns:
            for f in t.files:
                if f.checked and f.exists:
                    seen[f.name] = f.path
        return [{"filename": n, "workspace_path": p} for n, p in seen.items()]


def _render_turn(t: TurnRecord) -> str:
    lines = [f"── Turn {t.turn} ──", f"[用户]: {t.user_input}", f"[Agent]: {t.agent_content}"]
    if t.tool_calls:
        lines.append("[工具调用]:")
        for tc in t.tool_calls:
            out = (tc.output or "")[:2000]
            lines.append(f"  - {tc.tool}(input={tc.input[:500]}) -> {out}")
    if t.files:
        lines.append("[文件证据(磁盘真相)]:")
        for f in t.files:
            src = "工作区发现" if f.discovered else "agent 自报"
            sz = f" size={f.size}" if f.size is not None else ""
            if not f.checked:
                lines.append(f"  - {f.name}: (未核验,仅声称)")
            elif f.exists and f.content is not None:
                preview = f.content[:2000]
                lines.append(f"  - {f.name}: 存在 ✓ [{src}{sz}]\n{preview}")
            elif f.exists:
                lines.append(f"  - {f.name}: 存在 ✓ [{src}{sz}](内容未取到,以存在性为准)")
            elif f.error:
                lines.append(f"  - {f.name}: 核验受阻({f.error})→ 证据缺失,不得当负面证据")
            else:
                lines.append(f"  - {f.name}: 声称生成,但磁盘上不存在 ✗")
    if t.stop_reason:
        lines.append(f"[stop_reason]: {t.stop_reason}")
    if t.evidence_incomplete:
        lines.append("[注意]: 本轮证据不完整(经 history 兜底恢复)——证据缺失 ≠ 证据为负")
    return "\n".join(lines)


def _render_turn_compact(t: TurnRecord) -> str:
    """压缩渲染单轮:保留 tool_calls(反幻觉底线),但文件**不内联全文**——
    产物以指针(由 `Trajectory.generated_file_pointers` 统一汇总)交给 evaluator 用工具自查。
    """
    lines = [f"── Turn {t.turn} ──", f"[用户]: {t.user_input}", f"[Agent]: {t.agent_content}"]
    if t.tool_calls:
        lines.append("[工具调用]:")
        for tc in t.tool_calls:
            out = (tc.output or "")[:800]
            lines.append(f"  - {tc.tool}(input={tc.input[:300]}) -> {out}")
    if t.files:
        # 仅列出本轮涉及的产物名(指针清单另行统一给出),不贴内容
        names = ", ".join(f.name for f in t.files)
        lines.append(f"[本轮产物]: {names}")
    if t.stop_reason:
        lines.append(f"[stop_reason]: {t.stop_reason}")
    if t.evidence_incomplete:
        lines.append("[注意]: 本轮证据不完整(经 history 兜底恢复)——证据缺失 ≠ 证据为负")
    return "\n".join(lines)


# ============================================================================
# 捕获辅助
# ============================================================================

def build_turn_record(
    turn: int,
    user_input: str,
    result: ExecutionResult,
    evidence_incomplete: bool,
) -> TurnRecord:
    """从 ExecutionResult 构建 TurnRecord(纯内存,不触网)。

    tool_calls 直取;files 仅记录声称的文件名(checked=False),磁盘真相由
    `capture_file_evidence` 在需要时补齐。
    """
    tool_calls = [
        ToolCallEvidence(
            tool=tc.tool,
            input=tc.input,
            output=tc.output,
            duration_ms=tc.duration_ms,
        )
        for tc in (result.tool_calls or [])
    ]
    files = [FileEvidence(name=(gf.name or gf.path or "")) for gf in (result.files or [])]  # gf = generated_file
    return TurnRecord(
        turn=turn,
        user_input=user_input,
        agent_content=result.content or "",
        tool_calls=tool_calls,
        files=files,
        stop_reason=result.stop_reason,
        evidence_incomplete=evidence_incomplete,
    )


def _read_local(path: Optional[str]) -> Optional[str]:
    """同机时按绝对路径直读磁盘内容(agents.files.get 对用户新建文件有白名单限制时的回退)。"""
    if not path:
        return None
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.debug("本地读盘失败 %s: %s", path, e)
    return None


async def _resolve_file_evidence(
    gateway: Any,
    agent_id: str,
    fe: FileEvidence,
    inventory: dict[str, dict],
    workspace_path: Optional[str],
) -> None:
    """把单个 FileEvidence 解析为磁盘真相。

    取证优先级:
    1. `agents.files.get`(可移植、可跨 agent;但本网关对用户新建文件有白名单限制);
    2. 同机按绝对路径读盘(get 读不到用户文件时的真相来源);
    3. 网关清点的存在性(白名单可见但内容读不到时)。
    全部受阻才降级为 error(证据缺失,不得判负);网关明确报"缺失"则权威判 exists=False
    (用于拆穿"声称生成但磁盘无此文件")。
    """
    info = inventory.get(fe.name)
    if info is not None:
        fe.size = info.get("size")
        fe.path = info.get("path")
    fe.checked = True

    get_missing = False
    get_error: Optional[str] = None
    try:
        resp = await gateway.agents_files_get(agent_id, fe.name)
        parsed = AgentFileContent.model_validate(resp)
        if not parsed.missing and parsed.content is not None:
            fe.exists = True
            fe.content = parsed.content
            return
        get_missing = True  # 网关权威:该文件缺失
    except Exception as e:  # noqa: BLE001
        get_error = str(e)  # get 不支持该文件 / 网关异常

    # 同机读盘回退
    local_path = (info.get("path") if info else None) or (
        str(Path(workspace_path) / fe.name) if workspace_path else None
    )
    if fe.path is None:
        fe.path = local_path
    content = _read_local(local_path)
    if content is not None:
        fe.exists = True
        fe.content = content
        if fe.size is None and local_path:
            try:
                fe.size = Path(local_path).stat().st_size
            except Exception:  # noqa: BLE001
                pass
        return

    if info is not None:  # 白名单可见但内容读不到 → 以存在性为准
        fe.exists = bool(info.get("exists"))
        return

    if get_missing:  # 网关权威缺失 → 拆穿声称
        fe.exists = False
    else:  # 既读不到也无法判定 → 降级为证据缺失(不判负)
        fe.exists = False
        fe.error = get_error or "文件不可达/无法核验"
        logger.debug("capture_file_evidence 取证受阻 %s/%s: %s", agent_id, fe.name, get_error)


async def capture_file_evidence(
    gateway: Any, agent_id: str, record: TurnRecord, *, discover: bool = True
) -> None:
    """就地把 record.files 升级为磁盘真相,并主动清点工作区发现 agent 新建文件。

    D5 落实:不采信 `ExecutionResult.files` 自报(常为空)。流程:
    1. `agents.files.list` 取被测工作区**路径**与白名单文件清单;
    2. 升级 agent 自报的文件证据(get → 同机读盘 → 清点);
    3. `discover=True` 时**直接扫描本地工作区目录**(主动去发现 agent 未自报的新文件）

    取证受阻一律降级为证据缺失,MUST NOT 当负面证据(由下游 evaluator 规则保证)。
    """
    inventory: dict[str, dict] = {}
    workspace_path: Optional[str] = None
    if discover:
        try:
            listing = await gateway.agents_files_list(agent_id)
            workspace_path = listing.get("workspace")
            for e in (listing.get("files") or []):
                nm = e.get("name") or e.get("path") or ""
                if not nm:
                    continue
                inventory[nm] = {
                    "exists": not e.get("missing", False), # OpenClaw-网关会判断对应的文件是否missing,避免虚假声称
                    "size": e.get("size"),
                    "path": e.get("path"),
                }
        except Exception as e:  # noqa: BLE001
            logger.debug("agents_files_list 不可用,降级为仅核验自报文件: %s", e)

    # 2. 升级 agent 自报的文件
    for fe in record.files:
        await _resolve_file_evidence(gateway, agent_id, fe, inventory, workspace_path)

    # 3. 直接扫描本地工作区目录,发现用户新建产物(网关 list 受白名单限制看不到)
    if discover and workspace_path:
        claimed = {fe.name for fe in record.files}
        try:
            for child in sorted(Path(workspace_path).iterdir()):
                if not child.is_file():
                    continue
                nm = child.name
                if nm in claimed or nm in SCAFFOLDING_FILES:
                    continue
                fe = FileEvidence(name=nm, discovered=True, checked=True, exists=True, path=str(child))
                try:
                    fe.size = child.stat().st_size
                except Exception:  # noqa: BLE001
                    pass
                fe.content = _read_local(str(child))
                record.files.append(fe)
        except Exception as e:  # noqa: BLE001
            logger.debug("本地工作区清点失败 %s: %s", workspace_path, e)
