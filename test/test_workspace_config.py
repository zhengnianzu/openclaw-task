"""user_workspace 字段 + content_root 解析测试(不依赖 openclaw gateway / API)

覆盖 change add-user-workspace-field 的验收:
  1. content_root 三档语义(None / "name" / "")
  2. 向后兼容:无 user_workspace 的老配置 == 旧「同名目录」行为
  3. 单一来源:setup_agent_files 直接使用上游传入的 content_root,
     不再自行推导同名子目录
  4. evaluate 引用基准未漂移:oracle_ref 仍相对 user_dir.path 父层

本机无 pip/pydantic,须在镜像里跑:
  docker run --rm -v "$PWD":/app -w /app --entrypoint python3 \
    openclaw-task:latest test/test_workspace_config.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import UserDirConfig, InputDirConfig, ConfigLoader
from src.workspace import BaseWorkspaceManager


def test_content_root_three_tiers():
    """3.1 三档语义。"""
    base = "/data/tasks/paper_reader"
    assert UserDirConfig(path=base).content_root == Path(base) / "paper_reader"
    assert UserDirConfig(path=base, user_workspace="ws").content_root == Path(base) / "ws"
    assert UserDirConfig(path=base, user_workspace="").content_root == Path(base)
    print("[ok] 3.1 content_root 三档: None -> 同名 / 'ws' -> path/ws / '' -> path")


def test_is_none_not_truthy():
    """3.1 空串必须与未设置区分(is None,而非真值判断)。"""
    cfg = UserDirConfig(path="/x/y", user_workspace="")
    # 若实现用了 `or`,'' 会退化成同名目录 /x/y/y —— 明确反例。
    assert cfg.content_root == Path("/x/y")
    assert cfg.content_root != Path("/x/y/y")
    print("[ok] 3.1 空串走 is None 分支,未被误并入未设置")


def test_backward_compat():
    """3.2 老配置(无 user_workspace)== 旧硬编码 user_path / user_path.name。"""
    base = "/some/deep/long/task_name_v2"
    cfg = UserDirConfig(path=base)
    old_behavior = Path(base).expanduser() / Path(base).expanduser().name
    assert cfg.content_root == old_behavior
    # 字符串形式的 user_dir 经 coerce 后 user_workspace 仍为 None
    assert InputDirConfig(user_dir=base).user_dir.user_workspace is None
    print("[ok] 3.2 无 user_workspace 与旧同名目录行为逐字一致")


class _StubWorkspaceManager(BaseWorkspaceManager):
    """最小具体子类:workspace 指向临时目录,agent 配置复制为 no-op。"""

    def __init__(self, workspace: Path):
        self._workspace = workspace

    def get_agent_workspace(self, agent_name: str) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def _copy_agent_configs(self, workspace, config_files, agent_dir) -> None:
        pass


def test_single_source_no_rederive():
    """3.3 setup_agent_files 直接用传入 content_root,不再推导同名子目录。

    传入一个 basename 与父层不同的 content_root,内含 sentinel 文件。
    若仍走旧的 `content_root / content_root.name`,会去找不存在的子目录而复制不到。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        content_root = tmp / "explicit_ws"
        content_root.mkdir()
        (content_root / "sentinel.txt").write_text("hi", encoding="utf-8")

        workspace = tmp / "workspace"
        mgr = _StubWorkspaceManager(workspace)
        mgr.setup_agent_files(
            agent_name="main",
            config_files=[],
            skill_base_dir=None,
            agent_skills=[],
            agent_dir=None,
            content_root=str(content_root),
        )
        copied = workspace / "sentinel.txt"
        assert copied.exists(), "sentinel 未复制:content_root 被错误地再次推导同名子目录"
        assert copied.read_text(encoding="utf-8") == "hi"
    print("[ok] 3.3 workspace 层直接复制 content_root 内容,单一来源无重复推导")


def test_evaluate_ref_base_unchanged():
    """3.4 配置 user_workspace 后,oracle_ref 仍相对 user_dir.path 父层解析。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # oracle 放在 path 父层(不是 content_root 子层)
        (tmp / "oracle.json").write_text(json.dumps({"answer": 42}), encoding="utf-8")
        # 同时造一个 content_root 子层,确保解析没跑去那里
        (tmp / "ws").mkdir()

        data = {
            "harness_type": "openclaw",
            "input_dir": {"user_dir": {"path": str(tmp), "user_workspace": "ws"}},
            "agents": [{"name": "main", "config": []}],
            "queries": [{
                "agent_name": "main",
                "text": "q",
                "evaluate": {"oracle_ref": "oracle.json"},
            }],
        }
        cfg = ConfigLoader.load_from_dict(data)
        ev = cfg.queries[0].evaluate
        assert ev.oracle_data == {"answer": 42}, "oracle_ref 未从 path 父层解析"
        assert str(tmp / "oracle.json") in ev.file_vault
    print("[ok] 3.4 evaluate oracle_ref 基准仍为 user_dir.path 父层,未漂移到 content_root")


def main():
    test_content_root_three_tiers()
    test_is_none_not_truthy()
    test_backward_compat()
    test_single_source_no_rederive()
    test_evaluate_ref_base_unchanged()
    print("\nALL GREEN: user_workspace / content_root 全部通过")


if __name__ == "__main__":
    main()
