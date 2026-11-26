"""TaiChu 的 **docker 启动壳 —— 仅用于本地测试**。

批跑不走这里：批跑由 hive 在沙箱 Pod 内直接运行 ``taichu-cli``，见
``src/taichu_client.py``（本模块只是给那套客户端换一个启动壳，复用全部
persona / workspace / evidence / ledger 逻辑）。

用途：在没有 hive 的机器上，用 ``docker run`` 跑同一个镜像做冒烟验证。

    from test.taichu_docker import build_taichu_docker_client

    async with await build_taichu_docker_client() as client:
        ...
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.taichu_client import (
    TaichuAgent,
    TaichuClient,
    TaichuHarnessError,
    TaichuPaths,
    persona_for,
)

RUNTIME_MOUNT = "/trial"                    # runtime/ 在容器内的挂载点
CONTAINER_CONFIG = "/config/.taichu.json"   # 与镜像内置 .feature.json 同目录
CONTAINER_TOOLS = "/tools"
DEFAULT_IMAGE = "taichu-cli-linux:0.2.0"    # 派生自 taichu-cli-eval:0.2.0
# 本地测试默认用交付目录；批跑由 TAICHU_HOME 指向沙箱 home。
DEFAULT_HOME = Path("/root/zengxiang/taichu/taichu-cli-eval-v0.2.0-linux-amd64")


def _docker_name(*parts: str) -> str:
    """生成合法的容器名（[a-zA-Z0-9][a-zA-Z0-9_.-]*）。"""
    raw = "-".join(parts)
    return ("taichu-" + re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-."))[:63]


def _declared_manifest(home: Path) -> Optional[str]:
    """读取交付目录 .taichu.json 里声明的 tool_manifest（读不到就返回 None）。"""
    try:
        config = json.loads((home / "configs" / ".taichu.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    manifest = config.get("tool_manifest")
    return str(manifest) if manifest else None


@dataclass(frozen=True)
class DockerPaths(TaichuPaths):
    """把同一套宿主路径映射成容器内路径。"""

    image: str = DEFAULT_IMAGE
    network: Optional[str] = None

    @classmethod
    def resolve(
        cls,
        home: Optional[str] = None,
        tools: Optional[str] = None,
        cli: Optional[str] = None,
        image: Optional[str] = None,
        network: Optional[str] = None,
    ) -> "DockerPaths":
        base = TaichuPaths.resolve(home=home or str(DEFAULT_HOME), tools=tools, cli=cli)

        # 交付目录自带 example/tools；配置声明了 tool_manifest 就默认挂它。
        tools_path = base.tools
        if tools_path is None:
            manifest = _declared_manifest(base.home)
            candidate = base.home / "example" / "tools"
            if manifest and candidate.is_dir():
                tools_path = candidate.resolve()

        resolved_image = image or os.environ.get("TAICHU_IMAGE")
        if not resolved_image:
            tag_file = base.home / "image-tag.txt"
            if tag_file.is_file():
                resolved_image = tag_file.read_text(encoding="utf-8").strip()
        resolved_image = resolved_image or DEFAULT_IMAGE

        if network is None:
            network = os.environ.get("TAICHU_DOCKER_NETWORK", "host")
        return cls(base.home, tools_path, base.cli, resolved_image, network or None)

    # -- CLI 侧路径：映射到挂载点 -------------------------------------------
    def cli_config(self) -> str:
        return CONTAINER_CONFIG

    def cli_user_root(self) -> str:
        return f"{RUNTIME_MOUNT}/user"

    def cli_workspace(self, agent_name: str) -> str:
        return (f"{RUNTIME_MOUNT}/user"
                f"/agent_instances/{persona_for(agent_name)}/workspace")

    def cli_path(self, host_path: Path) -> str:
        relative = Path(host_path).resolve().relative_to(self.home)
        return f"{RUNTIME_MOUNT}/{relative.as_posix()}"


class DockerAgent(TaichuAgent):
    """每轮一次 ``docker run``，进程被杀时显式回收容器。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._container_name: Optional[str] = None

    def _launch_prefix(self) -> list[str]:
        paths = self.client.paths
        self._container_name = _docker_name(
            self.agent_name, self.session_name, uuid.uuid4().hex[:8]
        )
        command = ["docker", "run", "--rm", "--name", self._container_name]
        if paths.network:
            command += ["--network", paths.network]
        if os.name == "posix":
            command += ["--user", f"{os.getuid()}:{os.getgid()}"]
        command += ["-v", f"{paths.config_file}:{CONTAINER_CONFIG}:ro"]
        if paths.tools:
            command += ["-v", f"{paths.tools}:{CONTAINER_TOOLS}:ro"]
        command += ["-v", f"{paths.home}:{RUNTIME_MOUNT}"]
        command += [paths.image]
        return command

    async def _cleanup_launch(self) -> None:
        name = self._container_name
        if not name:
            return
        self._container_name = None
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        except OSError:
            pass


class DockerClient(TaichuClient):
    paths_class = DockerPaths
    agent_class = DockerAgent

    async def check_runtime(self) -> None:
        """docker 模式校验镜像是否已导入（CLI 在镜像里，不在宿主 PATH）。"""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", self.paths.image,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await process.communicate()
        except OSError as error:
            raise TaichuHarnessError(f"无法调用 docker: {error}") from error
        if process.returncode != 0:
            raise TaichuHarnessError(
                f"本地没有镜像 {self.paths.image}，请先导入：\n"
                f"  docker load -i <交付目录>/taichu-cli-eval.tar   # 上游\n"
                f"  或构建派生镜像 taichu-cli-linux:<版本>"
            )


async def build_taichu_docker_client(home=None, **kwargs) -> DockerClient:
    """仅供测试：返回值签名与 ``build_taichu_client`` 一致。"""
    return DockerClient(home=home, **kwargs)
