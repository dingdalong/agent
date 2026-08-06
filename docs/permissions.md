# 统一授权与数据安全

权限系统的唯一公共入口是 `PermissionManager.authorize()`。它对每次已经过 Pydantic 校验和可信 `PreToolUse` Hook 改写后再校验的工具调用，返回不可变的 `AuthorizationResult`。调用级裁决不缓存，也没有会话级或持久放行。

## 工具策略

工具通过冻结的 `ToolPolicy` 声明授权所需事实，定义见 `src/tools/policy.py`：

| 字段 | 含义 |
|---|---|
| `access` | `LOCAL_READ`、`EXTERNAL_READ`、`INTERNAL`、`WORKSPACE_WRITE` 或 `REVIEW` |
| `data_flow` | `LOCAL`、`EXTERNAL` 或 `DYNAMIC` |
| `path_args` | 一个或多个 `PathArgument(name, role)`；role 为 read/write/source/destination |
| `plan_safe` | INTERNAL 工具是否可在 Plan 激活时执行 |
| `detail_template` | 仅用于经 DataGuard 脱敏后的 UI 展示 |

`ToolOrigin` 记录 builtin、mcp 或 dynamic 来源。只有仓库内可信的 builtin 注册代码能获得确定性放行；非 builtin 工具即使传入更宽松策略，也会降级到 REVIEW。未声明策略的工具使用 `REVIEW + DYNAMIC`。

MCP 工具固定为 `REVIEW + EXTERNAL`。上游 annotation（包括 `readOnlyHint`）不改变策略。

## 授权顺序

`ToolsMgr.execute()` 的顺序不可交换：

1. 用 Pydantic 校验原始参数并展开默认值。
2. 运行已经过项目启动信任门的 `PreToolUse` Hook。
3. Hook 修改参数后重新校验。
4. 用 `PathResolver` 提取、规范化并分类全部路径；移动操作额外解析最终目标。
5. 运行 `HardDenyDetector` 和秘密外发检查。
6. 若 `agent.plan_active`，先执行独立 Plan 约束。
7. LOCAL_READ 和 INTERNAL 走确定性放行；EXTERNAL_READ 进入 Web 专用隐私预检和安全审查。
8. WORKSPACE_WRITE 仅在全部写目标为普通工作区或计划目录时确定性放行。
9. 其余调用交通用 LLM 智能权限审查。
10. 智能权限返回 ask、异常、超时或无效响应时，只进行一次 yes/no 人工确认；无 TTY、取消或拒绝均为 deny。
11. 工具执行结果立即经 DataGuard 脱敏和限长，再进入 PostToolUse、事件、分页和 Agent 历史。

`AuthorizationResult.source` 标明裁决来源：`hard_rule`、`plan`、`policy`、`judge`、`web_safety`、`user` 或 `failure`。`reason` 和 `safe_detail` 在返回前再次脱敏并限长。允许结果还包含冻结的 `path_grants`，只记录参数名、角色、授权时规范路径和分类；FileMgr 在每次实际 I/O 前复检规范路径与分类，移动操作同时复检 source、destination 与最终目标。

## 路径解析

`PathResolver` 是授权层和 `FileMgr` 的共同路径语义：

- 相对路径始终基于 Agent workdir，而非进程当前目录。
- 展开 `~` 和 `..`，解析已有符号链接；新路径通过最近存在父目录得到真实目标。
- 可选读取路径为 `None` 时解释为 workdir。
- 移动同时解析 source、destination；若 destination 是已有目录，再追加源文件名形成 `destination_final`。
- FileMgr 在创建父目录后和实际 I/O 前重新解析，降低检查后路径替换风险。

普通工作区快速写入排除 `.git/**`、`.agent/**`、`.vscode/**`、`.idea/**`、`.env*`、私钥和常见凭证路径；`.agent/plans/**` 单独分类为 PLAN。受保护路径、工作区外写入和移动操作进入 REVIEW。

LOCAL_READ 可以读取工作区外的普通文件或目录，但拒绝设备、FIFO、socket、`/dev`、`/proc`、`/sys` 等伪文件。单文件上限 8 MiB；单次工具结果上限 1 MiB 或 20,000 行。目录展开不跟随目录符号链接，深度最多 8、累计最多 10,000 项、最长 10 秒；grep/glob 子进程同样限制为 10 秒。

## Hard Deny

`HardDenyDetector` 只保留可高置信静态识别、且不能由智能权限或用户覆盖的危险操作：

- sudo、su、doas、pkexec 等提权。
- 根目录、主目录或系统目录级递归删除。
- mkfs、分区擦除、危险 dd 和块设备写入。
- shutdown、reboot、halt、poweroff 和 fork bomb。
- 下载内容直接交给 shell、解释器或动态执行入口。
- 未限定范围的 `git clean -fdx`。
- 凭证文件、环境变量或已登记秘密向网络工具外传。
- 修改 `trusted_projects.json`、关闭脱敏或绕过授权门控。

普通构建、测试、安装依赖、项目内删除、Git 推送和重置等不在代码中预判，统一进入 REVIEW。

## LLM 智能权限审查

`LLMJudgeClient` 使用 `llm.fast`，由 `LLMMgr` 在未配置 fast 时回退 default。调用最长 15 秒，并强制通过 `record_verdict` 工具返回 allow、deny 或 ask。

智能权限请求只包含：工具名和来源、动作类别、数据流、规范化路径分类、网络主机、参数类型与长度、风险标志、最多 2 KiB 的脱敏用户意图。Shell 额外发送最多 8 KiB 的脱敏命令。文件正文、待写内容、完整 body、header、cookie、环境变量和值、完整 URL query 都不会进入请求。参数摘要在系统提示词中明确标记为不可信数据。

智能权限 allow/deny 直接成为本次裁决；ask、异常、超时、缺失或无效输出进入一次性确认。确认只接受 yes/no，不产生任何后续调用权限。

权限裁决会以一行提示展示给用户，格式 `{标记} {来源} · {中文工具名} · {结论}(理由)`（组装见 `src/tools/display.py` 的 `permission_line`）。标记 `✘`/`✔`/`?` 分别对应拒绝/放行/需确认；来源标签由 `AuthorizationResult.source` 决定，映射见 `PERMISSION_SOURCES`（`hard_rule`→硬规则、`plan`→计划模式、`policy`→策略放行、`judge`→智能权限、`web_safety`→网页安全、`user`→用户、`failure`→授权失败）。理由完整展示、由历史区自动折行，不截断。所有拒绝都发 `PermissionNotice`（携带 `decision_source`），放行仅 `source="judge"` 的智能权限裁决提示，本地读取、普通工作区写入等策略放行不打断输出；拒绝亮红、放行亮黄。ask 走 `PermissionMenu`，其理由在确认弹窗之前的输出区提示，标签固定「智能权限」。

Web 外部读取另使用 `WebPrivacyGuard` 与 `LLMWebSafetyClient`。本地预检先拒绝秘密、URL userinfo 和认证/签名 query；疑似个人信息、源代码、专有文本或高熵私有标识符不进入 LLM，直接一次性确认。其余请求由当前 Agent 模型审查，不切换到 `llm.fast`；搜索只发送最多 2 KiB 的脱敏查询，抓取只发送 scheme、host、path 和 query key，不发送 query value。两种审查共用 15 秒超时、结构化 `allow/deny/ask` 和失败后一次性确认逻辑。

## DataGuard

共享 `DataGuard` 登记 Provider key、可信环境文件和 MCP header/env 中的确切秘密，并检测 Authorization、cookie、password、token、API key、JWT、平台 token 和私钥块。它递归处理结构化数据，也会清理 URL userinfo、敏感 query、命令、异常和普通文本中的 URL。

EXTERNAL 工具在执行前发现秘密即 Hard Deny；DYNAMIC Shell 还会运行结构性外传检测。读取层允许读取凭证文件，但正文在离开读取层前脱敏。Shell、Hook 和 stdio MCP 子进程使用 `safe_environment()`，模型 key、token、cookie 和密码不会从父进程环境继承；MCP 只额外获得可信配置显式声明的 env。

工具开始/完成事件、权限通知、授权日志、UI 预览、PostToolUse、分页缓存、Agent history、子 Agent transcript、session、compact 和工作区 transcript 只能接收已经脱敏的数据。Session、transcript 和信任库使用原子写入和 owner-only 权限。

## 项目启动信任

`ProjectTrustGate` 在项目环境、Provider/URL 配置、Hook、Plugin Hook 和 MCP 激活前运行。信任键是规范化 workdir；目录一旦确认信任，目录内文件变化不影响信任状态。

首次进入未记录的工作目录时要求确认；首次启动在 TUI 创建前通过异步纯文本行读取，运行中的 `/clear` 由 EventBus 选择菜单读取，避免与常驻 UI 争抢 stdin。两条通道都默认拒绝，仅明确选择信任才允许加载。拒绝、取消、确认失败或非 TTY 进入受限模式：忽略项目 `.env`、模型/Provider 配置、项目角色、项目 Hook、项目/角色 Plugin Hook 和项目 MCP。普通 AGENTS、agents、skills 和 memory 仍作为数据加载。

规范化工作目录列表保存在全局 `trusted_projects.json`，原子写入并设置为 `0600`。`/clear` 重新检查目录是否已记录，并按结果重载配置、Hook 和 MCP；此前拒绝的目录可在此时再次确认。

## Plan

Plan 是 `Agent.plan_active: bool`，不是授权策略变体。`PlanModeController` 只管理入口 Agent 的 Shift+Tab 双向切换和 `PlanStateChanged`；`/plan` 与 `enter_plan_mode` 进入 Plan，`exit_plan_mode` 提供展示与审核工作流，但不是唯一退出方式。活动计划路径在快捷键退出时保留。

Plan 激活时只允许：

- LOCAL_READ。
- EXTERNAL_READ，但仍须通过 Web 隐私预检和安全审查。
- `plan_safe=True` 的 INTERNAL 工具。
- 规范化后位于 `.agent/plans/**` 的 WORKSPACE_WRITE。

其他调用直接以 `source="plan"` 拒绝，不调用智能权限。子 Agent 在构造时继承父 Agent 当前 Plan 状态。

## 授权日志

每次授权的最终结论都写入 `{workdir}/.agent/logs/agent.log`（`bootstrap.create_app` 配 `RotatingFileHandler`，2 MiB × 3 个备份，目录 `0700`、文件 `0600`）。三类行：

- `授权 <工具> → allow/deny source=<来源> reason=<理由>` —— 唯一漏斗，在 `PermissionManager._result` 中于脱敏同一语句内产出，覆盖所有来源与来源的全部调用路径。
- `<来源> 裁决 <工具> → allow/deny/ask（理由）` 与 `<来源> 失败，转一次性人工确认：…` —— 智能权限/web 审查的阶段裁决，先于漏斗行。
- `转人工确认 <工具>（理由）` —— 一次性确认弹窗开启；与紧随的漏斗行的时间差即用户思考时长，进程被杀导致弹窗未结束时只有此行而无结果行。

级别约定：所有拒绝与非确定性放行（judge/plan/web_safety/user/failure）为 info；`source=policy` 的确定性放行（每次本地读取都会命中）为 debug，避免刷屏，调 `config.yaml` 的 `logging.level` 到 `debug` 才记录全量。写入的 reason 已过 `DataGuard.redact`，与展示给用户的同源。

## 安全边界

智能权限是风险分类器，不是 OS 沙箱。明确高危操作、已识别秘密外发、项目启动信任、路径复检、结果脱敏和安全子进程环境由代码保证；无法可靠静态判断的 Shell、网络、MCP、移动和动态工具交智能权限审查，并在不确定时回到一次性人工确认。
