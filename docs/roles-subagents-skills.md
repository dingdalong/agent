# 角色、子智能体与技能

本篇讲清框架的三个"可扩展装配单位"：**角色**（顶层组织单位）、**子智能体**（可被主 agent 委派的完整 Agent）、**技能**（按需注入的提示词包）。三者都以 `*.md`（YAML frontmatter + body）定义，共用同一套 frontmatter 解析（`parse_frontmatter` / `extract_manifest`，`src/mgr/role_mgr.py:34-147`）。

相关：feature 门控见 [architecture.md](architecture.md)；统一授权和 Plan 见 [permissions.md](permissions.md)；提示词拼装见 [managers.md](managers.md) 的 `PromptMgr`。

## 角色系统（Roles）

**一套角色决定了主 agent 的身份提示词、可用子 agent、技能、MCP server 与启用的 feature 集**——它是框架的顶层组织单位。由 `RoleMgr`（`src/mgr/role_mgr.py`）管理。

### 三层发现与激活

`RoleMgr._discover()`（`role_mgr.py:195-216`）按低→高优先级扫描三处目录，同名后者覆盖：

| 层 | 路径 | 说明 |
|---|---|---|
| 内置 | `src/roles/`（`builtin_root()/roles`） | 框架自带角色 |
| 全局 | `~/.agent/roles/`（`global_dir/roles`，可缺省） | 跨项目自定义 |
| 项目 | `<workdir>/.agent/roles/` | 项目专属 |

发现规则：每个子目录须含 `role.md` 才算一个角色；目录名 `common` **显式跳过**（`role_mgr.py:211`，它是共享目录不是角色）。

激活角色由 `config.yaml` 的 `role.default` 指定（`_resolve()`）：缺省或空值回退 `_DEFAULT_ROLE = "coding"`；指定角色不存在时告警并回退 `coding`；连 `coding` 都不存在则无角色激活（`active == False`）。`role` 必须是 mapping，旧标量格式不会被解析为激活角色。

主角色还可在同一段配置中覆盖 `role.md` 的模型和推理力度，例如为自定义 `reviewer` 角色配置：

```yaml
role:
  default: reviewer
  reviewer:
    model: best
    reasoning_effort: xhigh
```

`RoleMgr` 先解析实际激活角色的 `role.md`，再应用 `role.<实际角色名>.model` 与 `role.<实际角色名>.reasoning_effort`。模型只接受非空字符串；推理力度只接受 `low`、`medium`、`high`、`xhigh` 或 `max`，并按 `normalize_reasoning_effort` 规整。缺失或无效覆盖保留 `role.md` 的值；模型仍缺失时由 `llm.default` 兜底，推理力度仍缺失时由当前 provider 兜底。角色不存在而回退到 `coding` 时，应用的是 `role.coding` 的覆盖。该配置只影响主角色，不改变子 agent 的模型继承规则。

### `role.md` 的结构与作用

`role.md` 经 `extract_manifest(..., id_field="agent_type", default_id="main")` 解析为 `AgentManifest`（`role_mgr.py:150-168`）：

- **body** → 成为主 agent 的**核心身份与主控职责提示词**（`PromptMgr._build_core` 的"# 核心身份"段）；仅主 agent 的身份、委派职责与工作流写在这里，不放入共享准则文件。
- **frontmatter**：`agent_type` 对角色固定视为 `"main"`；`description`、`features`、`thinking`、`reasoning_effort`、`model`、`startInPlanMode` 等字段同子 Agent。`startInPlanMode` 只设置初始 `plan_active`，授权策略仍由工具策略和统一授权入口决定。

角色目录内其他资产由 `RoleMgr` 暴露路径（仅在目录/文件存在时返回，否则 `None`）：

| 方法 | 资产 | 用途 |
|---|---|---|
| `agent_md_path()` | `AGENTS.md` | 激活角色内主/子 agent 共用的行为准则 → "# 行为准则"段 |
| `agents_dir()` | `agents/*.md` | 角色专属子 agent |
| `skills_dir()` | `skills/*/SKILL.md` | 角色专属技能 |
| `plugins_dir()` | `plugins/` | 角色专属插件 |
| `mcp_servers_path()` | `mcp_servers.json` | 角色专属 MCP server（见 [mcp-and-hooks.md](mcp-and-hooks.md)） |

### `common/` 共享目录

`src/roles/common/` 不是角色，而是**对所有角色生效的最低优先级共享层**（`RoleMgr` 的 `common_*` 系列方法，`role_mgr.py:337-355`，基于 `common_role_dir()`）。其 `agents/`、`skills/`、`AGENTS.md` 会被叠加到任意激活角色之下（后续层同名覆盖）。当前 `common/agents/` 提供四个通用子 agent：`explore`、`general-purpose`、`plan`、`shell`。

### 内置角色一览

| 角色 | `model` | `startInPlanMode` | `thinking` / `reasoning_effort` | `memory` | `features` | 子 agent（`agents/`） | 说明 |
|---|---|---|---|---|---|---|---|
| `coding` | `best` | `true` | `true` / `max` | `project` | 未声明（全部启用） | coder、debug、doc、review | 通用编程助手（默认角色） |
| `mijia` | `fast` | `false` | `false` / 未声明 | 未声明 | `[subagent]` | device-control、home-diagnostics、home-status、scene-automation | 米家智能家居管家 |
| `onboard` | `best` | `false` | `true` / `high` | 未声明 | `[subagent, file, task]` | repository-map、module-analyst、cross-module、dimension-classifier、verifier、manual-writer、manual-reviewer | 证据驱动的项目开发手册分析与发布角色 |

> `coding` 与 `mijia` 都刻意省略 `tools`。`extract_manifest` 将缺失或空值解析为 `None`，即不设静态工具白名单，因此动态注册的 MCP 工具不会受静态白名单限制（仍受 feature 与权限过滤）。`coding` 也刻意省略 `features`；其 `None` 经 `resolve_features()` 解析为全部 feature。`onboard` 则声明固定工具白名单和 `[subagent, file, task]` feature 集，以限制其只执行证据流水线。

> 插件目前仅提供 skill 和 hook，不注册工具。`PluginMgr` 的发现和插件 hook 不受角色 feature 集限制；但插件 skill 需要 `skill` feature，因此 `mijia` 的 `features: [subagent]` 下不可用。

> `mijia` 声明 `features: [subagent]`，故 `task`/`skill`/`file`/`memory`/`plan` 均关闭——`MemoryMgr` 与 `PlanMgr` 在 `create_app()` 中注入 `None`；`file` feature 未启用时，不会为该 agent 创建 `FileMgr`，对应工具不会进入 schema。

### onboard 证据流水线

`onboard` 只负责分析和发布面向 Agent 的项目规则与任务技能，不承担后续编码工作。它以模块分片的 **MAP-REDUCE** 为骨架，并在两侧加装跨模块消解与分类核实，避免大型项目横扫源码导致的上下文超限与破坏性压缩：

- **阶段 1（索引 + 地图 + 分片）**：委派 `repository-map` 用 `index_repository` 建/更新代码图索引、`get_architecture` 读模块聚类，产出模块分层证据 `.agent/onboard/evidence/repository-map.md` 与分片计划 `.agent/onboard/shard-plan.md`（每片成员目录/glob、估算规模、neighbors 及生成物/第三方排除清单）。验证命令同时记录项目内实际调用位置，不能仅从工具配置推断。
- **阶段 2（MAP）**：按分片计划一轮发起全部分片的 `module-analyst` 委派，由 `asyncio.gather` + `llm.concurrency` 信号量自动流水线（有空位即补下一片），每个只读一个分片的源码（约 60k token 预算）+ 作用域内代码图，一遍产出小体量证据卡 `.agent/onboard/cards/<shard_id>.md`，并把跨分片才能确认的关系挂进卡的「未知项/需跨模块确认」。
- **阶段 2.5（跨模块消解）**：委派 `cross-module` 汇总全部卡的待确认跨模块关系，按点名符号做**有界**核对并逐条定级（`confirmed`/`conflict`/`unknown`），产出已核实的跨模块事实账本 `.agent/onboard/evidence/cross-module.md`。四个 REDUCE 维度共享此账本，不再各自 join。
- **阶段 3（REDUCE）**：同一轮并行发起四次 `dimension-classifier`，每次指派一个维度（`conventions`/`runtime-flow`/`change-patterns`/`guardrails`）。它们只读全部小卡与跨模块账本，做**维度内**归类（跨模块链路与契约直接引用账本），仅在维度内需升级为 `confirmed` 时按 `module::symbol` 打开有限源码，把报告写入 `.agent/onboard/evidence/`。四次调用共享同一份规范化输入却各自独立维护状态与验收，单份报告损坏只重跑它自己。主 agent 只在后续 prompt 中传递路径，不转运报告或卡的全文。
- **阶段 3.5（分类核实）**：委派 `verifier` 对四份报告里**每个** `dominant`/`conflict`/`unknown` 残留桶发现打开有界真实源码对抗式复核并改判（能判定的改判，只让静态不可判定的留 `unknown`），归一维度间分歧，产出核实侧车 `.agent/onboard/evidence/verification.md`。它不碰 `confirmed`。`unknown` 的定义随之收紧——**分片切割本身不构成 `unknown` 理由**，跨分片确认由 `cross-module` 完成，残留桶等级最终以 `verification.md` 为准。

onboard 的续跑只适用于同一未发布运行：`cross_module`、四个维度、`verification` 各自维护 `{status}`。流水线阶段严格线性（跨模块消解 → REDUCE 四维度 → 分类核实 → 候选生成 → 审核 → 发布），故某阶段的"下游"即它之后的全部阶段。同一未发布快照下，凡 `completed` 且正式产物存在、固定章节齐全、产物头快照一致的阶段直接复用（消费证据卡的阶段另需产物记录的 shard 集合覆盖当前全部证据卡），其余置 `pending` 重跑；四个维度各自独立走复用与验收，单个损坏只重跑自己。某阶段本轮**实际重跑**，或用户在同一未发布运行内强制重跑某阶段时，把该阶段之后的全部阶段一并置 `pending` 并重跑，避免修正内容被旧下游结论、旧候选或旧质量报告遗漏。每次启动或续跑都在当前会话重建任务图，不复用上一次会话的 task id。

仓库快照、范围或深度变化时不得做局部更新；发布阶段中断或已成功发布的运行也不支持增量更新。三种情况都要求用户手动清理 onboard 产物后全量重跑。

`manual-writer` 根据证据生成干净的根规则与独立任务技能，发现的**最终等级以 `verification.md` 的核实结论为准**（被 `verifier` 升级为 `confirmed` 的原残留桶可进活跃规则，进 `decisions.md` 的 `conflict`/`unknown` 每项带核实原因），并把规则/技能到 finding、符号、案例和命令来源的映射写入 `.agent/onboard/reference.md`。`manual-reviewer` 通过该映射按 `module::symbol` 打开实际代码反查候选正文，并强制**残留桶核实闭环**——凡最终仍为 `dominant`/`conflict`/`unknown` 的发现都必须在 `verification.md` 有核实结论且记录了已开符号，缺失即 FAIL，再写质量报告。候选最多修订两轮，审核或发布预检失败时不更新活跃规范；PASS 绑定候选内容，发布阶段原样写入，不再剥离证据。

审核通过后：

- 根 `AGENTS.md` 只在 `<!-- onboard:generated:start -->` 与 `<!-- onboard:generated:end -->` 之间维护自动生成的跨任务规则，区块外人工内容不变。
- 详细证据、映射、待决策项和状态保存在 `.agent/onboard/`，不作为活跃 Agent 指令。
- 达到证据门槛的开发范式发布为 `.agent/skills/onboard/<task-slug>/SKILL.md`。父目录只作分组，不放独立路由文件；每个技能依赖自己的 name 和 description 被发现。

`onboard` 本身未启用 `skill` feature。`PromptMgr` 将项目根 `AGENTS.md` 作为项目层最高顺序的行为准则加载，因此生成完成后根规则在 `/clear` 重建 Agent 时生效；项目技能需要重启应用并把角色切回 `coding`，由新的 `SkillMgr` 扫描后以 `user:onboard-<task-slug>` 名称加载。

## 子智能体（Subagents）

子智能体是**共享同一 `AgentDeps` 的完整 `Agent` 实例**，由主 agent 通过 `task_delegator` 工具委派。定义为 `agents/*.md`，由 `SubAgentMgr`（`src/mgr/subagent_mgr.py`）加载。

### 四层扫描

`SubAgentMgr._load_all()`（`subagent_mgr.py:36-67`）按低→高优先级扫描，同名 `agent_type` 后者覆盖：

| 层 | 来源 | 路径 |
|---|---|---|
| 共享 | `role_mgr.common_agents_dir()` | `src/roles/common/agents/` |
| 角色 | `role_mgr.agents_dir()` | 激活角色 `agents/` |
| 全局 | `global_dir/agents` | `~/.agent/agents/` |
| 项目 | `workdir/.agent/agents` | `<项目>/.agent/agents/` |

### frontmatter 字段（`extract_manifest`）

| 字段 | 类型 | 缺省 | 效果 |
|---|---|---|---|
| `agent_type` | str | 文件名 `path.stem` | 子 agent 标识（委派时用）；也是 `Agent.agent_type` |
| `description` | str | `"没有说明内容"` | 出现在主 agent 的"# 可用子智能体"提示词段，供 LLM 选择 |
| `tools` | 逗号分隔 str | 空 → `None`（全部工具） | 工具白名单；再经 `resolve_subagent_tools` 注入 `subagent=True`、排除 `subagent=False` |
| `model` | str | `None`（用 `default`） | 模型别名（`default`/`best`/`fast`）或真实 ID；`inherit` = 继承父 agent 已解析的真实模型 ID |
| `startInPlanMode` | bool | `False` | 独立构造时的初始 Plan 状态；经 `task_delegator` 构造时由父 Agent 当前状态覆盖 |
| `thinking` | bool | `None`（继承父 agent） | 是否启用思考；仅 bool 有效 |
| `reasoning_effort` | str | `None`（继承父 agent，主 agent 回退 provider 配置） | 推理力度档位；经 `normalize_reasoning_effort` 规整（小写去空白），合法值 `low`/`medium`/`high`/`xhigh`/`max`，非法值告警忽略 |
| `memory` | str | `None` | 记忆范围（如 `project`），控制 `MemoryMgr` 注入 |
| `features` | YAML 列表 | `None`（继承父 agent 已解析集） | 该子 agent 的 feature 集；空列表 = 全部禁用 |

解析规则细节见 `extract_manifest`（`role_mgr.py:50-147`）：`tools` 为空串则整体工具可见；`model` 空串视为未设置；`features` 非列表会告警丢弃。

### 委派流程 `task_delegator`

`SubAgentMgr.task_delegator(agent_type, prompt, parent_agent, task_id)`（`subagent_mgr.py:84-211`）：

1. 查表定位 `manifest`，不存在则返回错误文本（含可用列表）。
2. 若带 `task_id`：委派前把任务标记 `in_progress` 并设 `owner`；子 agent 异常退出时回滚为 `pending`（正常返回**不**自动标 `completed`，留给主 agent 评估）。
3. 解析工具集：`tools_mgr.resolve_subagent_tools(manifest.tools)`。
4. 解析模型：`inherit` → `parent_agent.llm.model`。
5. 解析思考：`enable_thinking is None` → 继承父 agent。
6. 解析推理力度：`reasoning_effort is None` → 继承父 agent 已解析值。
7. 解析 feature：`features is None` → 继承父 agent 已解析集。
8. 继承 `parent_agent.plan_active`，再由 `Agent.from_manifest(...)` 构造子 agent 实例（`is_subagent=True`）。
9. 触发 `SubagentStart` hook（若有）→ 发 `SubagentLifecycle(phase="start")` 事件 → `await agent.run(prompt)` → `finally` 发 `phase="end"` 事件 → 触发 `SubagentStop` hook。
10. `SubagentStop` 若 `blocked` 则用 `block_reason` 覆盖结果；若有 `additional_context` 则追加到结果末尾。

> Plan 工作流工具标记 `subagent=False`，不会进入子 Agent schema；但子 Agent 继承父 Agent 当前 `plan_active`，因此授权层的 Plan 限制仍然生效。

### 子智能体清单（当前仓库）

**共享（`common/`，所有角色可用）**

| agent_type | tools | model | 用途 |
|---|---|---|---|
| `explore` | 只读检索 + `web_search`/`web_fetch` | `default` | 只读探索代码/架构、联网研究并总结证据 |
| `general-purpose` | 全部（未声明） | `default` | 无专用 agent 匹配时的兜底任务执行 |
| `plan` | 只读检索（无写） | `best` | 架构设计与实现方案规划 |
| `shell` | `shell` | `fast` | 独立上下文运行命令 / Git 查询 / 测试执行 |

**coding 角色**

| agent_type | tools | model | 用途 |
|---|---|---|---|
| `coder` | 只读检索 + 写文件三件套 + `shell` | `best` | 实现功能 / 修 bug / 重构 / 写测试 |
| `debug` | 只读检索 + `shell` | `best` | 复现问题、定位根因、给诊断报告 |
| `doc` | 只读检索 + 写文件三件套 | `default` | 编写/维护文档 |
| `review` | 只读检索（无写） | `best` | 只读代码审查 |

**mijia 角色**

| agent_type | tools | model | 用途 |
|---|---|---|---|
| `device-control` | 只读检索* | `default` | 执行设备控制并验证状态 |
| `home-diagnostics` | 只读检索* | `default` | 只读诊断家居问题 |
| `home-status` | 只读检索* | `default` | 只读查询设备状态/布局/能力 |
| `scene-automation` | 只读检索* | `default` | 创建/编辑/删除/执行场景与自动化 |

> \* mijia 各 agent frontmatter 声明的 `tools` 均为内置只读检索工具集；实际的米家设备操作能力来自角色的 `mcp_servers.json` 注入的 MCP 工具（见 [mcp-and-hooks.md](mcp-and-hooks.md)），这些工具不在 frontmatter 白名单内时会经 `resolve_subagent_tools` 的 subagent 注入规则处理。"只读检索"指 `list_directory, glob, grep, get_file_info, read_file`。

**onboard 角色**

| agent_type | tools | model | 用途 |
|---|---|---|---|
| `repository-map` | 文件检索/报告写入 + `shell` + codebase-memory 索引/架构工具 | `best` | 建立索引、模块地图、生成物边界与分片计划 |
| `module-analyst` | 文件检索/卡写入 + codebase-memory 查询工具 | `best` | MAP：只读单分片源码+代码图，产出四维度证据卡 |
| `cross-module` | 文件检索/账本写入 + codebase-memory 查询/调用图工具 | `best` | 跨模块消解，产出事实账本 |
| `dimension-classifier` | 文件检索/报告写入 + `shell` + codebase-memory 查询/调用图工具 | `best` | REDUCE：按指派维度归类证据 |
| `verifier` | 文件检索/侧车写入 + `shell` + codebase-memory 查询/调用图工具 | `best` | 对残留桶发现做源码复核 |
| `manual-writer` | 文件读取与编辑 | `best` | 生成、修订并发布手册 |
| `manual-reviewer` | 文件检索/报告写入 + `shell` + codebase-memory 查询工具 | `best` | 反查候选规则并给出发布判定 |

这些子 agent 都显式声明 `features: [file]`，不会继承主 agent 的 `task` 或 `subagent` feature。codebase-memory 的 MCP 工具（`mcp__codebase-memory__*`）`feature=None`、不受 feature 门控，但 `subagent=None` 既不自动注入也不排除，故各 agent 必须在 frontmatter `tools:` 逐一列出所需 MCP 工具名。获准使用 `shell` 的代理只执行只读 Git 查询；所有分析报告、证据卡、跨模块账本与核实侧车的写入路径由角色提示词限制在 `.agent/onboard/`。

## 技能系统（Skills）

技能是**按需注入系统提示词的知识包**，由 `SkillMgr`（`src/mgr/skill_mgr.py`）加载，通过 `load_skill` 工具注入。适合"任务匹配时才需要的详细操作指南"，避免长期占用上下文。

### 多层扫描

`SkillMgr._load_all()`（`skill_mgr.py:44-88`）按低→高优先级扫描（同名 `namespace:name` 后者覆盖）：

```
共享 skills → 角色 skills → 全局 plugins → 全局 skills → 项目 plugins → 项目 skills
```

每个技能是一个含 `SKILL.md` 的目录（`rglob("SKILL.md")`）。技能名带命名空间前缀 `<namespace>:<name>`：共享与角色技能使用 `builtin`，全局和项目用户技能使用 `user`，插件技能使用插件名（`skill_mgr.py:16,99`）。

### `SKILL.md` 格式与注入

- frontmatter：`name`（缺省取父目录名）、`description`（缺省 `"没有说明内容"`）。
- `load_full_text(name)`（`skill_mgr.py:155`）返回包装文本：`<skill name=... skill_dir=...>` + body + 目录内其他文件的 `<skill-file path=... ref=... />` 清单 + `</skill>`。技能目录内的附属文件被登记为可引用资源（`skill_mgr.py:111-117`）。
- `prompt_section()`（`skill_mgr.py:140-150`）生成"# 可用技能"段（技能名+描述列表）与使用流程说明："当任务匹配某个技能时，调用 `load_skill` 加载后再执行操作。已加载技能的指令优先于本文的通用规则。"

> 技能系统受 `skill` feature 门控——角色未启用 `skill` 时 `SkillMgr` 与 `load_skill` 工具不生效。`coding` 提供内置工作流技能；用户也可在 `~/.agent/skills/` 或项目 `.agent/skills/` 自建技能。`onboard` 生成的任务范式属于项目用户技能，重启并切回启用 `skill` 的角色后以 `user:onboard-<task-slug>` 名称加载。
