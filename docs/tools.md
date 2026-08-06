# 工具层参考

工具层由 `@tool`、`ToolEntry`、`ToolPolicy` 和 `ToolsMgr` 组成。内置工具位于 `src/tools/builtin/`，动态 MCP 工具由 `McpMgr` 注册。

## 注册契约

`@tool` 接受 Pydantic 参数模型和以下元数据：

| 参数 | 用途 |
|---|---|
| `name` | 覆盖函数名作为工具名 |
| `policy` | 冻结的声明式授权策略；缺省为 `REVIEW + DYNAMIC` |
| `raw_output` | 跳过 LLM 分页，但仍执行结果脱敏与总量限制 |
| `subagent` | True 自动注入子 Agent，False 强制排除，None 按工具白名单决定 |
| `feature` | 所属 feature；未启用时从 schema 和执行入口排除 |
| `counts_as_work` | 是否计入回合活跃计算时间 |

装饰器读取 `model.model_json_schema()` 生成 function-calling schema，并把 `ToolEntry` 放入全局注册表。同步函数由 `ToolEntry.__call__()` 统一通过 `asyncio.to_thread()` 卸载；真正异步的工具保留 async 实现。

`ToolEntry.validate_arguments()` 使用 Pydantic 校验并 `model_dump()`，因此默认值和 `None` 都会成为授权与执行共用的规范输入。`ToolEntry.origin` 独立记录 builtin、mcp 或 dynamic 来源。

## ToolPolicy

策略类型定义在 `src/tools/policy.py`：

```python
ToolPolicy(
    access=AccessKind.REVIEW,
    data_flow=DataFlow.DYNAMIC,
    path_args=(),
    plan_safe=False,
    detail_template=None,
)
```

`path_args` 支持多个 `PathArgument`，用于同时声明读、写、源和目标路径。`detail_template` 在授权阶段生成一次脱敏后的展示文本，权限通知与工具事件复用该结果。策略是冻结数据，构造时拒绝 callable。

内置注册代码按工具真实语义选择策略：

| 类别 | 策略 |
|---|---|
| read/list/glob/grep/file info | `LOCAL_READ + LOCAL`，`plan_safe=True` |
| create/write/edit/replace | `WORKSPACE_WRITE + LOCAL` |
| move/rename | `REVIEW + LOCAL`，声明 source 和 destination |
| shell | `REVIEW + DYNAMIC` |
| web search/fetch | `EXTERNAL_READ + EXTERNAL` |
| MCP | 强制 `REVIEW + EXTERNAL` |
| calculator、ask_user、compact、任务/计划状态 | `INTERNAL + LOCAL`，按需声明 `plan_safe` |

只有可信 builtin 可以确定性放行。未知或动态工具保守使用 `REVIEW + DYNAMIC`。

## 执行流水线

`ToolsMgr.execute()` 的完整顺序是：

1. 查找 ToolEntry，拒绝未知或被 feature 排除的工具。
2. Pydantic 校验原始参数。
3. 运行可信 `PreToolUse` Hook；blocked 或 deny 立即拒绝。
4. Hook 改写参数后重新校验。
5. 调用 `PermissionManager.authorize()`；按裁决结果发布脱敏的 PermissionNotice（放行/拒绝/需确认一行提示，含理由），通知携带 `decision_source`，UI 按真实裁决来源标注。
6. 发布脱敏的 `ToolCallStarted`，携带 `ToolDisplay`（中文标题 + 参数摘要）。
7. 执行工具，并把结果立即经 DataGuard 递归脱敏、限制到 1 MiB/20,000 行。
8. 提取 `ToolResult`：若工具函数返回 `ToolResult`，分离 `.display`（展示侧）和 `.text`（LLM 侧）。
9. 以脱敏参数和结果运行 `PostToolUse`。
10. 再次脱敏和限长，发布 `ToolCallCompleted`，携带 `ToolDisplay`（文件 diff 或格式化结果）。
11. 非 raw_output 结果按模型 token 预算分页；分页缓存只存已脱敏文本。

授权流程详见 [permissions.md](permissions.md)。PreToolUse 可以读取和改写原始参数，因为项目 Hook 只有通过启动信任门后才会加载；任何离开执行边界的数据必须先脱敏。

## 文件工具

文件工具统一通过 `FileMgr` 和 `PathResolver`：

- `list_directory(path=None, max_depth=3)`：树状列目录，深度上限 8，不跟随目录符号链接，累计上限 10,000 项和 10 秒。
- `glob(pattern, path=None)`：使用 ripgrep 查找文件，10 秒超时。
- `grep(pattern, path=None)`：使用 ripgrep 搜内容，10 秒超时并限制展示行数。
- `get_file_info(path)`：返回普通文件或目录元信息。
- `read_file(path, start_line=None, end_line=None)`：读取带行号文本，单文件不超过 8 MiB。
- `create_directory(path)`、`write_file(...)`、`edit_file_lines(...)`、`replace_all_in_file(...)`：普通工作区写入走策略快速路径。
- `move_file(source, destination)`：移动前同时解析源、声明目标和目录语义下的最终目标，始终 REVIEW。

所有相对路径基于 Agent workdir。FileMgr 在实际 I/O 前重新解析路径；读取结果在返回前已经过 DataGuard。

### ToolResult 返回协议与文件差异

文件写入工具（`write_file`、`edit_file_lines`、`replace_all_in_file`）可返回 `ToolResult`（`src/tools/display.py`）而非普通字符串。`ToolResult` 携带 `.text`（LLM 侧结果，不变）和 `.display`（`ToolDisplay`，仅 UI 消费）。`ToolEntry.__call__()` 识别 `ToolResult` 并原样保留，`ToolsMgr.execute()` 提取 `.display` 后将 `.text` 作为常规字符串结果继续流水线。

文件差异由 `build_file_diff()`（`display.py`）生成：在写入前捕获 `old_lines`，写入后捕获 `new_lines`，使用 `difflib.SequenceMatcher` 生成 2 行上下文的分组差异。差异格式为 `  {lineno:>4} |+ {line}`（新增）/ `|- {line}`（删除）/ `| {line}`（上下文），标题含 `(+A -D)` 统计。分块写入（`chunk_index`/`total_chunks`）不生成差异，仅在最终写入完成时由 `FileMgr._make_diff_result()` 返回。

### 展示预算

工具展示内容受以下预算约束（`src/tools/display.py`）：

| 场景 | 行数上限 | 字节上限 |
|---|---|---|
| 参数摘要 (`format_params`) | 20 | 4 KiB |
| 结果内容 (`format_result`) | 60 | 12 KiB |
| 文件差异 (`build_file_diff`) | 60 | 12 KiB |

超限时截断并附 `… (已截断)` 提示。字节截断保持 UTF-8 完整性（`errors="ignore"`）。

## Shell

Shell 固定为 `REVIEW + DYNAMIC`，不会因命令看似只读而绕过智能权限。代码层只拦截高置信危险命令和外传模式，其他构建、测试、依赖安装与 Git 操作交智能权限审查。

执行 cwd 固定为 workdir，timeout 由 Pydantic 限制在 1–600 秒。子进程创建独立进程组；超时或取消后终止并回收整个进程组。环境由 DataGuard 构造，不继承模型密钥、token、cookie 或密码。stdout/stderr 并发读取并共享 1 MiB/20,000 行预算。

## Web 与 MCP

`web_search` 和 `web_fetch` 共用 provider 级 `llm_provider.<name>.web` 路由配置。`local` 使用本地后端；`provider` 优先使用当前 Agent 模型所属 provider 的原生能力。OpenAI 原生提供搜索、抓取回退本地；Anthropic 原生提供搜索和抓取；DeepSeek 与其他未声明能力的 provider 回退本地。只有明确的“能力不支持”会回退，认证、网络、限流、超时和响应协议错误不会再次外发。

两者固定为 `EXTERNAL_READ + EXTERNAL`，可在 Plan 中使用，但每次仍经过 Web 隐私预检与安全审查。查询/URL 含已识别秘密时本地拒绝；疑似个人信息、专有代码或私有标识符时不发送给审查模型，直接请求一次性确认。Web 安全审查使用发起调用的 Agent 当前模型，不使用 `llm.fast`，请求只包含脱敏查询或不含 query value 的 URL 摘要。

本地抓取只允许标准端口 HTTP/HTTPS，拒绝 URL 凭据和非公网 IPv4/IPv6；DNS 解析结果检查后固定连接 IP，HTTPS 仍按原主机名执行 SNI 与证书校验。重定向最多 5 次，只允许同主机且禁止 HTTPS 降级；不使用系统代理、cookie、认证或 referer，解压后正文上限 1 MiB。Web 完成事件只记录状态和结果长度，不记录搜索结果、网页正文或 URL query value；原始 Web 内容不建立额外磁盘缓存。

MCP 工具通过 `_PassThroughArgs(extra="allow")` 接收上游 schema 所描述的参数，名称格式为 `mcp__<server>__<tool>` 并清洗限长。无论上游如何标注，只能注册为 `REVIEW + EXTERNAL`。结果先由 `_format_result()` 转为文本，再进入统一脱敏、Hook、事件和分页流程。

## 工具可见性

`ToolsMgr.get_schemas()` 按 access 类别和工具名稳定排序。Agent 的 `tools` 白名单、`subagent` 元数据和 feature 门控共同决定 schema：

- 主 Agent 使用角色工具白名单，未声明表示全量注册工具。
- 子 Agent 在自身白名单基础上自动加入 `subagent=True`，强制移除 `subagent=False`。
- 未启用 feature 的工具始终排除，即使白名单显式列出。

Plan 不通过隐藏 schema 表达安全边界；调用时由 `PermissionManager` 独立执行 Plan 约束，避免动态工具或缓存 schema 绕过。
