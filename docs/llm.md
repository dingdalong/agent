# LLM 层

本文档说明 `src/llm/` 的统一 provider 抽象、错误分类与重试、流式响应协议、消息归一化、模型发现及五个 provider 的差异。模型配置和解析见 [managers.md](managers.md)，完整配置键见 [configuration-reference.md](configuration-reference.md)，Agent 对终态错误的处理见 [agent-runtime.md](agent-runtime.md)，事件展示见 [events-and-ui.md](events-and-ui.md)。

## 1. Provider 注册与公共响应

`src/llm/__init__.py:17-23` 的静态注册表包含五个实现：

| 名称 | 实现 | API |
|---|---|---|
| `anthropic` | `AnthropicProvider` | Anthropic Messages API |
| `openai` | `OpenAIProvider` | OpenAI Responses API |
| `deepseek` | `DeepSeekProvider` | OpenAI 兼容 Chat Completions |
| `ollama` | `OllamaProvider` | OpenAI 兼容 Chat Completions |
| `moonshot` | `MoonshotProvider` | OpenAI 兼容 Chat Completions，适配 Kimi K3 |

`get_provider(name)` 按精确名称返回实现类，未知名称抛 `ValueError`（`src/llm/__init__.py:25-29`）。模型到 provider 的归属由 `LLMMgr.load_models()` 建立，本层只接收已经解析的模型 ID。

`LLMResponse`（`src/llm/base.py:168-176`）统一五家的返回值：

| 字段 | 含义 |
|---|---|
| `content` | 不含思考文本的正文 |
| `tool_calls` | 按流索引聚合的工具调用，元素含 `id`、`name`、`arguments` |
| `finish_reason` | 归一后的 `stop`、`tool_calls`、`length`；Anthropic 还可返回协议续接终态 `pause_turn` |
| `assistant_message` | 可直接回填历史的完整 assistant 消息，允许携带 provider 专属往返字段 |
| `token_usage` | 统一 token 用量 |
| `has_partial_data` | 本次成功尝试是否曾接收正文、思考或工具片段 |
| `truncation_kind` | 仅 `finish_reason="length"` 时非空；`classify_truncation` 按 **工具 → 正文 → 思考 → 未知**（`tool_call`/`content`/`thinking`/`unknown`）判定的截断阶段，供 Agent 恢复链选择续写或丢弃重生成 |

`classify_truncation(response, call=None)` 与 `_has_reasoning_carrier(assistant_message)`（`src/llm/base.py`）集中处理跨 provider 的截断分类：`has_tool` 看 `tool_calls` 或 `call.tool_fragment_state`；`has_content` 看正文或 `call.response_parts`；`has_thinking` 看各家推理载体（`reasoning_content` / `reasoning` 文本、Anthropic `_anthropic_content` 的 `thinking` 块、OpenAI `_response_output` 的 `reasoning` 项）或 `call.thinking_parts`。`chat()` 是唯一同时持有成品 `LLMResponse` 与 `LLMCallContext` 的位置，故分类只在此计算，五个 provider 的 `_build_response` 无需改动。

token 用量统一为 `input_tokens`、`output_tokens`、`total_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`。Anthropic 将未缓存输入、缓存读取和缓存创建相加作为统一 `input_tokens`；其余实现按各 SDK 的输入总量字段归一（`src/llm/anthropic.py:202-229`、`src/llm/openai.py:139-165`）。

## 2. `LLMProvider` 与模板方法

`LLMProvider` 是 `@dataclass` 抽象基类（`src/llm/base.py:307-326`）。主要构造字段如下：

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `api_key`、`base_url`、`model`、`event_bus` | 必填 | 连接信息、精确模型 ID 与事件总线 |
| `concurrency` | `5` | 同一 provider 实例的并发信号量上限 |
| `max_attempts` | `3` | 最大尝试次数，包含首次调用 |
| `base_delay_seconds` | `2.0` | 指数退避基础秒数 |
| `max_delay_seconds` | `60.0` | 单次等待封顶秒数 |
| `timeout` | `120.0` | SDK 请求超时秒数 |
| `context_limit` | `0` | 模型上下文窗口；非正值表示未知 |
| `page_token_rate` | `0.03` | 单页工具结果占上下文窗口的比例 |
| `reasoning_effort` | `"max"` | provider 共享的默认推理力度（agent 未声明 `reasoning_effort` 时的最终回退）；**per-agent 覆盖与按调用降档只经 `reasoning_effort_override` 参数传递，绝不修改此共享字段**（provider 被缓存并跨子 agent 共享） |
| `preserve_thinking` | `False` | Ollama 历史思考保留开关 |
| `max_pause_turn_continuations` | `0` | 协议续接上限；仅 Anthropic 从配置读取正整数，内置默认值为 `5` |

`__post_init__` 创建信号量、计算 `page_token_budget = max(1, floor(context_limit × page_token_rate))`，并构造 `RetryPolicy`（`src/llm/base.py:328-340`）。`protocol_continuation_limit(finish_reason)` 是 Agent 查询协议续接预算的统一接口：基类及非 Anthropic provider 返回 `0`，Anthropic 对 `pause_turn` 返回实例配置值（`src/llm/base.py:342-351`、`src/llm/anthropic.py:158-169`）。协议续接会产生新的完整 LLM 调用，与同一次调用内部的网络重试次数相互独立。

**推理力度降档阶梯**是单一真源：基类类属性 `_EFFORT_DOWNGRADE: ClassVar[dict[str, str]] = {}` 与方法 `next_lower_effort(current) -> str | None`（返回 `_EFFORT_DOWNGRADE.get(current)`）。各 provider 只覆写字典（pre-map 词表，Anthropic 的档位另经 `_map_effort` 二次映射）：

| provider | 降档阶梯 |
|---|---|
| OpenAI | `max→xhigh→high→medium→low` |
| Anthropic / Ollama | `max→high→medium→low` |
| DeepSeek | `max→high` |
| Moonshot | 空（`{}`）→ `next_lower_effort` 恒 `None` → 思考截断直接走一次性压缩指令 |

底层 SDK 的内建自动重试在五个调用客户端及模型发现客户端上均关闭，值固定为 0；调用级重试只由基类统一控制（`src/llm/base.py:388-394`、五个 provider 的 `__post_init__`）。

`chat()`（`src/llm/base.py` 的 `chat`）是唯一公共调用入口，采用模板方法：

1. 构造 `effective_messages`：`ephemeral_instruction` 非空时在尾部追加一条一次性 `user` 指令（用 `user` 角色避开 `normalize_messages` 的 developer 门控），**不改动调用方 `messages` 列表**；较“append 后回滚”更稳、天然一次性且并发安全。
2. 在信号量内按 `1..max_attempts` 创建独立 `LLMCallContext`。
3. 发出 `LLMCallStarted`，再调用子类 `_do_chat(..., reasoning_effort_override=...)`。
4. `finish_reason=="length"` 时用 `classify_truncation(response, call)` 计算并写入 `response.truncation_kind`（其余终态不设）。
5. 成功后补齐工具片段完成态，发出 `LLMCallCompleted` 并返回 `LLMResponse`。
6. 异常由统一分类器转换成 `LLMErrorInfo`；不可重试或尝试耗尽时发出 `LLMCallFailed`，抛出 `LLMCallError`。
7. 可重试时计算等待时间、发出 `LLMRetrying`、异步等待，再以全新的尝试上下文重试。

`chat()` 另有两个默认 `None` 的按调用参数：`reasoning_effort_override`（临时替换本次调用的推理力度档位，不改共享 `reasoning_effort`）与 `ephemeral_instruction`（见步骤 1）。`reasoning_effort_override` 现由两条来源驱动：其一是 **per-agent 推理力度**——`Agent._on_llm_call` 用 `ctx.length_effort_override or self.reasoning_effort` 取值，`self.reasoning_effort` 源自 role.md / 子 agent frontmatter 的 `reasoning_effort` 字段（子 agent 未声明时继承父 agent 已解析值，主 agent 未声明为 `None` → 退回 provider 共享档位），见 [roles-subagents-skills.md](roles-subagents-skills.md)；其二是**长度恢复降档**——`ctx.length_effort_override` 由恢复链经 `next_lower_effort()` 从 `_base_reasoning_effort()`（即 `self.reasoning_effort or self.llm.reasoning_effort`）起步逐级降档。两参默认 `None`，故退出总结、compact 等调用不受影响。

`CancelledError`、`KeyboardInterrupt`、`SystemExit` 始终原样传播。事件发布调用 `emit_telemetry_safely()`，普通遥测发布故障不会改变 LLM 调用结果，控制流异常仍传播（`src/events/bus.py:36-65`）。

### `LLMCallContext`

每次尝试独占一个 `LLMCallContext`（`src/llm/base.py:48-159`），记录：

- `attempt` 与调用方 `caller_agent_type` / `caller_uuid`；
- 正文 `response_parts`、思考 `thinking_parts`；
- 按流索引累积的 `tool_fragments` 与 `tool_fragment_state`（`none` / `partial` / `complete`）；
- `partial_output`、`partial_thinking`、`has_partial_data` 派生属性。

因此重试后的正文、思考和工具片段不会与上一尝试合并。`LLMCallError.partial_output` 仅保存最后一次失败尝试已接收的正文；用户可见转录则由事件边界明确分段。

### 子类契约

子类必须实现：

- `estimate_tokens(messages, prompt=None, tools=None)`：按该 provider 实际请求形态估算完整输入；
- `_do_chat(..., reasoning_effort_override=None, call=LLMCallContext)`：只执行一次 provider 调用，不自行重试，返回已归一并校验的 `LLMResponse`。请求构建处用 `reasoning_effort_override or self.reasoning_effort` 取本次力度（Anthropic 再经 `_map_effort`），从不写回共享字段（`src/llm/base.py` 的 `_do_chat` 抽象声明）。

## 3. 统一错误域

`src/llm/errors.py` 定义统一错误域：

- `LLMErrorKind`：稳定分类枚举；
- `LLMErrorInfo`：安全摘要、是否可重试、HTTP 状态、provider code、request ID、重试响应头和原始异常类型（`errors.py:52-73`）；
- `LLMCallError`：调用终态异常，携 `info`、实际尝试次数、最后尝试的部分正文和本地 `diagnostic_id`（`errors.py:76-105`）；
- `LLMConfigurationError`：调用前即可确定的配置终态错误，尝试次数为 0（`errors.py:108-127`）；
- `LLMStreamResponseError`：流缺少合法终态、解析失败或违反 provider 协议（`errors.py:130-159`）。

`LLMErrorKind` 的完整集合（`errors.py:22-40`）：

| 类别 | 自动重试 |
|---|---|
| `network`、`timeout`、`rate_limit`、`service`、`response_protocol` | 是 |
| `authentication`、`permission`、`billing_quota` | 否 |
| `bad_request`、`not_found`、`payload_too_large`、`unprocessable` | 否 |
| `context_limit`、`output_limit`、`content_policy`、`unknown` | 否 |

### 固定分类优先级

`classify_llm_error()` 保留控制流异常，并按固定顺序分类（`src/llm/errors.py:275-373`）：

1. 已是 `LLMCallError` 时直接复用安全 `info`；
2. 顶层异常的 provider `code` / `type`；
3. 顶层 SDK 异常类型；
4. 顶层 HTTP 状态码；
5. `cause` / `context` 链中逐层重复结构化 code、SDK 类型和状态码判断；
6. 异常链中保守的语义文本信号；
7. `unknown`。

provider code 内部先识别上下文、输出、内容政策和额度语义，再识别限流、服务、认证、权限、资源与请求错误（`errors.py:376-415`）。HTTP 映射覆盖 400、401、402、403、404、408、409、413、422、429、529 与 5xx（`errors.py:463-494`）。语义文本只作为后置兜底，识别明确的上下文/输出/内容政策、超时和网络信号（`errors.py:521-575`）。

### 安全日志与诊断字段

分类器只提取受控的结构化 `message`、code、状态码、常见 request ID 与 `Retry-After` 元数据；消息会去除换行、凭据和 URL userinfo，并限制长度（`src/llm/errors.py:640-765,824-878`）。检测到响应正文时不会把正文写入摘要。调用失败日志不记录请求、响应体或凭据；已知类别记安全字段，未知类别使用仅含安全消息的异常包装保留 traceback。异常的 traceback getter 本身失效时安全降级为空堆栈，控制流异常仍原样传播（`src/llm/base.py:778-821`、`src/llm/errors.py:787-800`）。

`LLMRetrying` 与 `LLMCallFailed` 同样只携安全摘要、有限元数据、片段状态和调用方身份，不携原始异常对象（`src/llm/base.py:887-951`）。

## 4. 重试与等待时间

`RetryConfig` 自身执行严格校验（`src/llm/retry.py:15-52`）：`max_attempts` 必须是非 bool 的 `int` 且大于等于 1；两项延迟必须是非 bool 的 `int | float`、有限且大于 0；`max_delay_seconds` 不得小于基础延迟。错误类型、字符串、bool、NaN、Infinity、零和负数都会以 `ValueError` 拒绝。`max_attempts` 包含首次，因此配置为 1 表示只调用一次；`LLMMgr` 在构造 provider 前执行同等规则的配置级校验。

`RetryPolicy.should_retry()` 只在错误分类可重试且当前尝试尚未达到上限时返回真（`retry.py:79-89`）。等待时间优先级（`retry.py:110-151`）：

1. 合法且有限的 `retry-after-ms`，换算为秒；
2. 合法且有限的 `Retry-After` 秒数或 HTTP date；
3. 指数退避 `base_delay_seconds × 2^(attempt-1)`，乘以 `[0.75, 1.0]` 的随机抖动；
4. 最终夹在 `[0, max_delay_seconds]`。

响应头等待时间也受最大延迟封顶。每次失败在等待前发 `LLMRetrying`，下一次调用会再发新的 `LLMCallStarted`。

## 5. 流式响应协议

五个 provider 都通过 `iter_llm_stream()` 迭代 SDK 流；数据帧中途的 `EOFError` 转成 `LLMStreamResponseError`（`src/llm/base.py:285-305`）。

Chat Completions 风格的通用校验（`base.py:179-283`）要求：

- 必须有允许的终态；内容政策终态直接失败；
- 首个 `finish_reason` 锁定业务终态；之后只允许 `choices=[]` 的 usage 尾块，任何后置 choice 或 delta 都是响应协议错误；
- `tool_calls` 终态必须有工具调用，`stop` 终态不得携工具调用；
- 非 `length` 响应的每个工具调用必须有非空且唯一的 ID、非空名称、JSON object 参数。

`length` 允许保留尚未完整的工具片段，交给 Agent 的 `LENGTH_RETRY` 丢弃并重新生成；其他终态都必须通过完整工具校验。

各 provider 还验证自身终态：

- Anthropic 必须先收到 `message_stop`，之后不得有额外事件，且必须取得最终消息；`stop_reason` 映射为 `stop` / `tool_calls` / `length` / `pause_turn`，上下文、拒绝和内容政策终态转统一错误。parser 对最终 `content` 的每个 SDK block 调用 `model_dump(exclude_none=True)` 原样保存；空 block 列表或含客户端 `tool_use` 的 `pause_turn` 被视为协议错误，防止构造无法续接的载体（`src/llm/anthropic.py:501-579,581-695`）。
- OpenAI Responses API 只接受一个合法的 `completed` 或 `incomplete` 终态，终态后不得出现事件；未知工具输出项、失败事件、未知 incomplete 原因和终态前 EOF 都转协议错误。流中的 `response.refusal.delta` / `done` 以及仅出现在最终嵌套 output 中的 refusal block 都转为 provider code `refusal`，统一分类为非重试 `content_policy`，不会返回空的成功响应或泄露拒绝正文（`src/llm/openai.py:58-94,300-466`）。
- DeepSeek、Moonshot、Ollama 允许 `stop` / `length` / `tool_calls`，流结束后统一校验；DeepSeek 的 `insufficient_system_resource` 转 `service`（`src/llm/deepseek.py:151-227`）。

## 6. 消息归一化与分页

`normalize_messages()`（`src/llm/base.py:453-660`）规范 role、content、assistant 工具调用和 tool 响应，并校验工具往返序列：调用 ID 必须非空且唯一，紧随的 tool 消息必须逐一且只响应一次。默认模式会删除无正文的非法 assistant 工具组及其 tool 消息；有正文时降级为纯文本 assistant。`strict=True` 时直接抛出类型或值错误。

assistant 的 provider 专属字段在判断“真正为空”之前由 `_normalize_assistant_extra()` 保存（`src/llm/base.py:512-544`）。因此 OpenAI `_response_output`、Anthropic `_anthropic_content`，以及 DeepSeek、Ollama、Moonshot 的 reasoning-only carrier 即使正文为空也会保留；没有正文、工具调用或任何专属载体的空 assistant 才会删除。

工具结果分页由 `split_page()` / `_split_page_once()`（`src/llm/base.py:415-440`）提供：先按 provider token 估算判断整段是否可用，超预算时二分查找最大可容纳前缀，直至无损切完。实际分页缓存和读取由 `ToolsMgr` 完成。

## 7. 五个 provider 的差异

| 维度 | Anthropic | OpenAI | DeepSeek | Ollama | Moonshot |
|---|---|---|---|---|---|
| 请求 API | Messages | Responses | Chat Completions | Chat Completions | Chat Completions |
| 结构化输出标记 | 是 | 是 | 否 | 否 | 否 |
| token 估算 | `cl100k_base`，失败时字符估算 | 模型编码，未知模型用 `o200k_base` | 本地 tokenizer | 字符估算 | 字符估算 |
| 历史专属字段 | `_anthropic_content` | `_response_output` | `reasoning_content`、`prefix` | `reasoning`、`reasoning_content` | `reasoning_content` |
| temperature | 不下发 | 下发 | 下发 | 下发 | 不下发 |
| 输出上限 | 固定传入 128,000 | 不下发（provider 默认） | 不下发（provider 默认） | 不下发（provider 默认） | 不下发（provider 默认） |

实现要点：

- Anthropic 转换 system、messages、tools 和 tool choice，合并连续同角色消息；历史 `_anthropic_content` 使用深拷贝原样往返。稳定 system 与最新消息设置缓存断点，但只向 SDK 明确允许 `cache_control` 的 block 类型写入该字段；思考结束后可从历史剥离（`src/llm/anthropic.py:231-403`）。
- OpenAI 把 system/developer 合并为 Responses API 首条 developer input；`prompt_cache_key` 使用模型与 agent 类型构造稳定键；历史用 `_response_output` 原样往返（`src/llm/openai.py:132-259`）。
- DeepSeek 支持 `prefix`，开启思考时下发 `reasoning_effort` 与 thinking enabled，关闭时显式 disabled；工具增量伴随的纯空白正文不计入内容（`src/llm/deepseek.py:83-149,151-252`）。
- Ollama 可把 developer 归为 system，同时兼容 `reasoning` 与 `reasoning_content`；`preserve_thinking` 控制 chat template 历史思考保留（`src/llm/ollama.py:79-147`）。
- Moonshot/Kimi K3 保留跨轮 `reasoning_content`，带工具调用的 assistant 即使无思考正文也补空字符串；不下发 temperature，思考力度走顶层字段（`src/llm/moonshot.py:114-175,240-276`）。

`enable_thinking=False` 时，Anthropic 显式 disabled，OpenAI 不传 reasoning，DeepSeek 显式 disabled，Ollama 关闭 chat template thinking，Moonshot 不传 reasoning effort。此时各 provider 都不下发推理力度，故 `reasoning_effort_override` 对本次调用是无害 no-op；关闭思考时也不会出现思考阶段截断。

## 8. 模型发现

基类 `list_models()` 用 OpenAI 兼容 Models API，外层 `asyncio.wait_for` 与 SDK 共用 `llm.timeout_seconds`，并保证关闭临时客户端（`src/llm/base.py:368-394`）。Anthropic 覆写为分页读取全部模型（`src/llm/anthropic.py:107-145`）。

`LLMMgr.load_models()` 并发发现所有已配置 provider；响应必须是仅含非空字符串的列表并按首次出现去重。发现失败先进入统一分类与安全日志；该 provider 配有非空静态 `models` 时使用静态列表，否则不注册模型，并把错误保存到 `provider_errors`。模型在不同 provider 间重复归属属于配置错误（`src/mgr/llm_mgr.py:121-223,443-504`）。启动时仍会精确验证 `llm.default`，不会因其他 provider 可用而切换默认模型（`src/mgr/llm_mgr.py:308-335`）。
