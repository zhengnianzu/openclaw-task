#!/usr/bin/env python3
"""
open-webui 沙箱启动引导脚本 (方案 A:每个沙箱现拿一把 key)

流程:
  1. (可选) 拉起 open-webui 后端子进程(--start;端口取自 --base-url)
  2. 轮询 /health 等服务就绪
  3. signin(免登录模式的固定账号 admin@localhost/admin)拿 JWT
  4. 复用已有 sk- key;没有则调 /api/v1/auths/api_key 现生成;服务端禁用则回退 JWT
  5. 把 {base_url, api_key, model} 写进 user_proxy_model.json 里所有需走 open-webui
     的 agent 段(被测 + evaluator,排除 user_simulator),并打印 export 行

只用标准库 (urllib),沙箱里无需额外依赖。

用法:
  # 推荐:从 harness 配置自动读所有 agent(被测 + evaluator),不用手写 --agent
  python scripts/openwebui_bootstrap.py --from-config configs/config_openwebui.json \
      --base-url http://localhost:8088 --model deepseek-v4-flash

  # 手动指定单个 agent
  python scripts/openwebui_bootstrap.py --agent assistant1 \
      --base-url http://localhost:8088 --model deepseek-v4-flash

  # 让本脚本负责拉起 open-webui(端口跟随 --base-url,这里是 8088)
  python scripts/openwebui_bootstrap.py --start --backend-dir ../open-webui-main/backend \
      --from-config configs/config_openwebui.json \
      --base-url http://localhost:8088 --model deepseek-v4-flash

环境变量(均有默认值,命令行参数优先):
  OPENWEBUI_BASE_URL   默认 http://localhost:8080
  OPENWEBUI_ADMIN_EMAIL / OPENWEBUI_ADMIN_PASSWORD   默认 admin@localhost / admin
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:8080")
DEFAULT_ADMIN_EMAIL = os.environ.get("OPENWEBUI_ADMIN_EMAIL", "admin@localhost")
DEFAULT_ADMIN_PASSWORD = os.environ.get("OPENWEBUI_ADMIN_PASSWORD", "admin")


# ---------------------------------------------------------------------------
# 极简 HTTP 封装 (urllib)
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 15.0,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------

def start_openwebui(backend_dir: Path, port: int = 8080) -> subprocess.Popen:
    """拉起 open-webui 后端(uvicorn)。返回子进程句柄,由调用方决定生命周期。

    环境变量与 start_openwebui.bat 对齐:免登录 + 固定 secret + 开 API key(复数名)。
    """
    env = dict(os.environ)
    env.setdefault("WEBUI_AUTH", "False")               # 免登录 → 自动 admin@localhost/admin
    # 固定 secret:兜底 OAUTH_SESSION_TOKEN_ENCRYPTION_KEY,且重启后 JWT/加密数据不失效
    env.setdefault("WEBUI_SECRET_KEY", "sandbox-fixed-secret-0707")
    env.setdefault("ENABLE_API_KEYS", "True")           # 允许生成 sk- key(注意是复数)
    env.setdefault("ENABLE_OPENAI_API", "True")         # 接上游 OpenAI 兼容端点
    env.setdefault("ENABLE_OLLAMA_API", "False")        # 沙箱一般不用 ollama
    cmd = [
        sys.executable, "-m", "uvicorn", "open_webui.main:app",
        "--host", "0.0.0.0", "--port", str(port),
    ]
    print(f"[bootstrap] 启动 open-webui: {' '.join(cmd)} (cwd={backend_dir})")
    return subprocess.Popen(cmd, cwd=str(backend_dir), env=env)


def wait_ready(base_url: str, timeout: float = 120.0, interval: float = 2.0) -> None:
    """轮询 /health 直到 200 或超时。"""
    deadline = time.monotonic() + timeout
    url = base_url.rstrip("/") + "/health"
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            _request("GET", url, timeout=5.0)
            print(f"[bootstrap] open-webui 就绪: {url}")
            return
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(interval)
    raise RuntimeError(f"等待 open-webui 就绪超时 ({timeout}s): {url} 最后错误: {last_err}")


def signin(base_url: str, email: str, password: str) -> str:
    """登录拿 JWT。免登录模式下账号固定 admin@localhost/admin。"""
    url = base_url.rstrip("/") + "/api/v1/auths/signin"
    try:
        data = _request("POST", url, body={"email": email, "password": password})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"signin 失败 HTTP {e.code}: {detail}") from e
    token = data.get("token")
    if not token:
        raise RuntimeError(f"signin 响应无 token: {str(data)[:300]}")
    print(f"[bootstrap] 登录成功: {email}")
    return token


def ensure_api_key(base_url: str, jwt: str) -> str:
    """复用/生成 sk- key;若服务端禁用了 api_key(403)则回退用 JWT 当 Bearer。

    JWT 默认有效期 4 周(JWT_EXPIRES_IN),沙箱单次跑足够;要永久 key 则需在
    服务端开启 ENABLE_API_KEYS=True(且为全新库或在 UI 里打开开关)。
    """
    root = base_url.rstrip("/") + "/api/v1/auths/api_key"
    # 先看有没有现成的 sk- key
    try:
        existing = _request("GET", root, token=jwt)
        if existing.get("api_key"):
            print("[bootstrap] 复用已有 API key")
            return existing["api_key"]
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("[bootstrap] 服务端禁用 API key(403),回退使用 JWT 作为 Bearer")
            return jwt
    except Exception:  # noqa: BLE001
        pass  # 没有就往下尝试生成
    # 尝试生成
    try:
        created = _request("POST", root, token=jwt)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("[bootstrap] 服务端禁用 API key(403),回退使用 JWT 作为 Bearer")
            return jwt
        raise
    key = created.get("api_key")
    if not key:
        raise RuntimeError(f"生成 API key 失败: {str(created)[:300]}")
    print("[bootstrap] 新生成 API key")
    return key


def _import_one_tool(root: str, jwt: str, t: Dict[str, Any]) -> None:
    """导入单个工具对象(open-webui 导出格式 {id,name,content,meta})。"""
    if not isinstance(t, dict) or not t.get("id"):
        print(f"[bootstrap] 跳过无效工具项: {str(t)[:80]}")
        return
    body = {
        "id": t["id"],
        "name": t.get("name", t["id"]),
        "content": t.get("content", ""),
        "meta": t.get("meta") or {},
    }
    try:
        _request("POST", root, token=jwt, body=body)
        print(f"[bootstrap] 导入工具: {body['id']}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        if e.code == 400:
            print(f"[bootstrap] 工具 {body['id']} 已存在或校验未过,跳过 (HTTP 400: {detail[:150]})")
        else:
            print(f"[bootstrap] 导入工具失败 {body['id']}: HTTP {e.code} {detail[:200]}",
                  file=sys.stderr)


def import_tools(base_url: str, jwt: str, tools_path: str) -> None:
    """把 open-webui 导出格式的工具导入服务端(部署时加载工具)。

    tools_path 可以是:
      - 目录:导入其中所有 *.json(默认 open-webui-main/tools);
      - 单个文件:导入该文件。
    每个 JSON 为 [{id,name,content,meta}, ...] 或单个对象。幂等:同 id 已存在则跳过。
    路径不存在则静默跳过(不阻断引导)。
    """
    p = Path(tools_path).expanduser()
    if p.is_dir():
        files = sorted(p.glob("*.json"))
        if not files:
            print(f"[bootstrap] 工具目录无 *.json,跳过导入: {p}")
            return
        print(f"[bootstrap] 从目录导入工具: {p}(共 {len(files)} 个文件)")
    elif p.is_file():
        files = [p]
    else:
        print(f"[bootstrap] 工具路径不存在,跳过导入: {p}")
        return

    root = base_url.rstrip("/") + "/api/v1/tools/create"
    for f in files:
        try:
            tools = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[bootstrap] 工具文件解析失败,跳过 {f.name}: {e}", file=sys.stderr)
            continue
        for t in (tools if isinstance(tools, list) else [tools]):
            _import_one_tool(root, jwt, t)


def agents_from_harness_config(path: str) -> list:
    """从 harness 配置里解析所有需走 open-webui 的 agent 名(去重、保序)。

    收集范围:
      - queries[].agent_name         —— 被测 agent(如 main);
      - queries[].evaluate.agent_name —— evaluator(现在也走 open-webui);
      - agents[].name               —— 配置里声明的 agent(兜底,覆盖上面漏掉的)。
    排除 user_simulator(它用自己的模型端点,不指向 open-webui)。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names: list = []

    def _add(n):
        if n and n != "user_simulator" and n not in names:
            names.append(n)

    for q in data.get("queries", []):
        _add(q.get("agent_name"))
        ev = q.get("evaluate")
        if isinstance(ev, dict):
            _add(ev.get("agent_name"))
    for a in data.get("agents", []):
        if isinstance(a, dict):
            _add(a.get("name"))
    return names


def write_into_config(
    config_path: Path,
    agent: str,
    base_url: str,
    api_key: str,
    model: Optional[str],
) -> None:
    """把 {base_url, api_key, model} 写进 user_proxy_model.json 的指定 agent 段。"""
    data: Dict[str, Any] = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    entry = dict(data.get(agent) or {})
    entry["base_url"] = base_url
    entry["api_key"] = api_key
    if model:
        entry["model"] = model
    data[agent] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[bootstrap] 已写入 {config_path} 的 '{agent}' 段")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="open-webui 沙箱 key 引导")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--password", default=DEFAULT_ADMIN_PASSWORD)
    parser.add_argument("--from-config", default=None,
                        help="harness 配置文件(如 configs/config_openwebui.json);自动读取"
                             "被测 + evaluator 等所有需走 open-webui 的 agent")
    parser.add_argument("--agent", default="assistant1",
                        help="要写入的单个 agent 名(未提供 --from-config 时用)")
    parser.add_argument("--model", default=os.environ.get("OPENWEBUI_MODEL"),
                        help="open-webui 里的模型 id")
    parser.add_argument("--config", default="configs/user_proxy_model.json",
                        help="要写入的 harness 模型配置文件(user_proxy_model.json)")
    parser.add_argument("--import-tools", default="../open-webui-main/tools",
                        help="工具来源:目录(导入其中所有 *.json)或单个 JSON 文件;"
                             "默认 ../open-webui-main/tools。部署时 POST /api/v1/tools/create,"
                             "之后客户端自动发现并转发。设为空串可禁用")
    parser.add_argument("--start", action="store_true", help="是否由本脚本拉起 open-webui")
    parser.add_argument("--backend-dir", default="../open-webui-main/backend",
                        help="open-webui backend 目录(--start 时用)")
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    args = parser.parse_args()

    proc: Optional[subprocess.Popen] = None
    if args.start:
        # 端口从 --base-url 取,保证 --start 与后续探活/写入用同一个端口
        from urllib.parse import urlsplit
        port = urlsplit(args.base_url).port or 8080
        proc = start_openwebui(Path(args.backend_dir).resolve(), port=port)

    try:
        wait_ready(args.base_url, timeout=args.ready_timeout)
        jwt = signin(args.base_url, args.email, args.password)

        # 部署时加载工具(可选):导入后客户端会自动发现并转发其 tool_ids
        if args.import_tools:
            import_tools(args.base_url, jwt, args.import_tools)

        key = ensure_api_key(args.base_url, jwt)

        # 确定要写入的被测 agent 列表:优先从 harness 配置读,否则用 --agent
        if args.from_config:
            targets = agents_from_harness_config(args.from_config)
            if targets:
                print(f"[bootstrap] 从 {args.from_config} 解析到被测 agent: {targets}")
            else:
                print(f"[bootstrap] 警告: {args.from_config} 未解析到 agent,回退 --agent {args.agent}")
                targets = [args.agent]
        else:
            targets = [args.agent]

        for ag in targets:
            write_into_config(Path(args.config), ag, args.base_url, key, args.model)
        # 便于 shell 里 eval / 采集
        print(f"OPENWEBUI_BASE_URL={args.base_url}")
        print(f"OPENWEBUI_API_KEY={key}")
        if args.model:
            print(f"OPENWEBUI_MODEL={args.model}")
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap] 失败: {e}", file=sys.stderr)
        if proc is not None:
            proc.terminate()
        return 1

    if args.start:
        print("[bootstrap] open-webui 仍在前台运行(--start);Ctrl-C 结束。")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
