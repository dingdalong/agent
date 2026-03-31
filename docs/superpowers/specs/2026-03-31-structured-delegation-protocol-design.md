# 结构化委托协议设计

## 背景与目标

### 问题

当前多 Agent 协作中，委托任务通过 `DelegateToolProvider` 传递，但请求只有一个 `task: str` 字段。接收方 Agent 拿到的是一句模糊的自然语言，缺少目标、上下文和期望结果，导致：

1. 接收方可能误解任务意图，返回偏离预期的结果
2. 多级委托（A → B → C）中，最终目标逐级丢失
3. 信息不足时，LLM 倾向于猜测而非报告缺失

### 目标

通过结构化委托流程（非结构化 Agent），确保：

1. 发送方被强制说清楚 **做什么、为什么、已知信息、期望结果**
2. 接收方拿到充分上下文，能自主执行或明确报告缺失信息
3. 返回结果干净，只有结果，不回传冗余上下文
4. 与现有架构兼容，改动最小

### 不做的事情

- 不改 `messages: list[dict]` 格式 — 那是 LLM API 层
- 不在 Agent 模型上加 input_contract / output_contract — Agent 保持自主性，不退化为 Tool
- 不建 `src/protocols/` 模块 — 改动集中在 delegate 一个文件
- 不改 handoff 机制 — handoff 是图引擎的节点跳转，与委托协议是不同的事
- 不引入给代码解析的 output_schema / data — 结构化数据提取是工具层已有的能力

## 核心设计

### 设计原则

- **结构化委托流程，不是结构化 Agent** — 约束的是"怎么委托"，不是"Agent 接受什么"
- **Prompt 模板 + Function Calling 双重保障** — 模板引导 LLM 想清楚，schema 强制输出结构
- **混合式强制** — 代码需要解析的地方（委托参数）用 function calling 强制；面向用户的回复用 prompt 约定
- **协议是 LLM-to-LLM 的语义通道** — 所有字段都是给 LLM 理解用的自然语言，不是给代码解析的 JSON Schema

### 委托 Schema（发送方）

将 `DelegateToolProvider.get_schemas()` 生成的 function calling schema 从单一 `task: str` 扩展为四个字段：

```python
DELEGATE_DESCRIPTION_TEMPLATE = (
    "委托任务给{description}专家。"
    "请基于当前对话上下文，清晰完整地填写以下字段，"
    "确保对方无需额外信息就能执行任务。"
)

# 每个 delegate_<agent_name> 的 schema
{
    "type": "function",
    "function": {
        "name": "delegate_{agent_name}",
        "description": DELEGATE_DESCRIPTION_TEMPLATE.format(description=agent.description),
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "你的最终目标是什么（为什么需要这次委托）",
                },
                "task": {
                    "type": "string",
                    "description": "你需要对方具体做什么",
                },
                "context": {
                    "type": "string",
                    "description": "当前已知的相关信息。只填你确定知道的，不要猜测。",
                },
                "expected_result": {
                    "type": "string",
                    "description": "你期望对方完成后告诉你什么。如果不确定，可简要描述即可。",
                },
            },
            "required": ["objective", "task"],
        },
    },
}
```

**设计决策：**

- `objective` 和 `task` 必填 — 没有目标和任务就不应该委托
- `context` 和 `expected_result` 可选 — 避免信息不足时 LLM 被迫编造内容
- 字段 description 中包含 "不要猜测" — 降低 LLM 填充幻觉的倾向

### 接收方模板

`DelegateToolProvider.execute()` 用模板组装接收方 Agent 的 input，替代现在的裸 `task` 字符串：

```python
RECEIVING_TEMPLATE = (
    "你收到了一个委托任务：\n"
    "最终目标：{objective}\n"
    "具体任务：{task}\n"
    "{context_line}"
    "{expected_result_line}"
    "\n"
    "请完成任务并直接返回结果。\n"
    "如果信息不足以完成任务，请明确列出缺少的信息，不要猜测或假设。"
)
```

- `context_line` / `expected_result_line`：有值时显示 `"相关上下文：{context}\n"`，无值时不显示
- 最后一句确保接收方在信息不足时**报告缺失**而非编造

### ID 关联

复用现有 `tool_call_id` 机制。因为 delegate Agent 已被包装为 function calling 工具，整个委托链路天然走 tool_call 协议：

```
AgentA 的 LLM 调用 delegate_weather(...)   → 产生 tool_call_id
DelegateToolProvider.execute()              → 内部驱动 Weather Agent
返回 result.text                            → 带着同一个 tool_call_id 回到 AgentA
```

不需要额外设计 UUID。

### 返回值

`execute()` 仍然返回 `str`（`result.text`），保持 `ToolProvider` 协议兼容。返回内容只包含结果，不回传委托上下文。

## 场景示例

### 场景 1：正常委托

```
用户："明天去故宫，天气怎么样？"

→ Triage Agent 决定委托，function calling 输出：
  delegate_weather(
    objective="判断明天是否适合去故宫游玩",
    task="查询北京明天的天气预报",
    context="用户计划明天去故宫，需要天气来决定是否出行",
    expected_result="天气状况、温度、是否适合户外活动"
  )

→ Weather Agent 收到：
  "你收到了一个委托任务：
   最终目标：判断明天是否适合去故宫游玩
   具体任务：查询北京明天的天气预报
   相关上下文：用户计划明天去故宫，需要天气来决定是否出行
   期望结果：天气状况、温度、是否适合户外活动

   请完成任务并直接返回结果。
   如果信息不足以完成任务，请明确列出缺少的信息，不要猜测或假设。"

→ Weather Agent 返回："北京明天晴，最高25°C，微风，非常适合户外游览。"
→ Triage Agent 拿到结果，继续向用户回复。
```

### 场景 2：信息不足 — Triage 能判断

```
用户："帮我查天气"

→ Triage Agent 判断缺少地点和日期，直接向用户提问：
  "请问您要查哪个城市、哪天的天气？"

（不发生委托，协议不介入）
```

### 场景 3：信息不足 — 需要专业 Agent 判断

```
用户："查天气"

→ Triage Agent 无法判断是否信息充足（不是天气领域专家）
→ 委托：
  delegate_weather(
    objective="帮用户查询天气",
    task="查询天气预报"
  )
  （context 和 expected_result 未填，因为确实不知道更多）

→ Weather Agent 收到任务，判断缺少必要信息
→ 返回："无法完成任务，需要以下信息：1.查询地点（城市名称） 2.查询日期"

→ 该结果作为 tool result 回到 Triage Agent 的 messages
→ Triage Agent 的 LLM 理解后向用户提问：
  "请问您要查哪个城市、哪天的天气？"
```

### 场景 4：多级委托（目标传递）

```
Triage → Travel Agent：
  delegate_travel(
    objective="帮用户规划明天的故宫行程",
    task="制定完整的出行方案",
    context="用户明天要去故宫",
    expected_result="包含天气、交通、门票的出行方案"
  )

Travel Agent → Weather Agent：
  delegate_weather(
    objective="制定故宫出行方案，需要天气信息",
    task="查询北京明天的天气",
    context="这是出行方案的一部分，需要天气来决定穿着和时间安排",
    expected_result="天气状况、温度、适合出行的时间段"
  )

objective 随委托链向下传递，每一级 Agent 都知道最终目标。
```

## 改动范围

| 文件 | 改动 |
|------|------|
| `src/tools/delegate.py` | `get_schemas()` 使用新的四字段 schema 模板；`execute()` 使用接收方模板组装 input |

**不改动的文件：**

- `src/agents/agent.py` — Agent 模型不变
- `src/agents/runner.py` — 工具调用循环不变
- `src/llm/` — LLM 接口不变
- `src/graph/` — 图引擎不变
- `src/tools/router.py` — 路由逻辑不变

## 向后兼容

- 现有的 `delegate_<name>(task="...")` schema 变为 `delegate_<name>(objective="...", task="...", ...)`
- LLM 下次调用自动按新 schema 填字段，无需迁移
- `execute()` 返回值类型不变（`str`），`ToolProvider` 协议兼容
- 现有 Agent 定义不需要任何修改

## 建议：专业 Agent 的 instructions 配合

协议模板提供通用兜底（"如果信息不足...不要猜测"），建议在编写专业 Agent 时也加入领域相关的约束：

```python
weather_agent = Agent(
    name="weather",
    instructions="你是天气查询专家...如果缺少必要信息（地点、日期），"
                 "请明确告知需要什么信息，不要假设默认值。",
    ...
)
```

双重保障：协议模板管通用行为，Agent instructions 管领域行为。
