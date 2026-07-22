# LLM 层

本文档面向开发者与运维者，说明本框架的 LLM 抽象层：provider 注册表、`LLMProvider` 基类、`LLMResponse`、`chat()` 重试与并发机制、工具结果分页，以及五个具体 provider（Anthropic / OpenAI / DeepSeek / Ollama / Moonshot）的差异。

相关文档：模型别名解析与 `LLMMgr` 见 [managers.md](managers.md)；配置键 `llm.*` / `llm_provider.*` / `tool.page_token_rate` 见 [configuration-reference.md](configuration-reference.md)；工具结果分页的消费端见 [tools.md](tools.md)；LLM 调用发出的事件见 [events-and-ui.md](events-and-ui.md)。

---

## 1. 总述

LLM 层位于 `src/llm/`，职责是把「一段消息 + 工具 schema」转换成对具体大模型 API 的一次带重试、带流式事件、带 token 统计的调用，并把各家 API 的响应归一化为统一的 `LLMResponse`。上层（Agent 状态机、`CompactMgr` 等）只依赖抽象基类 `LLMProvider`，不感知具体 provider。

### provider 注册表

`src/llm/__init__.py:7` 维护静态注册表 `_PROVIDERS`，键为 provider 名，值为实现类：

| provider 名 | 实现类 | 文件 |
|---|---|---|
| `deepseek` | `DeepSeekProvider` | `src/llm/deepseek.py` |
| `openai` | `OpenAIProvider` | `src/llm/openai.py` |
| `anthropic` | `AnthropicProvider` | `src/llm/anthropic.py` |
| `ollama` | `OllamaProvider` | `src/llm/ollama.py` |
| `moonshot` | `MoonshotProvider` | `src/llm/moonshot.py` |

`get_provider(name)`（`src/llm/__init__.py:14`）按名查表返回实现类；未知名抛 `ValueError` 并列出可选值。

### 模型别名

具体的模型 ID 由 `LLMMgr` 结合 `config.yaml` 的 `llm.default` / `llm.best` / `llm.fast` 以及内置的 Claude Code 别名映射解析得出，再由 `LLMMgr` 决定用哪个 provider 类去实例化。别名解析规则详见 [managers.md](managers.md) 的 LLMMgr 一节。本层只接收已解析好的 `model` 字段。

---

## 2. `LLMProvider` 基类构造字段

`LLMProvider` 是 `@dataclass(ABC)`（`src/llm/base.py:30`），所有 provider 共享一套构造字段。字段大多来自 config 的 `llm_provider.<name>` 段、`llm.*` 段与 `tool.page_token_rate`，由 `LLMMgr` 组装后传入。

| 字段 | 默认值 | 说明与效果 |
|---|---|---|
| `api_key` | 必填 | API 密钥，通常来自 `.env`。 |
| `base_url` | 必填 | API 基础地址，来自 `llm_provider.<name>.base_url`。 |
| `model` | 必填 | 已解析的模型 ID。 |
| `event_bus` | 必填 | 事件总线，用于发出流式增量与调用起止事件；为 `None` 时静默跳过发事件。 |
| `concurrency` | `5` | 并发信号量上限，`chat()` 进入时 `async with self._semaphore` 限流。来自 `llm.concurrency`。 |
| `max_retries` | `6` | `chat()` 的最大尝试次数（含首次）。来自 `llm.max_retries`（当前 config 设为 3）。 |
| `timeout` | `120.0` | 传给底层 SDK 客户端的超时（秒）。 |
| `context_limit` | `0` | 模型上下文窗口大小，来自 `llm_provider.<name>.context_limit`。用于计算分页预算与压缩阈值。 |
| `page_token_rate` | `0.03` | 单页工具结果占上下文窗口的最大比例，来自 `tool.page_token_rate`。 |
| `page_token_budget` | 计算得出（`init=False`） | 单页 token 预算，`__post_init__` 中 `max(1, floor(context_limit * page_token_rate))`（`src/llm/base.py:49`）。`context_limit=0` 时退化为 1。 |
| `supports_native_structured_output` | `False` | 是否原生支持结构化输出（Anthropic/OpenAI 在 `__post_init__` 置 `True`）。 |
| `reasoning_effort` | `"max"` | 推理力度，来自 `llm_provider.<name>.reasoning_effort`。各 provider 映射方式不同，见第 7 节。 |
| `preserve_thinking` | `False` | 是否在多轮中保留历史思考内容。仅 Ollama 使用（`chat_template_kwargs.preserve_thinking`）。来自 `llm_provider.<name>.preserve_thinking`。Moonshot 不走此字段，而是通过「不覆写 `clear_reasoning_content`」保留历史 `reasoning_content`（见第 6 节）。 |

`__post_init__`（`src/llm/base.py:47`）负责创建并发信号量 `self._semaphore` 与计算 `page_token_budget`；各子类在自己的 `__post_init__` 中先 `super().__post_init__()`，再构造 SDK 客户端并设置 `supports_native_structured_output`。

---

## 3. `LLMResponse`

`LLMResponse` 是所有 provider 归一化后的响应（`@dataclass`，`src/llm/base.py:21`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `content` | `str` | 文本内容（不含思考）。 |
| `tool_calls` | `dict[int, dict[str, str]]` | 按序号索引的工具调用，每项含 `id` / `name` / `arguments`（arguments 为 JSON 字符串）。默认空 dict。 |
| `finish_reason` | `str \| None` | 归一化后的结束原因，取值 `stop` / `tool_calls` / `length`，或透传底层原始值。 |
| `assistant_message` | `dict \| None` | 完整的 assistant 消息，含 provider 特有字段（如 `_anthropic_content` / `_response_output` / `reasoning_content`），供下一轮回填历史。 |
| `token_usage` | `dict[str, int \| None] \| None` | 统一口径的 token 用量。 |

### finish_reason 归一化

各 provider 把原生结束原因映射为统一三态：

| provider | 原生值 → 归一值 |
|---|---|
| Anthropic | `end_turn`→`stop`，`tool_use`→`tool_calls`，`max_tokens`→`length`，其余透传（`src/llm/anthropic.py:350`）。 |
| OpenAI | `completed`→`stop`，`incomplete`→`length`，其余透传 `resp.status`（`src/llm/openai.py:209`）。 |
| DeepSeek / Ollama | 直接透传 Chat Completions 的 `finish_reason`；若有工具调用而 `finish_reason` 为空则补 `tool_calls`。 |

### token_usage 统一键

所有 provider 的 `_extract_token_usage` 归一为同一组键，使上层（状态条、`CompactMgr`）无需感知 provider 差异：

| 统一键 | 含义 |
|---|---|
| `input_tokens` | 提交给模型的全部输入 token（含缓存）。 |
| `output_tokens` | 输出 token。 |
| `total_tokens` | 总 token。 |
| `cache_read_input_tokens` | 缓存命中（读取）token。 |
| `cache_creation_input_tokens` | 缓存写入（创建）token。 |

注意 Anthropic 的口径对齐：原生 `usage.input_tokens` 只计未命中缓存的新算输入，故 `AnthropicProvider._extract_token_usage`（`src/llm/anthropic.py:60`）把 `input_tokens + cache_read + cache_creation` 相加作为统一 `input_tokens`，与 DeepSeek/OpenAI/Ollama 的 `prompt_tokens`（本就含缓存）口径一致；`cache_read` / `cache_creation` 仍单列保留。

---

## 4. `chat()` 机制

`chat()`（`src/llm/base.py:310`）是唯一对外调用入口，模板方法模式：并发限流 + 重试循环 + 发事件，真正的 API 调用委托给抽象方法 `_do_chat`。

流程（`src/llm/base.py:321`）：

1. `async with self._semaphore` — 并发不超过 `concurrency`。
2. 循环最多 `max(1, max_retries)` 次：
   - 发 `LLMCallStarted` 事件（`_emit_llm_call_started`，在线程中运行 `estimate_tokens`，携带输入 token 估算且不阻塞事件循环）。
   - `await self._do_chat(...)` 执行真实调用。
   - 发 `LLMCallCompleted` 事件（带耗时、token 用量、吞吐率）。
   - 返回 `LLMResponse`。
3. 捕获异常：若 `not is_retryable_error(e)` 或已到最后一次尝试，直接 `raise`；否则退避后重试。
4. 所有重试耗尽抛 `RuntimeError("LLM chat: 所有重试均失败")`。

### 退避策略

`_retry_delay(attempt)`（`src/llm/base.py:206`）= `min(2 ** attempt * 5, 60) + _retry_jitter()`，即指数退避，基数 5 秒、封顶 60 秒，叠加 `random.uniform(0, 1)` 的抖动。

### 是否重试的判定

`is_retryable_error`（`src/llm/base.py:143`）：

- **首先** 若 `is_context_too_long_error(e)` 为真 → 不重试（返回 `False`）。
- **不重试** 的异常类型：认证 `AuthenticationError`、权限 `PermissionDeniedError`、`NotFoundError`（404）、`BadRequestError`、`UnprocessableEntityError`、`APIResponseValidationError`，以及 OpenAI 的 `ContentFilterFinishReasonError` / `LengthFinishReasonError`（openai 与 anthropic 两套 SDK 的对应类型均列入）。
- **重试** 的异常类型：连接错误 `APIConnectionError`、超时 `APITimeoutError`、限流 `RateLimitError`、服务端错误 `InternalServerError`、冲突 `ConflictError`，以及 `httpx.TimeoutException` / `httpx.TransportError` / `asyncio.TimeoutError` / `TimeoutError` / `ConnectionError`。
- **按状态码**：`408` / `409` / `429` 重试；`>= 500` 重试。
- `OSError`（排除 `FileNotFoundError` / `PermissionError` / `IsADirectoryError` / `NotADirectoryError`）也重试。

### 上下文超长判定

`is_context_too_long_error`（`src/llm/base.py:130`）对异常文本（`_exception_text` 拼接 `str(exc)` + `body` + `response.text` 并小写）做关键短语匹配，命中任一即判定为上下文超长：`context length`、`maximum context`、`prompt too long`、`overlong_prompt`、`input is too long`、`tokens exceed`、`too many tokens`。此类错误不重试，交由 Agent 状态机的 `CONTEXT_OVERFLOW` 处理（见 [agent-runtime.md](agent-runtime.md)）。

### 抽象方法

子类必须实现两个抽象方法：

- `estimate_tokens(messages, prompt=None, tools=None)`（`src/llm/base.py:62`）— 估算该 provider **实际发送的完整请求载荷** token 数，用于状态条、自动压缩与分页判定；外部签名不变。
- `_do_chat(...)`（`src/llm/base.py:471`）— 执行真实的流式 API 调用并返回 `LLMResponse`。

### normalize_messages

`normalize_messages`（`src/llm/base.py:225-315`）先清洗单条消息：校验并规范 `role`（合法集 `system`/`user`/`assistant`/`tool`，可选 `developer`）、规整 `content`、过滤空消息、规范 assistant 的 `tool_calls` 与 tool 消息的 `tool_call_id`。可覆写钩子 `_normalize_role` / `_normalize_content` / `_normalize_assistant_extra` 继续负责 provider 转换（如 Ollama 把 `developer` 归为 `system`、DeepSeek 保留 `prefix`、OpenAI/Anthropic 保留原始调用载体）。

随后执行序列级工具协议校验（`src/llm/base.py:317-427`）：每条带 `tool_calls` 的 assistant 消息中，调用 ID 必须是非空且组内唯一的字符串；紧随其后的连续 tool 消息必须按 ID 对每个调用恰好响应一次，不能缺失、重复或混入未知 ID。完整单工具和多工具往返连同 provider 额外字段原样保留。

默认模式会安全修复非法序列：有可见 `content` 的非法工具 assistant 降级为只含 `role/content` 的纯文本消息，无文本则删除；同组 tool 消息、`tool_calls`、推理字段、OpenAI `_response_output` / Anthropic `_anthropic_content` 等 provider 原始调用载体一并删除。游离或重复 tool 消息同样删除，并只记录结构原因和消息数量，不记录工具参数内容。`strict=True` 时上述非法序列直接抛出 `ValueError`，不静默修复。`allow_tool_calls=False` 时保持原有的禁用工具字段转换路径，不执行工具序列配对（`src/llm/base.py:297-315`）。

---

## 5. 工具结果分页

单次工具调用结果可能远超模型上下文窗口，因此按 `page_token_budget` 切页，由基类统一提供切分算法，`ToolsMgr` 负责触发与缓存（见 [tools.md](tools.md) 的执行流水线）。

- `split_page(text)`（`src/llm/base.py:100`）— 反复调用 `_split_page_once` 直至耗尽，返回页列表（空文本返回 `[""]`）。
- `_split_page_once(text)`（`src/llm/base.py:81`）— 若整段 `estimate_tokens` 已不超预算则整段返回；否则二分查找最大的、`estimate_tokens` 不超过 `page_token_budget` 的前缀切点。

`page_token_budget` 与 `tool.page_token_rate`、`context_limit` 的关系见第 2 节。运维侧调节单页大小改 `tool.page_token_rate`（见 [configuration-reference.md](configuration-reference.md)）。

---

## 6. 五 provider 差异

| 维度 | Anthropic | OpenAI | DeepSeek | Ollama | Moonshot |
|---|---|---|---|---|---|
| API 类型 | Messages API（`messages.stream`） | Responses API（`responses.create`，流式） | OpenAI 兼容 Chat Completions | OpenAI 兼容 Chat Completions（本地） | OpenAI 兼容 Chat Completions（Moonshot） |
| SDK 客户端 | `AsyncAnthropic` | `AsyncOpenAI` | `AsyncOpenAI` | `AsyncOpenAI`（默认 `base_url` 回退 `http://localhost:11434/v1`，`api_key` 回退 `"ollama"`） | `AsyncOpenAI` |
| 结构化输出 | `supports_native_structured_output=True` | `True` | `False` | `False` | `False` |
| tokenizer（estimate_tokens） | tiktoken `cl100k_base`（异常时回退 `len//4`）；统计 `_convert_messages`、system/cache-control 与转换后的 tools | `tiktoken.encoding_for_model(model)`，未知模型回退 `o200k_base`；统计 `_convert_to_input` 与转换后的 tools | 本地 transformers tokenizer（`src/llm/tokenizer/deepseek`，`trust_remote_code=True`，`cached_property`） | 字符估算 `len(str(...)) // 4` | 字符估算 `len(str(...)) // 4` |
| 思考处理 | `thinking={"type":"adaptive"}` + `output_config={"effort": _map_effort(...)}`；关闭时 `thinking={"type":"disabled"}` | `reasoning={"effort": reasoning_effort, "summary":"auto"}`；关闭时不传 `reasoning` | `reasoning_effort` + `extra_body={"thinking":{"type":"enabled"}}`；关闭时 `{"type":"disabled"}` | 开启时按需传 `reasoning_effort`，`preserve_thinking` 时传 `chat_template_kwargs`；关闭时 `chat_template_kwargs={"enable_thinking": False}` | 顶层 `reasoning_effort`（仅 `enable_thinking=True` 时下发，**不用** `extra_body.thinking`）；Kimi（k2.6/k2.7 系列）恒思考，关闭时仅不传该字段 |
| 思考流式事件 | `content_block_delta` 中 `thinking_delta`→`emit_thinking_delta`，`text_delta`→`emit_response_delta` | `response.reasoning_summary_text.delta`→思考，`response.output_text.delta`→正文 | `delta.reasoning_content`→思考，`delta.content`→正文 | `delta.reasoning` 与 `delta.reasoning_content` 双字段均→思考；正文在流末尾一次性 emit | `delta.reasoning_content`→思考，`delta.content`→正文（逐块 emit） |
| 历史往返载体 | `_anthropic_content`（含 text/thinking/tool_use 原始块，回填时严格交替，`_merge_messages` 合并同角色） | `_response_output`（Responses API 的 `output` 项，`model_dump(exclude_none=True)`） | `reasoning_content`（可选 `prefix`） | `reasoning` 与 `reasoning_content` | `reasoning_content`（带 `tool_calls` 时**恒有**该键，即使为 `""`） |
| 缓存 token | 三项相加进 `input_tokens`（见第 3 节） | `input_token_details.cached_tokens` / `cache_creation_tokens` | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | `prompt_tokens_details.cached_tokens` / `cache_creation_tokens`（回退 `cache_creation_input_tokens`） | `prompt_tokens_details.cached_tokens` / `cache_creation_tokens`（回退 `cache_creation_input_tokens`） |
| temperature | 不下发 | 不下发 | 下发 | 下发 | **不下发**（思考模型不可传） |
| max_tokens | 固定 16000 | 由 SDK 默认 | 由 SDK 默认 | 由 SDK 默认 | 固定 32768（容纳 reasoning + content） |

补充要点：

- **Anthropic**：`max_tokens` 固定 16000；`_convert_messages` 把 OpenAI 兼容消息转为 Claude 格式并抽取 system，`_merge_messages` 合并连续同角色消息以满足 Claude 严格交替要求；`_convert_tools` / `_convert_tool_choice` 做格式转换（`auto`/`any`/`tool`）。token 估算复用同一转换结果，并对副本应用 cache-control，不修改调用方消息。`clear_reasoning_content` 会剥离历史中的 thinking 块。
- **OpenAI**：`_convert_to_input` 把 Chat 消息转为 Responses API 的 `input` 项，并把 system/developer 内容合并为首条 developer input 以进入可缓存前缀；工具 `strict=False`。token 估算只序列化转换后的 Responses API `input` 和 tools，不重复统计 `_response_output` 与标准消息字段。
- **DeepSeek**：`_normalize_content` 会 `strip()` 文本；支持 assistant 的 `prefix: true`（前缀续写）。流式解析中当既有 `tool_calls` 又 `content.isspace()` 时跳过空白内容。
- **Ollama**：流式解析末尾对有工具调用的情形 `strip()` 正文；`reasoning_effort` 为 `"none"`（大小写不敏感）时不传该参数。
- **Moonshot**：深度匹配 Kimi（k2.6/k2.7 系列）的 **Preserved Thinking 恒开**——API 无状态且要求跨轮回传历史 `reasoning_content`，尤其带 `tool_calls` 的 assistant 消息若缺该字段会报 `400 "reasoning_content is missing in assistant tool call message"`。实现靠两处配合：**不覆写 `clear_reasoning_content`**（继承基类无操作，Agent 轮末不剥离，思考持续留在 history 并随每轮回传）；`_normalize_assistant_extra` 在归一化时回注 `reasoning_content`，且当带 `tool_calls` 却无思考内容时补空串 `""`，从源头杜绝 400。`_do_chat` 不下发 `temperature`、固定 `max_tokens=32768`、思考仅经顶层 `reasoning_effort` 控制。

`list_models`（分类方法）：基类版用 OpenAI SDK 的 `models.list`（`src/llm/base.py:53`）；Anthropic 覆写为分页拉取（`src/llm/anthropic.py:17`）。

---

## 7. 推理力度 reasoning_effort

`reasoning_effort` 默认 `"max"`，实际取值来自各 provider 在 config 中的配置（`src/config.yaml` `llm_provider` 段）：

| provider | config 中的 reasoning_effort | 传给 API 的实际值 |
|---|---|---|
| deepseek | `max` | 原样作为 Chat Completions 的 `reasoning_effort` |
| openai | `xhigh` | 原样作为 Responses API `reasoning.effort` |
| anthropic | `high` | 经 `_map_effort` 映射后作为 `output_config.effort` |
| ollama | `high` | 原样作为 `reasoning_effort`（`none` 时不传） |
| moonshot | `max` | 原样作为顶层 `reasoning_effort`（当前仅支持 `max`；`enable_thinking=False` 时不传） |

### Anthropic 的 `_map_effort`

`_map_effort`（`src/llm/anthropic.py:237`）把配置值映射为 Claude 合法 effort：

- `max` 且模型名含 `sonnet` → `high`；
- `xhigh` → `high`；
- 值在 `{low, medium, high, max}` 内 → 原样；
- 其余 → 回退 `high`。

### enable_thinking 开关

`chat()` / `_do_chat` 的 `enable_thinking` 参数（默认 `True`）控制是否开启思考：

- `True`：按上表下发思考参数（Anthropic adaptive、OpenAI reasoning、DeepSeek/Ollama enabled、Moonshot 顶层 `reasoning_effort`）。
- `False`：显式关闭（Anthropic `thinking.disabled`、OpenAI 不传 `reasoning`、DeepSeek `thinking.disabled`、Ollama `chat_template_kwargs.enable_thinking=False`、Moonshot 不传 `reasoning_effort`——恒思考，返回的 `reasoning_content` 照常保留）。

调用方（如子 agent 的 `thinking` frontmatter、`CompactMgr` 的总结调用）据此决定是否让模型思考。
