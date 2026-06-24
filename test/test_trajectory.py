"""轨迹捕获单测(能力: trajectory-capture)。

用法:  python test/test_trajectory.py
不依赖网关/网络,只验证纯逻辑。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openclaw_sdk.core.types import ExecutionResult, GeneratedFile, ToolCall

from trajectory import build_turn_record, capture_file_evidence


def test_normal_turn_captures_tool_calls():
    """1.5: 正常返回的 turn 捕获到 tool_calls(及其返回值)。"""
    result = ExecutionResult(
        success=True,
        content="已完成",
        tool_calls=[ToolCall(tool="write_file", input="report.md", output="ok", duration_ms=12)],
        files=[GeneratedFile(name="report.md", path="/ws/report.md", size_bytes=10, mime_type="text/markdown")],
        stop_reason="complete",
    )
    rec = build_turn_record(1, "帮我写报告", result, evidence_incomplete=False)
    assert len(rec.tool_calls) == 1
    assert rec.tool_calls[0].tool == "write_file"
    assert rec.tool_calls[0].output == "ok"
    assert [f.name for f in rec.files] == ["report.md"]
    assert rec.files[0].checked is False  # 尚未做磁盘核验,仅记录声称
    assert rec.evidence_incomplete is False
    print("✓ 正常 turn 捕获 tool_calls")


def test_fallback_turn_marked_incomplete():
    """1.5: 经 history 兜底恢复的 turn 被标 evidence_incomplete。"""
    result = ExecutionResult(success=True, content="兜底文本", stop_reason="complete")
    rec = build_turn_record(2, "继续", result, evidence_incomplete=True)
    assert rec.evidence_incomplete is True
    assert rec.tool_calls == []
    print("✓ 兜底 turn 标记 evidence_incomplete")


class _FakeGateway:
    """假网关:disk[name] = 内容字符串(存在)或 None(不存在)。"""

    def __init__(self, disk):
        self.disk = disk

    async def agents_files_get(self, agent_id, name):
        if self.disk.get(name) is not None:
            return {"name": name, "content": self.disk[name], "missing": False}
        return {"name": name, "content": None, "missing": True}


def test_disk_truth_overrides_claim():
    """1.5: 声称生成的文件以磁盘核对为准(拆穿声称但磁盘无)。"""
    result = ExecutionResult(
        success=True,
        content="我生成了 a.md 和 b.md",
        files=[
            GeneratedFile(name="a.md", path="/ws/a.md", size_bytes=1, mime_type="text/markdown"),
            GeneratedFile(name="b.md", path="/ws/b.md", size_bytes=1, mime_type="text/markdown"),
        ],
    )
    rec = build_turn_record(1, "q", result, evidence_incomplete=False)
    gw = _FakeGateway({"a.md": "hello", "b.md": None})
    asyncio.run(capture_file_evidence(gw, "paper_reader", rec))
    by = {f.name: f for f in rec.files}
    assert by["a.md"].checked and by["a.md"].exists and by["a.md"].content == "hello"
    assert by["b.md"].checked and not by["b.md"].exists  # 声称生成但磁盘无 → 拆穿
    print("✓ 磁盘真相校正自报文件证据")


def test_file_fetch_error_degrades_not_negative():
    """1.5: 取证受阻降级为 error(证据缺失),而非判负。"""

    class _BrokenGateway:
        async def agents_files_get(self, agent_id, name):
            raise RuntimeError("路径不可达")

    result = ExecutionResult(
        success=True, content="生成了 c.md",
        files=[GeneratedFile(name="c.md", path="/ws/c.md", size_bytes=1, mime_type="text/markdown")],
    )
    rec = build_turn_record(1, "q", result, evidence_incomplete=False)
    asyncio.run(capture_file_evidence(_BrokenGateway(), "a", rec))
    fe = rec.files[0]
    assert fe.checked and fe.error is not None and fe.exists is False
    print("✓ 取证受阻降级为证据缺失(不判负)")


def test_discovers_workspace_file_when_self_report_empty():
    """1.3/D5: 自报为空 + 网关 files.get/list 白名单读不到用户文件时,
    经 files.list 拿到 workspace 路径后**直接扫描本地工作区目录**发现新产物并读盘。

    复现真实网关行为: files.list 只回脚手架白名单、files.get 对用户文件抛 'unsupported file'。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as ws:
        Path(ws, "AGENTS.md").write_text("scaffold", encoding="utf-8")
        Path(ws, "openclaw_report.md").write_text("- 要点1\n- 要点2\n- 要点3\n", encoding="utf-8")

        class _WhitelistGateway:
            async def agents_files_list(self, agent_id):
                # 真实网关只暴露脚手架白名单,不含用户新建文件
                return {"agentId": agent_id, "workspace": ws, "files": [
                    {"name": "AGENTS.md", "path": str(Path(ws, "AGENTS.md")), "missing": False, "size": 8},
                ]}

            async def agents_files_get(self, agent_id, name):
                raise RuntimeError(f'unsupported file "{name}"')

        result = ExecutionResult(success=True, content="文件已创建", files=[])  # 自报为空
        rec = build_turn_record(1, "创建 openclaw_report.md", result, evidence_incomplete=False)
        assert rec.files == []
        asyncio.run(capture_file_evidence(_WhitelistGateway(), "main", rec))
        by = {f.name: f for f in rec.files}
        assert "openclaw_report.md" in by, "应扫描本地工作区发现用户新建文件"
        fe = by["openclaw_report.md"]
        assert fe.checked and fe.exists and fe.discovered
        assert fe.content and "要点1" in fe.content, "应读到真实磁盘内容"
        assert "AGENTS.md" not in by, "脚手架文件不应被当作新产物 surface"
    print("✓ 自报为空时扫描本地工作区发现新文件并读到磁盘内容")


if __name__ == "__main__":
    test_normal_turn_captures_tool_calls()
    test_fallback_turn_marked_incomplete()
    test_disk_truth_overrides_claim()
    test_file_fetch_error_degrades_not_negative()
    test_discovers_workspace_file_when_self_report_empty()
    print("\n全部通过 ✅ (test_trajectory)")
