"""任务和网关共享沙箱 ID；工作区首次启动时创建，进程重启后继续复用。"""

from functools import lru_cache
import os
from pathlib import Path
import re
import time
from uuid import uuid4


@lru_cache(maxsize=None)
def _file_identity(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # 另一个启动进程可能刚创建文件，等待它写完这 32 个字节。
        for _ in range(50):
            value = path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
            if value and (len(value) > 32 or not re.fullmatch(r"[0-9a-f]+", value)):
                raise ValueError(f"沙箱 ID 文件内容无效：{path}")
            time.sleep(0.01)
        raise RuntimeError(f"沙箱 ID 文件未完成初始化：{path}")
    value = uuid4().hex
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(value)
    return value


def get_sandbox_id() -> str:
    value = os.environ.get("SANDBOX_ID", "").strip()
    if value:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            raise ValueError("SANDBOX_ID 必须为不含空白的 ASCII 标识")
        return value
    path = Path(os.environ.get("SANDBOX_ID_FILE") or Path(__file__).resolve().parents[1] / "cache/sandbox_id").expanduser().resolve()
    return _file_identity(path)
