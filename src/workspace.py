"""
WorkspaceManager 共享基类

提供 setup_from_map / user_dir 整体复制 / skills 复制等通用逻辑,
harness 特有的 workspace 路径计算和 agent 配置文件放置由子类实现。
"""

import json
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("harness_automation")


DEFAULT_CONFIG_DIR = "__default__"


def copy_path(src: Path, dst: Path):
    """复制文件或目录，自动判断类型，目标已存在时合并/覆盖。

    ``__default__`` 是 Agent 配置的公共来源目录，不应出现在最终 workspace：
    复制整个目录时将其子项合并到目标根；复制其直接子项时自动去掉目标路径中的
    ``__default__`` 层。
    """
    src = Path(src)
    dst = Path(dst)

    if src.is_dir() and src.name == DEFAULT_CONFIG_DIR:
        target_root = dst.parent if dst.name == DEFAULT_CONFIG_DIR else dst
        target_root.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            copy_path(child, target_root / child.name)
        return

    if src.parent.name == DEFAULT_CONFIG_DIR and dst.parent.name == DEFAULT_CONFIG_DIR:
        default_dst = dst.parent
        dst = dst.parent.parent / dst.name
        # 部分 client 会在调用 copy_path 前预建 target.parent；这里只清理空目录，
        # 若其中已有其他内容则保留，避免误删调用方数据。
        if default_dst.is_dir():
            try:
                default_dst.rmdir()
            except OSError:
                pass

    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    elif src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)


class BaseWorkspaceManager(ABC):
    """Agent 工作空间管理基类"""

    @abstractmethod
    def get_agent_workspace(self, agent_name: str) -> Path:
        """获取 Agent 工作空间路径"""
        ...

    @abstractmethod
    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        """将 agent 配置文件(SOUL.md, USER.md 等)复制到 workspace,布局由子类决定"""
        ...

    def get_skills_dst(self, workspace: Path) -> Path:
        """skills 落盘目录,默认 workspace/skills,子类可覆盖布局"""
        return workspace / "skills"

    def setup_agent_files(
        self,
        agent_name: str,
        config_files: List[str],
        skill_base_dir: Optional[str],
        agent_skills: List[str],
        agent_dir: Optional[str] = None,
        content_root: Optional[str] = None,
    ) -> None:
        workspace = self.get_agent_workspace(agent_name)

        logger.info("workspace: %s", workspace)
        if skill_base_dir and agent_skills:
            logger.info("skills_dst: %s", self.get_skills_dst(workspace))
        if content_root:
            logger.info(
                "content_root -> workspace: %s -> %s",
                Path(content_root).expanduser(),
                workspace,
            )

        if agent_dir and config_files:
            self._copy_agent_configs(workspace, config_files, agent_dir)

        if skill_base_dir and agent_skills:
            skills_dst = self.get_skills_dst(workspace)
            skills_dst.mkdir(parents=True, exist_ok=True)
            for skill_path in agent_skills:
                skill_name = Path(skill_path).name
                src = Path(skill_base_dir) / skill_path
                if src.exists() and src.is_dir():
                    dst = skills_dst / skill_name
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    logger.info("复制技能: %s -> %s", skill_path, dst)
                else:
                    logger.warning("技能目录不存在: %s", src)

        if content_root:
            # content_root 已由上游(UserDirConfig.content_root)解析好,
            # 此处直接把其下内容复制进 workspace,不再自行推导同名子目录。
            root_path = Path(content_root).expanduser()
            logger.debug("check content_root: %s", root_path)
            if root_path.exists() and root_path.is_dir():
                for item in root_path.iterdir():
                    item_dst = workspace / item.name
                    if item_dst.exists():
                        if item_dst.is_dir():
                            shutil.rmtree(item_dst)
                        else:
                            item_dst.unlink()
                    if item.is_dir():
                        shutil.copytree(item, item_dst)
                    else:
                        shutil.copy2(item, item_dst)
                logger.info("复制用户目录: %s -> %s", root_path, workspace)
            else:
                logger.warning("用户内容目录不存在或不是目录: %s", root_path)

    def setup_from_map(
        self,
        map_file: str,
        base_dir: Optional[str] = None,
        agent_name: str = "main",
    ) -> None:
        """根据 map.json 按映射逐条复制文件/目录
        目标路径里的 "~" 解析为 **执行 agent 的 workspace**,"""
        map_path = Path(map_file)
        if not map_path.exists():
            logger.warning("map 文件不存在: %s", map_path)
            return

        mapping: Dict[str, str] = json.loads(map_path.read_text(encoding="utf-8"))
        base = Path(base_dir) if base_dir else None
        agent_ws = self.get_agent_workspace(agent_name)
        logger.info("读取 map 文件: %s,共 %d 条映射", map_path, len(mapping))
        logger.info("目标 workspace(~) [agent=%s]: %s", agent_name, agent_ws)
        if base:
            logger.info("源路径基准目录: %s", base)

        for src_str, dst_str in mapping.items():
            src = (base / src_str) if base else Path(src_str).expanduser()

            # "~" 解析为该 agent 的 workspace;其余按绝对路径直接复制
            s = dst_str.strip()
            if s == "~":
                dst = agent_ws
            elif s.startswith("~/") or s.startswith("~\\"):
                dst = agent_ws / s[2:]
            else:
                dst = Path(s)

            if not src.exists():
                logger.warning("源路径不存在,跳过: %s", src)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)

            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

            logger.info("映射复制: %s -> %s", src_str, dst)
