# 更新日志

## v1.0.3 (2026-03-06)

### 🐛 Bug 修复

#### 修复 Agent.execute() 的 timeout 参数问题
**问题**: `Agent.execute() got an unexpected keyword argument 'timeout'`

**原因**:
- `Agent.execute()` 不直接接受 `timeout` 参数
- 需要通过 `ExecutionOptions` 对象传递
- 字段名是 `timeout_seconds` 而不是 `timeout`

**修复**:
```python
# ❌ v1.0.2 的错误方式
result = await agent.execute(query_text, timeout=query.timeout)

# ✅ v1.0.3 的正确方式
from openclaw_sdk import ExecutionOptions

options = ExecutionOptions(timeout_seconds=query.timeout)
result = await agent.execute(query_text, options=options)
```

**API 签名**:
```python
# Agent.execute() 的正确签名
async def execute(
    self,
    query: str,
    options: ExecutionOptions | None = None,
    callbacks: list[CallbackHandler] | None = None,
    idempotency_key: str | None = None,
) -> ExecutionResult
```

**ExecutionOptions 字段**:
- `timeout_seconds`: int (1-3600, 默认 300)
- `stream`: bool
- `max_tool_calls`: int
- `attachments`: list
- `thinking`: bool | str
- `deliver`: bool | None

**修改文件**:
- `openclaw_automation.py`:
  - 第 11 行: 导入 `ExecutionOptions`
  - 第 249-252 行: 创建 `ExecutionOptions` 并传递给 `execute()`

---

## v1.0.2 (2026-03-06)

### 🐛 Bug 修复

#### 修复协程上下文管理器问题
**问题**: `TypeError: 'coroutine' object does not support the asynchronous context manager protocol`

**原因**:
- `OpenClawClient.connect()` 是一个 async 方法，返回协程
- 协程本身不支持 `async with`
- 需要先 `await` 得到 `OpenClawClient` 实例
- 实例才实现了 `__aenter__` 和 `__aexit__`

**修复**:
```python
# ❌ v1.0.1 的错误方式
async with OpenClawClient.connect(**kwargs) as client:
    # TypeError: 协程不支持 async with
    ...

# ✅ v1.0.2 的正确方式
client = await OpenClawClient.connect(**kwargs)  # 先 await
async with client:  # 再 async with
    self.client = client
    # ... 执行操作 ...
```

**改进**:
- ✅ 正确理解 async 函数的返回值
- ✅ 分两步：先 await 协程，再 async with 实例
- ✅ 符合 Python 异步编程规范
- ✅ 实际测试通过

**修改文件**:
- `openclaw_automation.py`:
  - 第 346-349 行: 分两步处理连接

---

## v1.0.1 (2026-03-06)

### 🐛 Bug 修复

#### 资源管理改进 - 使用 async with
**问题**: OpenClawClient 未使用标准的 async with 上下文管理器

**修复**:
- 删除手动调用 `__aenter__()` 和 `__aexit__()`
- 使用 `async with` 语法（但方式有误）
- 这个版本仍有问题，已在 v1.0.2 修复

---

## v1.0.0 (2026-03-05)

### 🎉 初始版本

#### 核心功能
- ✅ 配置驱动的任务自动化
- ✅ 多 Agent 协作支持
- ✅ 工作空间自动管理
- ✅ 查询结果传递
- ✅ 技能自动安装
- ✅ 执行报告生成

#### 核心组件
- `AutomationConfig` - Pydantic 配置模型
- `WorkspaceManager` - 工作空间管理
- `AgentManager` - Agent 创建和管理
- `QueryOrchestrator` - 查询编排执行
- `OpenClawAutomation` - 主控制器
- `ConfigLoader` - 配置加载器

#### 文档
- `README.md` - 项目概述
- `DESIGN.md` - 详细设计文档
- `QUICKSTART.md` - 快速开始指南
- `PROJECT_SUMMARY.md` - 项目总结

#### 示例
- `examples.py` - 10 个实用示例
- `config_simple.json` - 简单配置
- `example_config.json` - 完整配置
- `config_code_review.json` - 代码审查流程

#### 测试
- `test_automation.py` - 12 个自动化测试

---

## 升级指南

### 从 v1.0.0 升级到 v1.0.1

无需更改配置文件或使用方式，这是一个内部改进。

只需替换 `openclaw_automation.py` 文件即可。

---

## 最佳实践建议

### 资源管理

✅ **推荐**: 让框架自动管理资源
```python
automation = OpenClawAutomation(config)
results = await automation.run()  # 自动处理连接和清理
```

❌ **不推荐**: 手动管理客户端
```python
# 除非有特殊需求，否则不要这样做
client = await OpenClawClient.connect().__aenter__()
# ... 使用 client ...
# 容易忘记调用 __aexit__()
```

### 长时间运行

如果需要长时间运行多个任务:

```python
# 为每个任务创建新的 automation 实例
for task_config in task_configs:
    automation = OpenClawAutomation(task_config)
    try:
        results = await automation.run()
    except Exception as e:
        print(f"任务失败: {e}")
    # automation.run() 的 finally 块会自动清理资源
```

### 调试连接问题

如果遇到连接问题，可以启用日志:

```python
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

---

## 已知问题

无

---

## 下一步计划

### v1.1.0 (计划中)

- [ ] 支持并行执行独立查询
- [ ] 添加查询执行重试机制
- [ ] 支持查询条件分支
- [ ] 添加执行前/后钩子
- [ ] 支持自定义结果处理器
- [ ] 添加性能指标收集
- [ ] 支持增量报告输出

### v1.2.0 (计划中)

- [ ] Web UI 控制台
- [ ] 任务调度支持
- [ ] 执行历史记录
- [ ] 结果缓存机制
- [ ] 支持 Agent 池管理

---

## 贡献

发现 Bug 或有改进建议？欢迎提交 Issue！

---

## 许可证

MIT License
