# v1.0.3 修复说明

## 🐛 问题

运行任务时报错：
```
✗ 执行失败: Agent.execute() got an unexpected keyword argument 'timeout'
```

---

## 🔍 原因分析

### Agent.execute() 的正确签名

```python
# openclaw-sdk/src/openclaw_sdk/core/agent.py
async def execute(
    self,
    query: str,
    options: ExecutionOptions | None = None,  # ← 通过这个传递 timeout
    callbacks: list[CallbackHandler] | None = None,
    idempotency_key: str | None = None,
) -> ExecutionResult:
```

### ExecutionOptions 的定义

```python
# openclaw-sdk/src/openclaw_sdk/core/config.py
class ExecutionOptions(BaseModel):
    timeout_seconds: int = Field(default=300, ge=1, le=3600)  # ← 注意字段名
    stream: bool = False
    max_tool_calls: int = Field(default=50, ge=1, le=200)
    attachments: list[Attachment | str | Path] = Field(default_factory=list)
    thinking: bool | str = False
    deliver: bool | None = None
```

### 关键点

1. **不能直接传 timeout**
   ```python
   # ❌ 错误
   await agent.execute(query, timeout=300)
   ```

2. **需要通过 ExecutionOptions**
   ```python
   # ✅ 正确
   options = ExecutionOptions(timeout_seconds=300)
   await agent.execute(query, options=options)
   ```

3. **字段名是 timeout_seconds**
   - 不是 `timeout`
   - 是 `timeout_seconds`

---

## ✅ 解决方案

### 修复代码

```python
# 1. 导入 ExecutionOptions
from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions

# 2. 执行查询时创建 options
async def execute_queries(self, queries: List[QueryItem]):
    for query in queries:
        agent = self.agent_manager.get_agent(query.agent_name)

        # 创建执行选项
        options = ExecutionOptions(timeout_seconds=query.timeout) if query.timeout else None

        # 传递给 execute
        result = await agent.execute(
            query_text,
            options=options
        )
```

### 完整的修复位置

#### openclaw_automation.py

**导入部分** (第 11 行):
```python
from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions
from openclaw_sdk.core.types import ExecutionResult
```

**执行部分** (第 246-253 行):
```python
# 执行查询
try:
    # 创建执行选项（注意：字段名是 timeout_seconds）
    options = ExecutionOptions(timeout_seconds=query.timeout) if query.timeout else None

    result = await agent.execute(
        query_text,
        options=options
    )
```

---

## 📚 ExecutionOptions 详解

### 可用字段

| 字段 | 类型 | 默认值 | 范围 | 说明 |
|------|------|--------|------|------|
| `timeout_seconds` | int | 300 | 1-3600 | 执行超时（秒） |
| `stream` | bool | False | - | 是否流式返回 |
| `max_tool_calls` | int | 50 | 1-200 | 最大工具调用次数 |
| `attachments` | list | [] | - | 附件列表 |
| `thinking` | bool/str | False | - | 思维模式 |
| `deliver` | bool/None | None | - | 是否发送到渠道 |

### 使用示例

```python
# 基本用法
options = ExecutionOptions(timeout_seconds=600)

# 多个选项
options = ExecutionOptions(
    timeout_seconds=600,
    stream=False,
    max_tool_calls=100,
    thinking=True
)

# 传递给 execute
result = await agent.execute(
    "Your query here",
    options=options
)
```

---

## 🧪 测试验证

### 配置文件中的 timeout

配置文件中的 `timeout` 会被正确转换：

```json
{
  "queries": [
    {
      "agent_name": "test_agent",
      "text": "Hello",
      "timeout": 600  // 这个值会被传递给 ExecutionOptions(timeout_seconds=600)
    }
  ]
}
```

### 运行测试

```bash
cd C:\Users\nianzu\code

# 运行简单配置
python openclaw_automation.py config_simple.json
```

---

## 📊 版本对比

| 版本 | timeout 处理 | 结果 |
|------|-------------|------|
| v1.0.2 | `agent.execute(query, timeout=300)` | ❌ `unexpected keyword argument` |
| v1.0.3 | `agent.execute(query, options=ExecutionOptions(timeout_seconds=300))` | ✅ 正确 |

---

## 💡 最佳实践

### 1. 使用默认 timeout

如果不需要自定义 timeout，可以省略：

```python
# 使用默认值 300 秒
result = await agent.execute(query_text)
```

### 2. 自定义 timeout

```python
# 长时间任务
options = ExecutionOptions(timeout_seconds=1800)  # 30 分钟
result = await agent.execute(query_text, options=options)
```

### 3. 配置中设置 timeout

```json
{
  "queries": [
    {
      "agent_name": "researcher",
      "text": "Deep research on topic",
      "timeout": 1800  // 30 分钟
    }
  ]
}
```

### 4. 组合多个选项

```python
options = ExecutionOptions(
    timeout_seconds=600,
    max_tool_calls=100,
    thinking=True  # 启用思维模式
)
result = await agent.execute(query, options=options)
```

---

## 🎯 相关修复历史

### v1.0.0 → v1.0.1
- 修复：手动调用 `__aenter__()` 问题

### v1.0.1 → v1.0.2
- 修复：协程不支持 `async with` 问题

### v1.0.2 → v1.0.3
- 修复：`timeout` 参数传递问题 ✅ **当前版本**

---

## ✅ 修复确认

### 测试清单

- [x] 导入 `ExecutionOptions`
- [x] 修改 `execute()` 调用
- [x] 使用 `timeout_seconds` 字段
- [x] 更新 CHANGELOG
- [x] 创建修复文档

### 现在可以正常运行

```bash
cd C:\Users\nianzu\code
python openclaw_automation.py config_simple.json
```

应该不再有 timeout 相关的错误！

---

**修复版本**: v1.0.3
**修复日期**: 2026-03-06
**状态**: ✅ 已完成
**前置要求**: OpenClaw 实例正在运行
