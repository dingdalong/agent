# 角色、子智能体与技能

本篇讲清框架的三个"可扩展装配单位"：**角色**（顶层组织单位）、**子智能体**（可被主 agent 委派的完整 Agent）、**技能**（按需注入的提示词包）。三者都以 `*.md`（YAML frontmatter + body）定义，共用同一套 frontmatter 解析（`parse_frontmatter` / `extract_manifest`，`src/mgr/role_mgr.py:34-147`）。

相关：feature 门控见 [architecture.md](architecture.md)；权限模式见 [permissions.md](permissions.md)；提示词拼装见 [managers.md](managers.md) 的 `PromptMgr`。

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

激活角色由 `config.yaml` 的 `role:` 键指定（`_resolve()`，`role_mgr.py:220-272`）：缺省或空值回退 `_DEFAULT_ROLE = "coding"`（`role_mgr.py:28`）；指定角色不存在时告警并回退 `coding`；连 `coding` 都不存在则无角色激活（`active == False`）。当前仓库 `config.yaml` 设为 `role: mijia`。

### `role.md` 的结构与作用

`role.md` 经 `extract_manifest(..., id_field="agent_type", default_id="main")` 解析为 `AgentManifest`（`role_mgr.py:150-168`）：

- **body** → 成为主 agent 的**核心身份提示词**（`PromptMgr._build_core` 的"# 核心身份"段）。
- **frontmatter**：`agent_type` 对角色固定视为 `"main"`；`description`、`features`（启用的 feature 集）、`thinking`（默认思考开关）、`model`、`permissionMode` 等字段同子 agent（见下表）。

角色目录内其他资产由 `RoleMgr` 暴露路径（仅在目录/文件存在时返回，否则 `None`）：

| 方法 | 资产 | 用途 |
|---|---|---|
| `agent_md_path()` | `AGENT.md` | 角色级行为准则 → "# 行为准则"段 |
| `agents_dir()` | `agents/*.md` | 角色专属子 agent |
| `skills_dir()` | `skills/*/SKILL.md` | 角色专属技能 |
| `plugins_dir()` | `plugins/` | 角色专属插件 |
| `mcp_servers_path()` | `mcp_servers.json` | 角色专属 MCP server（见 [mcp-and-hooks.md](mcp-and-hooks.md)） |

### `common/` 共享目录

`src/roles/common/` 不是角色，而是**对所有角色生效的最低优先级共享层**（`RoleMgr` 的 `common_*` 系列方法，`role_mgr.py:337-355`，基于 `common_role_dir()`）。其 `agents/`、`skills/`、`AGENT.md` 会被叠加到任意激活角色之下（后续层同名覆盖）。当前 `common/agents/` 提供四个通用子 agent：`explore`、`general-purpose`、`plan`、`shell`。

### 内置角色一览

| 角色 | `thinking` | `features` | 子 agent（`agents/`） | 说明 |
|---|---|---|---|---|
| `coding` | `true` | 未声明（全部启用） | coder、debug、doc、review | 通用编程助手（默认角色） |
| `mijia` | `false` | `[subagent]` | device-control、home-diagnostics、home-status、scene-automation | 米家智能家居管家；仅启用 subagent feature（无 file/memory/plan 等） |

> `mijia` 声明 `features: [subagent]`，故 `task`/`skill`/`file`/`memory`/`plan` 均关闭——`MemoryMgr`/`PlanMgr`/`FileMgr` 等在 `create_app()` 中注入 `None`，其工具与提示词段随之从 schema 排除。

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
| `permissionMode` | str | `None`（回退 `default_mode`） | 该子 agent 固定权限模式，经 `parse_permission_mode` 解析；非法值告警忽略 |
| `thinking` | bool | `None`（继承父 agent） | 是否启用思考；仅 bool 有效 |
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
6. 解析 feature：`features is None` → 继承父 agent 已解析集。
7. `Agent.from_manifest(...)` 构造子 agent 实例（`is_subagent=True`）。
8. 触发 `SubagentStart` hook（若有）→ 发 `SubagentLifecycle(phase="start")` 事件 → `await agent.run(prompt)` → `finally` 发 `phase="end"` 事件 → 触发 `SubagentStop` hook。
9. `SubagentStop` 若 `blocked` 则用 `block_reason` 覆盖结果；若有 `additional_context` 则追加到结果末尾。

> 子 agent **无 plan 能力**：四个 plan 工具标记 `subagent=False`，被 `resolve_subagent_tools` 强制排除；权限模式在构造时固定，故并发子 agent 互不干扰（见 [permissions.md](permissions.md)）。

### 子智能体清单（当前仓库）

**共享（`common/`，所有角色可用）**

| agent_type | tools | model | permissionMode | 用途 |
|---|---|---|---|---|
| `explore` | 只读检索 + `web_search`/`web_fetch` | `default` | `dontAsk` | 只读探索代码/架构、联网研究并总结证据 |
| `general-purpose` | 全部（未声明） | `default` | `default` | 无专用 agent 匹配时的兜底任务执行 |
| `plan` | 只读检索（无写） | `best` | `dontAsk` | 架构设计与实现方案规划 |
| `shell` | `shell` | `fast` | `default` | 独立上下文运行命令 / Git 查询 / 测试执行 |

**coding 角色**

| agent_type | tools | model | permissionMode | 用途 |
|---|---|---|---|---|
| `coder` | 只读检索 + 写文件三件套 + `shell` | `best` | `default` | 实现功能 / 修 bug / 重构 / 写测试 |
| `debug` | 只读检索 + `shell` | `best` | `default` | 复现问题、定位根因、给诊断报告 |
| `doc` | 只读检索 + 写文件三件套 | `default` | `acceptEdits` | 编写/维护文档 |
| `review` | 只读检索（无写） | `best` | `dontAsk` | 只读代码审查 |

**mijia 角色**

| agent_type | tools | model | permissionMode | 用途 |
|---|---|---|---|---|
| `device-control` | 只读检索* | `default` | `default` | 执行设备控制并验证状态 |
| `home-diagnostics` | 只读检索* | `default` | `dontAsk` | 只读诊断家居问题 |
| `home-status` | 只读检索* | `default` | `dontAsk` | 只读查询设备状态/布局/能力 |
| `scene-automation` | 只读检索* | `default` | `default` | 创建/编辑/删除/执行场景与自动化 |

> \* mijia 各 agent frontmatter 声明的 `tools` 均为内置只读检索工具集；实际的米家设备操作能力来自角色的 `mcp_servers.json` 注入的 MCP 工具（见 [mcp-and-hooks.md](mcp-and-hooks.md)），这些工具不在 frontmatter 白名单内时会经 `resolve_subagent_tools` 的 subagent 注入规则处理。"只读检索"指 `list_directory, find_files, search_files, get_file_info, read_file`。

## 技能系统（Skills）

技能是**按需注入系统提示词的知识包**，由 `SkillMgr`（`src/mgr/skill_mgr.py`）加载，通过 `load_skill` 工具注入。适合"任务匹配时才需要的详细操作指南"，避免长期占用上下文。

### 多层扫描

`SkillMgr._load_all()`（`skill_mgr.py:44-88`）按低→高优先级扫描（同名 `namespace:name` 后者覆盖）：

```
共享 skills → 角色 skills → 全局 plugins → 全局 skills → 项目 plugins → 项目 skills
```

每个技能是一个含 `SKILL.md` 的目录（`rglob("SKILL.md")`）。技能名带命名空间前缀 `<namespace>:<name>`（`namespace` 为 `common`/角色名/`user`/插件名，`skill_mgr.py:99`）。

### `SKILL.md` 格式与注入

- frontmatter：`name`（缺省取父目录名）、`description`（缺省 `"没有说明内容"`）。
- `load_full_text(name)`（`skill_mgr.py:155`）返回包装文本：`<skill name=... skill_dir=...>` + body + 目录内其他文件的 `<skill-file path=... ref=... />` 清单 + `</skill>`。技能目录内的附属文件被登记为可引用资源（`skill_mgr.py:111-117`）。
- `prompt_section()`（`skill_mgr.py:140-150`）生成"# 可用技能"段（技能名+描述列表）与使用流程说明："当任务匹配某个技能时，调用 `load_skill` 加载后再执行操作。已加载技能的指令优先于本文的通用规则。"

> 技能系统受 `skill` feature 门控——角色未启用 `skill` 时 `SkillMgr` 与 `load_skill` 工具不生效。当前内置角色未附带 `SKILL.md`（`skills/` 目录为空），技能主要供用户在 `~/.agent/skills/` 或项目 `.agent/skills/` 自建。
