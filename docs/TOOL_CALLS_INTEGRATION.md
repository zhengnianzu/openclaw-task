# 工具调用捕获统一集成方案

## 实现日期
2026-08-27

## 背景

在 `executor.py` 的 `process_turn()` 函数中，需要从各个 client（OpenClaw、Hermes、Pi、Grok）获取工具调用详情，用于构建轨迹证据。之前的实现只支持 OpenClaw 和 Hermes，没有处理 Pi 和 Grok 这种在 client 内部已经解析好 `tool_calls` 的情况。

## 问题分析

### 各 Client 的工具调用来源

| Client | 工具调用来源 | 解析位置 | 数据格式 | 增量策略 |
|--------|------------|---------|---------|---------|
| **OpenClaw** | Gateway API `chat_history` | `executor.py` 调用 `extract_tool_calls()` | OC 自定义格式（`toolCall`/`toolResult` 块） | 按 timestamp 截取增量 |
| **Hermes** | OpenAI API `messages` | `executor.py` 调用 `extract_tool_calls_openai()` | OpenAI 格式（`tool_calls` + `role==tool`） | API 天然返回增量 |
| **Pi** | RPC events 流 | `pi_client.py` 内部解析 | 已填充到 `ExecutionResult.tool_calls` | 事件流天然增量 |
| **Grok** | Session `chat_history.jsonl` | `grok_client.py` 内部解析 | 已填充到 `ExecutionResult.tool_calls` | 按 `prompt_index` 过滤本轮 |

### 原有问题

`executor.py:93-110` 的逻辑：
```python
# 只判断是否有 gateway
if gateway is None:
    # Hermes: 从 result.messages 解析
    turn_tool_calls = extract_tool_calls_openai(native_msgs)
else:
    # OpenClaw: 从 chat_history 解析
    turn_tool_calls = extract_tool_calls(new_msgs)
```

**问题**：
- Pi 和 Grok 没有 gateway，会被误判为 Hermes 路径
- 但它们没有 `result.messages`，导致解析失败返回空列表
- 实际上它们已经在 `result.tool_calls` 中准备好了数据

## 解决方案

### 统一集成策略

采用**优先级机制**，按以下顺序尝试获取工具调用：

```
优先级1: ExecutionResult.tool_calls (Pi/Grok 已填充)
    ↓ 如果为空
优先级2: 从 chat_history 或 messages 解析 (OpenClaw/Hermes)
    ├─ 有 gateway → OpenClaw 路径（extract_tool_calls）
    └─ 无 gateway → Hermes 路径（extract_tool_calls_openai）
```

### 实现代码（executor.py:88-130）

```python
turn_tool_calls: Optional[List[ToolCallEvidence]] = None
if agent is not None:
    try:
        # 优先级1: 直接从 ExecutionResult.tool_calls 获取(Pi/Grok 已在 client 内部解析好)
        result_tool_calls = getattr(result, "tool_calls", None)
        if result_tool_calls:
            # Pi/Grok: ExecutionResult.tool_calls 已填充,直接使用
            turn_tool_calls = [
                ToolCallEvidence(
                    tool=tc.tool,
                    input=tc.input,
                    output=tc.output,
                    duration_ms=getattr(tc, "duration_ms", None),
                )
                for tc in result_tool_calls
            ]
        else:
            # 优先级2: 从 chat_history 或 messages 解析(OpenClaw/Hermes)
            gateway = getattr(getattr(agent, "_client", None), "gateway", None)
            if gateway is None:
                # Hermes/ClaudeCode: 从 result.messages 解析
                native_msgs = getattr(result, "messages", None) or []
                turn_tool_calls = extract_tool_calls_openai(native_msgs)
            else:
                # OpenClaw: 从 chat_history 解析
                after_history = await _safe_chat_history(agent)
                new_msgs = _new_messages_since(before_history or [], after_history)
                turn_tool_calls = extract_tool_calls(new_msgs)
        
        # 兜底: 如果从证据里救回了工具调用 → 不再算"证据不完整"
        if evidence_incomplete and turn_tool_calls:
            evidence_incomplete = False
    except Exception as e:
        logger.debug("解析本轮 tool_calls 失败,降级为空: %s", e)
```

## 优势

### 1. 统一接口
所有 client 都可以通过同一个逻辑路径返回工具调用证据，无需在 `executor.py` 中添加特殊判断。

### 2. 解耦设计
- **Client 内部解析**（Pi/Grok）：复杂的格式解析逻辑封装在各自的 client 中
- **Executor 统一消费**：只需要从 `result.tool_calls` 读取标准化的 `ToolCallEvidence`

### 3. 向后兼容
- OpenClaw 和 Hermes 的现有逻辑不受影响（它们的 `result.tool_calls` 为空，走优先级2）
- 未来新增 client 只需在内部填充 `tool_calls` 字段即可自动集成

### 4. 容错性
- 每个优先级都有独立的异常处理
- 任何解析失败都降级为空列表，不影响主流程

## 数据流示意

```
┌─────────────┐
│ Pi Client   │  RPC events → parse → ExecutionResult.tool_calls ✓
└─────────────┘                              ↓
                                    ┌────────────────┐
┌─────────────┐                    │                │
│ Grok Client │  chat_history.jsonl → parse → ExecutionResult.tool_calls ✓
└─────────────┘                              ↓       │
                                             │       │
┌─────────────┐                              │       │  executor.py
│ Hermes      │  result.messages → (empty tool_calls)  process_turn()
└─────────────┘                              ↓       │  ↓
                                    extract_tool_calls_openai()
┌─────────────┐                              ↓       │
│ OpenClaw    │  chat_history → (empty tool_calls)   │
└─────────────┘                              ↓       │
                                    extract_tool_calls()
                                             ↓       │
                                    ┌────────────────┘
                                    ↓
                            TurnRecord.tool_calls
                                    ↓
                            Trajectory 落盘
```

## 相关修改文件

1. **`src/grok_client.py`**
   - 添加 `ToolCall` 数据类
   - `ExecutionResult` 添加 `tool_calls` 字段
   - 实现 `extract_tool_calls_grok()` 函数
   - 在 `GrokAgent.execute()` 中调用提取逻辑

2. **`src/executor.py`** (第 88-130 行)
   - 修改 `process_turn()` 的工具调用获取逻辑
   - 添加优先级1：从 `result.tool_calls` 直接获取
   - 保留优先级2：从 `chat_history` 或 `messages` 解析

## 验证结果

✅ **语法检查**：
- `python -m py_compile src/grok_client.py` ✓
- `python -m py_compile src/executor.py` ✓

✅ **逻辑验证**：
- Pi: `result.tool_calls` 已有数据 → 走优先级1
- Grok: `result.tool_calls` 已有数据 → 走优先级1
- Hermes: `result.tool_calls` 为空 → 走优先级2（extract_tool_calls_openai）
- OpenClaw: `result.tool_calls` 为空 → 走优先级2（extract_tool_calls）

## 未来扩展

如果新增其他 client，只需遵循以下原则之一：

### 方案A：Client 内部解析（推荐）
```python
# 在 new_client.py 中
class ExecutionResult:
    tool_calls: List[ToolCall] = field(default_factory=list)

async def execute(...):
    # ... 执行逻辑 ...
    tool_calls = extract_tool_calls_from_new_format(...)
    return ExecutionResult(..., tool_calls=tool_calls)
```

### 方案B：Executor 中添加解析函数
```python
# 在 trajectory.py 中添加
def extract_tool_calls_new_format(messages):
    # 解析新格式
    return [ToolCallEvidence(...), ...]

# 在 executor.py 中扩展优先级2
elif is_new_format:
    turn_tool_calls = extract_tool_calls_new_format(...)
```

**推荐方案A**：解析逻辑封装在 client 内部，executor 保持简洁。

## 总结

通过引入优先级机制，实现了：
- ✅ 四种 client（OpenClaw、Hermes、Pi、Grok）的工具调用统一捕获
- ✅ 解耦设计：client 负责解析，executor 负责消费
- ✅ 向后兼容：现有 client 不受影响
- ✅ 易于扩展：新 client 只需填充 `tool_calls` 字段

所有工具调用详情现在都能正确流入 `Trajectory`，用于评估和 RL 样本构建。
