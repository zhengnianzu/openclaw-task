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


def copy_path(src: Path, dst: Path):
    """复制文件或目录，自动判断类型，目标已存在时合并/覆盖"""
    if src.is_file():
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

    def setup_agent_files(
        self,
        agent_name: str,
        config_files: List[str],
        skill_base_dir: Optional[str],
        agent_skills: List[str],
        agent_dir: Optional[str] = None,
        user_dir: Optional[str] = None,
    ) -> None:
        workspace = self.get_agent_workspace(agent_name)

        logger.info("workspace: %s", workspace)
        if skill_base_dir and agent_skills:
            logger.info("skills_dst: %s", workspace / "skills")
        if user_dir:
            logger.info(
                "user_dir -> workspace: %s -> %s",
                Path(user_dir).expanduser(),
                workspace,
            )

        if agent_dir and config_files:
            self._copy_agent_configs(workspace, config_files, agent_dir)

        if skill_base_dir and agent_skills:
            skills_dst = workspace / "skills"
            skills_dst.mkdir(exist_ok=True)
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

        if user_dir:
            user_path = Path(user_dir).expanduser()
            logger.debug("check user_path: %s", user_path)
            if user_path.exists() and user_path.is_dir():
                content_root = user_path / user_path.name

                if not content_root.exists() or not content_root.is_dir():
                    logger.warning(
                        "user_dir content root does not exist or is not a directory: %s",
                        content_root,
                    )
                    return

                for item in content_root.iterdir():
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
                logger.info("复制用户目录: %s -> %s", content_root, workspace)
            else:
                logger.warning("用户目录不存在或不是目录: %s", user_path)

    def setup_from_map(self, map_file: str, base_dir: Optional[str] = None) -> None:
        """根据 map.json 按映射逐条复制文件/目录"""
        map_path = Path(map_file)
        if not map_path.exists():
            logger.warning("map 文件不存在: %s", map_path)
            return

        mapping: Dict[str, str] = json.loads(map_path.read_text(encoding="utf-8"))
        base = Path(base_dir) if base_dir else None
        logger.info("读取 map 文件: %s,共 %d 条映射", map_path, len(mapping))
        if base:
            logger.info("源路径基准目录: %s", base)

        for src_str, dst_str in mapping.items():
            src = (base / src_str) if base else Path(src_str).expanduser()
            dst = Path(dst_str).expanduser()

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

            logger.info("映射复制: %s -> %s", src_str, dst_str)
