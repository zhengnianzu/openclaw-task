# Open WebUI 适配总结

汇总「把 Open WebUI 作为第 4 种 harness 接入 openclaw-task」的全部适配现状。
改动分属两个仓库,已明确标注;**未改动 open-webui 任何源码**(仅新增启动脚本 + 工具目录)。

- **A. 当前项目 `D:\code\openclaw-task`**
- **B. `D:\code\open-webui-main`**

---

## 1. 接入方式与总体架构

- openwebui 作为**已运行的 HTTP 服务**被调用,通过其 **OpenAI 兼容接口 `POST /api/chat/completions`**
  (`Authorization: Bearer` 鉴权)驱动被测模型。不在进程内嵌入 open-webui。
- 多轮会话历史由客户端**本地累积**(`OpenwebuiAgent._history`),每轮把完整 messages 发过去
  (open-webui 兼容接口本身无状态)。
- 与其它 harness 完全对等地复用共享的 `src/executor.py` / evaluator / user_simulator。
- 连接三元组 `base_url / api_key / model` 来源优先级:`simulator_config` 覆盖
  (`user_proxy_model.json` 里按 agent 名)> 环境变量(`OPENWEBUI_BASE_URL/API_KEY/MODEL`)> 默认。

---

## 2. A —— `D:\code\openclaw-task` 改动

### 2.1 新增文件

| 文件 | 作用 |
|---|---|
| `src/openwebui_client.py` | Open WebUI harness 客户端(核心)。契约与 claudecode/hermes 对齐。 |
| `scripts/openwebui_bootstrap.py` | 部署引导:起服务/等就绪/拿鉴权/导入工具/写配置,一条命令搞定。纯标准库。 |
| `configs/config_openwebui.json` | 示例配置(`harness_type=openwebui`,含 evaluate 块,`platform=["windows"]`)。 |
| `configs/system_prompt_openwebui.txt` | openwebui 默认系统提示词("盘古"),由 `system_prompt_file` 引用。 |
| `docs/openwebui_integration.md` | 本文件。 |

### 2.2 修改文件

| 文件 | 改动 |
|---|---|
| `harness_automation.py` | `__init__` / `run()` 增加 openwebui 分支;新增 `_run_openwebui()`;CLI `--harness` 帮助更新。 |
| `src/executor.py` | `_make_options` fallback 链追加 openwebui 的 `ExecutionOptions`(让 timeout 生效)。 |
| `src/config.py` | `harness_type` description 更新;新增 `AgentConfigItem.system_prompt_file` 字段 + 加载时 `_resolve_system_prompt_files()`(把文件读入 system_prompt)。 |
| `README.md` | 新增 Open WebUI 章节、测试矩阵/运行示例/模型路由表各补一行。 |
| `configs/user_proxy_model.json` | `main` 与 `evaluator` 段均指向 open-webui(⚠️ 含凭据,勿入公共库)。 |
| `src/evaluator/evaluator.py` | `_reset_session` 无 gateway 时回退 `await eval_agent.reset()`。 |
| `src/hermes_client.py` / `src/claudecode_client.py` | 各新增 `reset()`(配合上一条,跨 harness 统一)。 |

### 2.3 功能详解(`src/openwebui_client.py`)

**(a) 请求与错误处理** —— `OpenwebuiAgent.execute`
- body:`{model, messages, stream:false}`;`Authorization: Bearer <api_key>`。
- 错误全部返回式处理:超时→`stop_reason=timeout`;非 2xx/结构异常→`success=False`;不抛穿主循环。
- 成功且非空才把 user+assistant 落入本地 `_history`。

**(b) 端点归一** —— `_completions_url` / `_is_openwebui_endpoint`
- 已带 `.../chat/completions` → 原样;
- 裸 OpenAI 端点(以 `/v1` 结尾,如 yibuapi)→ 拼 `/chat/completions`;
- 其余(open-webui 服务,如 `http://localhost:8088`)→ 拼 `/api/chat/completions`。
- 解决了 evaluator 指向裸 OpenAI 端点时路径拼错导致的 `No JSON found`。

**(c) evaluator 每轮防锚定 reset**
- `OpenwebuiAgent.reset()` 持锁清空 `_history`;由 evaluator 每评审点调用,确保其上一轮判词不锚定。
- 同问题在 hermes/claudecode 也修了(`reset()` 分别为清历史 / 断子进程重连)。

**(d) 工具:自动发现 + 透传(路径「用 open-webui 内置工具」)**
- **本工程零硬编码工具名**。`OpenwebuiClient.discover_tool_ids()` 对 open-webui 端点调
  `GET /api/v1/tools/` 取回所有已注册工具 id(按 base_url 缓存),`execute` 时塞进请求的 `tool_ids`。
- `features`(web_search/code_interpreter 等布尔开关,非注册工具、无法自动发现)由环境变量
  `OPENWEBUI_FEATURES`(逗号分隔)声明。
- 仅对 open-webui 端点生效;裸 OpenAI 端点(`/v1`)不带这两个字段(不污染上游)。
- 工具由 **open-webui 服务端执行并回喂**(与 hermes 让 AIAgent 自执行工具同模式),本工程不建执行器。

> ⚠️ 关键事实:open-webui 对 **API 调用不会自动套用**模型挂的工具(内置工具注入被 `session_id`
> 门卡住,仅 UI 生效,见 `middleware.py:2518`)。所以必须由客户端显式带 `tool_ids`/`features`——
> 这正是 (d) 做的事,无法绕过(除非改 open-webui 源码)。

**(e) 系统提示词:文件化 + 时间变量替换**
- **文件化**(通用,`src/config.py`):`AgentConfigItem` 新增 `system_prompt_file`;加载时
  `_resolve_system_prompt_files()` 把文件内容读入 `system_prompt`。字面 `system_prompt` 优先
  (作覆盖),文件作默认。`config_openwebui.json` 的 main 用
  `"system_prompt_file": "configs/system_prompt_openwebui.txt"`。改默认=编辑该文件;换一份=改路径。
- **时间变量替换**(`_apply_prompt_vars`,openwebui 客户端):每次请求发送前(`_build_messages`)
  替换 `{{CURRENT_DATE/TIME/DATETIME/WEEKDAY}}`,格式与 open-webui `utils/task.py` 对齐。
  放在发送时而非部署/引导时,保证长批跑里日期始终是当下。

---

## 3. B —— `D:\code\open-webui-main` 改动

**未改动 open-webui 源码。** 仅两项新增:

| 路径 | 作用 |
|---|---|
| `start_openwebui.bat` | Windows 一键启动(免登录 + 接上游端点)。纯 ASCII(避免 GBK cmd 乱码)。 |
| `tools/`(目录) | bootstrap 默认的工具导入来源;已放入 `sub_agent_tool.json`。新工具丢这里即可。 |

`start_openwebui.bat` 关键环境变量:

| 变量 | 值 | 说明 |
|---|---|---|
| `OPENAI_API_BASE_URL` / `OPENAI_API_KEY` | 上游模型端点 | open-webui 从其 `/models` 自动发现模型 |
| `WEBUI_SECRET_KEY` | 固定值 | 修复 `OAUTH_SESSION_TOKEN_ENCRYPTION_KEY is not set`;重启后 JWT/加密不失效 |
| `WEBUI_AUTH=False` | 免登录 | 自动 `admin@localhost`/`admin`(需全新库) |
| `ENABLE_API_KEYS=True` | 开 API key | 注意**复数**(源码变量名),默认 False |
| `ENABLE_OLLAMA_API=False` | 关 ollama | 沙箱不用 |
| `PORT=8088` | 端口 | 本机 8080 被 VS Code 占用 |

---

## 4. Bootstrap 脚本(`scripts/openwebui_bootstrap.py`)

把「服务起来」到「harness 能直接用」之间的手工步骤全自动化。流程:

1. **(可选 `--start`)拉起 open-webui**(端口取自 `--base-url`,环境变量对齐 bat)。
2. **等就绪**:轮询 `GET /health`。
3. **signin**:免登录固定账号 `admin@localhost/admin` → JWT。
4. **导入工具**:`--import-tools`(默认 `../open-webui-main/tools`),目录则导入所有 `*.json`,
   逐个 `POST /api/v1/tools/create`(幂等,同 id 跳过)。
5. **拿 key**:复用/生成 `sk-` key;服务端禁用(403)则回退用 JWT(默认 4 周有效)。
6. **写配置**:`--from-config` 自动解析所有需走 open-webui 的 agent
   (`queries[].agent_name` + `queries[].evaluate.agent_name` + `agents[].name`,排除
   `user_simulator`),给每个写入 `{base_url, api_key, model}`,并打印 `OPENWEBUI_*` 三行。

典型用法(零参数默认导入 `tools/`):
```cmd
python scripts\openwebui_bootstrap.py --from-config configs\config_openwebui.json ^
  --base-url http://localhost:8088 --model deepseek-v4-flash
```

---

## 5. 端到端流程(Windows)

```cmd
REM 1) 起 open-webui(先在 bat 里填好上游端点)
d:\code\open-webui-main\start_openwebui.bat

REM 2) 引导:等就绪 → 导入 tools/ 下工具 → 拿 key → 写 main+evaluator 配置
cd /d d:\code\openclaw-task
set OPENWEBUI_FEATURES=web_search   REM 需要内置搜索时
python scripts\openwebui_bootstrap.py --from-config configs\config_openwebui.json ^
  --base-url http://localhost:8088 --model deepseek-v4-flash

REM 3) 跑 harness
python harness_automation.py --harness openwebui --config configs\config_openwebui.json
```

产物:
- 运行日志 `logs\config_openwebui.log`
- 评估轨迹(启用 evaluator 时)`logs\trajectories\<run_id>\<session_name>.json`

---

## 6. 关键事实与踩坑记录

| 现象 | 结论 |
|---|---|
| `localhost:8080` 打到 VS Code | VS Code 占了 `127.0.0.1:8080`;open-webui 改用 **8088** |
| `OAUTH_SESSION_TOKEN_ENCRYPTION_KEY is not set` | `WEBUI_SECRET_KEY` 为空所致;bat 里已设固定值 |
| 生成 API key 返回 403 | 变量名应为 `ENABLE_API_KEYS`(复数)且默认 False;bootstrap 已支持 JWT 回退 |
| 打开网页 `{"detail":"Not Found"}` | 前端未构建,后端只提供 API;需 `npm run build` 或用 Docker 镜像 |
| API 调用工具不生效 | open-webui 对 API 不自动套工具,必须请求带 `tool_ids`/`features`(已由客户端自动做) |
| 工具"放目录自动加载"? | open-webui 工具存 DB,无目录扫描;由 bootstrap `POST /create` 导入实现 |

---

## 7. 已知限制 / 后续可做

- **skill/config MD 注入 system prompt**:尚未做(本轮聚焦 tools)。openclaw/hermes 靠运行时读
  workspace,openwebui 需客户端主动拼进 system prompt——后续单独实现。
- **evaluator 也带工具**:现 main 与 evaluator 都走 open-webui,都会自动带上已注册工具。若不希望
  evaluator 调工具(避免它不吐 JSON),需按 agent 名做排除(未做)。
- **工具更新**:bootstrap 导入幂等,同 id 不覆盖;更新工具内容需走 UI 或 `/id/{id}/update`(未做)。
- **凭据安全**:`user_proxy_model.json`、`start_openwebui.bat` 含明文 key,勿入公共版本库。
- **工具真正可执行**的前提:open-webui 侧已配好(web_search 需搜索引擎;工具的 Python 依赖已装)、
  上游模型支持 function calling。本适配只负责把开关按到请求里。
