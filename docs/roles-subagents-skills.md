# 角色、子智能体与技能

本篇讲清框架的三个"可扩展装配单位"：**角色**（顶层组织单位）、**子智能体**（可被主 agent 委派的完整 Agent）、**技能**（按需注入的提示词包）。三者都以 `*.md`（YAML frontmatter + body）定义，共用 `src/mgr/role_mgr.py` 的 `parse_frontmatter()` / `extract_manifest()` 解析。

相关：feature 门控见 [architecture.md](architecture.md)；统一授权和 Plan 见 [permissions.md](permissions.md)；提示词拼装见 [managers.md](managers.md) 的 `PromptMgr`。

## 角色系统（Roles）

### 统一执行与协作范式

主 agent 持续负责目标理解、关键决策、执行、验收和交付。PromptMgr 注入统一执行原则，SubAgentMgr 在可委派时注入协作规则；TaskManager 只管理任务进度与依赖。自定义角色同样获得这些指引，无需复制策略。

角色定义领域职责和能力边界，技能定义步骤、产物和验收；只有明确的上下文隔离或独立核验需求才指定委派步骤。子 agent 的 description 说明能力与输入输出，不要求主 agent 优先委派。主 agent 已掌握上下文、步骤紧密关联或下一步被阻塞时直接推进；独立任务、大量中间输出和独立核验按需委派。

task_delegator 等待结果返回，同轮独立调用可以并行，全部结束后主 agent 继续推理。每次创建新子 agent，必要上下文通过任务正文与共享摘要传入；需要用户裁决时返回具体缺口，主 agent 沟通后发起新的完整委派。独立审核传 shared_context="none"，主 agent 按实际产物和证据验收。

coding 的 plan-workflow 在主对话形成方案，execute-plan 接续计划并连续实现与验证；mijia 的设备操作与用户确认由主 agent 连续负责。任务跟踪按可验收结果建立，不按子 agent 数量机械拆分。

**一套角色决定了主 agent 的身份提示词、可用子 agent、技能、MCP server 与启用的 feature 集**——它是框架的顶层组织单位。由 `RoleMgr`（`src/mgr/role_mgr.py`）管理。

### 三层发现与激活

`RoleMgr` 按低→高优先级扫描内置 `src/roles/`、全局 `~/.agent/roles/`、可信项目 `<workdir>/.agent/roles/`，同名后者覆盖。目录必须含 `role.md`；目录名作为配置 mapping key 原样使用，允许 Unicode、点号和长名称。`common` 与 `default` 是保留名，不可激活；`src/roles/common/` 是共享资源层，不是角色。

激活角色由 `config.yaml` 的 `role.default` 指定：缺省、非字符串或空值回退 `coding`；指定角色未发现时也告警并回退 `coding`；连 `coding` 都不存在时无角色激活。模型按角色配置为两个必填槽位：

```yaml
role:
  default: reviewer
  reviewer:
    model:
      default: anthropic/claude-opus-5
      fast: deepseek/deepseek-v4-flash
    reasoning_effort: xhigh
```

每个可能被激活的角色都必须在全局或可信项目配置中同时提供 `role.<角色>.model.default` 与 `.fast`，内置 `src/config.yaml` 不提供模型兜底。主 agent 恒用 default 槽位；`reasoning_effort` 是角色级单值，两个槽位共用。角色配置中的合法 effort 覆盖 `role.md`；缺键或 `null` 保留 manifest 值，非法值告警并忽略，最终都未声明时使用 Provider 类默认值 `max`。

### `role.md` 的结构与作用

`role.md` 经 `parse_frontmatter()` 与 `extract_manifest()` 解析为 `AgentManifest`：

- **body**：成为主 agent 的核心身份与主控职责提示词；仅主 agent 的身份、委派职责与工作流写在这里。
- **frontmatter**：可声明 `description`、`features`、`thinking`、`reasoning_effort`、`memory`、`tools`、`startInPlanMode` 等；`agent_type` 固定视为 `main`。
- **禁止 `model`**：主 agent 恒用角色的 default 槽位。frontmatter 只要出现 `model` 键，`RoleMgr` 就抛 `LLMConfigurationError`，错误包含 `role.md` 路径和应使用的两个配置键。

角色目录内其他资产由 `RoleMgr` 暴露路径（仅在目录/文件存在时返回，否则 `None`）：

| 方法 | 资产 | 用途 |
|---|---|---|
| `agent_md_path()` | `AGENTS.md` | 激活角色内主/子 agent 共用的行为准则 |
| `agents_dir()` | `agents/*.md` | 角色专属子 agent |
| `skills_dir()` | `skills/*/SKILL.md` | 角色专属技能 |
| `plugins_dir()` | `plugins/` | 角色专属插件 |
| `mcp_servers_path()` | `mcp_servers.json` | 角色专属 MCP server |

### `common/` 共享目录

`src/roles/common/` 对所有角色生效，作为最低优先级共享层叠加其 `agents/`、`skills/`、`AGENTS.md`。后续角色、全局和项目层可覆盖同名子 agent 或技能。

### 内置角色一览

| 角色 | 初始 Plan | features | 专属执行器 | 工作方式 |
|---|---|---|---|---|
| `coding` | `true` | 未声明（全部启用） | `coder`、`review` | 主 agent 连续规划、实现、调试与验证 |
| `mijia` | `false` | `[subagent, skill]` | 复用 common | 主 agent 通过技能操作、诊断设备与管理场景 |
| `onboard` | `false` | `[subagent, file, task, skill]` | `repository-map`、`evidence-analyst`、`evidence-reviewer` | 证据流水线，主 agent 验收并发布 |

角色模型来自运行配置的 default/fast 槽位。`coding` 与 `mijia` 不设静态工具白名单；`onboard` 的主 agent 只声明状态、候选文件、Git 查询、技能、任务和委派所需工具，代码图索引由专用执行器承担。

插件提供 skill 和 hook；插件 skill 要求启用 `skill` feature。米家使用实际注册的 MCP 工具，无需修改内置资源回填工具名。

### onboard 证据流水线

主 agent 维护范围、快照、状态和验收，默认加载 `builtin:onboard-write-manual` 编写或修订候选，并亲自执行发布。需要隔离大量证据综合时，将完整输入与技能名交给 `general-purpose`。分析与审核执行器每次加载一个阶段技能：

| 阶段 | 执行器 / 技能 | 正式产物 |
|---|---|---|
| 索引、地图、分片 | `repository-map` | `.agent/onboard/evidence/repository-map.md`、`.agent/onboard/shard-plan.md` |
| MAP | `evidence-analyst` + `builtin:onboard-analyze-module` | `.agent/onboard/cards/<shard_id>.md` |
| 跨模块消解 | `evidence-analyst` + `builtin:onboard-resolve-relations` | `.agent/onboard/evidence/cross-module.md` |
| REDUCE | `evidence-analyst` + `builtin:onboard-classify-evidence`，每次一个维度 | 四份维度证据报告 |
| 分类核实 | 新的 `evidence-reviewer` + `builtin:onboard-verify-evidence` | `.agent/onboard/evidence/verification.md` |
| 候选编写 | 主 agent 或通用执行器 + `builtin:onboard-write-manual` | generated-rules、generated-skills、reference、decisions |
| 候选审核 | 新的 `evidence-reviewer` + `builtin:onboard-review-manual` | `.agent/onboard/quality-report.md` |

分片任务同轮独立运行，跨模块关系集中核对后供四维度共享，避免重复探索。分类核实逐项打开源码检查 `dominant/conflict/unknown`；候选审核反查活跃规则及技能的证据映射。两类核验均通过 `shared_context="none"` 隔离先前摘要，不能由分析或编写实例自审替代。详细阶段方法和四维度 references 只在执行时按需读取。

同一未发布快照下，跨模块、四个维度和分类核实各自维护 `{status}`；只复用状态为 completed、正式产物完整、快照/范围/深度一致且覆盖满足要求的阶段。四个维度单个损坏只重跑自己；任一阶段实际重跑，必须把该阶段之后的全部阶段一并置 `pending` 并重跑。报告通过 `.partial` 完整写入后用 `exec_command` 执行 `mv` 发布；残留 partial 不算完成。每个新会话重建任务图，不复用旧 task id。

候选只使用最终等级为 `confirmed` 的证据，正文与审核证据侧车分离。PASS 必须绑定仓库快照和候选内容标识；任何候选修改都需要新的独立审核。发布前重算并比对两者，检查人工规则和目标技能冲突；全部通过后先写技能、最后更新根 `AGENTS.md`，原样发布候选并保留人工区。

已成功发布、发布阶段中断、快照或范围变化时不得增量更新，要求手动清理 onboard 产物后全量重跑。Git 不可用时不复用旧阶段，发布必须取得用户明确批准。全部失败与恢复契约见 onboard 角色和共同准则。

活跃产物为根 `AGENTS.md` 的 onboard 受管区块及 `.agent/skills/onboard/<task-slug>/SKILL.md`；证据、候选、质量报告与状态保存在 `.agent/onboard/`。onboard 本轮只加载内置流水线技能，被分析项目的技能作为证据数据。生成的项目规则在 `/clear` 后重新加载；项目技能重启应用并切回 `coding` 后以 `user:onboard-<task-slug>` 使用。

## 子智能体（Subagents）

子智能体是**共享同一 `AgentDeps` 的完整 `Agent` 实例**，由主 agent 通过 `task_delegator` 工具委派。定义为 `agents/*.md`，由 `SubAgentMgr`（`src/mgr/subagent_mgr.py`）加载。

### 四层扫描

`SubAgentMgr._load_all()` 按低→高优先级扫描，同名 `agent_type` 后者覆盖：

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
| `description` | str | `"没有说明内容"` | 出现在主 agent 的可用子智能体提示词段 |
| `tools` | 逗号分隔 str | 空 → `None`（全部工具） | 执行期工具声明边界；不改变统一 schema |
| `model` | str | `None`（解析角色 default 槽位） | 只允许 `default`、`fast`、`opus`、`sonnet`、`haiku` 或`供应商/模型ID` |
| `startInPlanMode` | bool | `False` | 独立构造时的初始 Plan 状态；经 `task_delegator` 构造时由父 Agent 当前状态覆盖 |
| `thinking` | bool | `None`（继承父 agent） | 是否启用思考；仅 bool 有效 |
| `reasoning_effort` | str | `None`（继承父 agent 已解析值） | 合法值 `low`/`medium`/`high`/`xhigh`/`max`；字符串会去空白并转小写，非法值告警后视为未声明 |
| `memory` | str | `None` | 记忆范围（如 `project`），控制 `MemoryMgr` 注入 |
| `features` | YAML 列表 | `None`（继承父 agent 已解析集） | 该子 agent 的 feature 集；空列表 = 全部禁用 |

模型别名固定映射为 `opus`/`sonnet` → `default`，`haiku` → `fast`。`SubAgentMgr` 加载 manifest 时只校验别名或 `供应商/模型ID` 格式，不查询候选列表；非法格式抛 `LLMConfigurationError`，消息包含定义文件路径和合法格式。字段缺失、`null`、空字符串或纯空白均视为未设置，委派时由 `LLMMgr.get(None)` 解析到 default 槽位；其他非字符串类型直接报错。

### 委派流程 `task_delegator`

1. 查表定位 `manifest`，不存在则返回错误文本（含可用列表）。
2. 若带 `task_id`：委派前把任务标记 `in_progress` 并设 `owner`；子 agent 异常退出或返回 LLM 错误时回滚为 `pending`，正常返回不自动标 `completed`。
3. 把 `manifest.tools` 原样传入 Agent，供 `PermissionManager` 在执行期检查；schema 保持完整。
4. 模型直接传 `manifest.model`；`None` 由 `LLMMgr` 解析 default，合法别名解析对应槽位，完整模型 ID 精确使用。
5. `thinking` 未声明时继承父 agent。
6. `reasoning_effort` 自身合法声明优先；否则依次继承 `parent_agent.reasoning_effort`、父 agent Provider 的 `reasoning_effort`。继承的是父 agent 已解析的有效值，与子 agent 选择 default 还是 fast 槽位无关。
7. `features` 未声明时继承父 agent 已解析集；同时继承父 agent 当前 `mode`。
8. 用 `Agent.from_manifest(is_subagent=True, ...)` 构造完整子 agent，触发 start hook/事件，运行后在 `finally` 发 end 事件，再触发 stop hook。
9. **上下文交接**：`run()` 前把 `ContextMgr.digest()` 拼到 `prompt` 之前（子 agent 的 history 从空开始，这是它唯一能拿到「别人已核实了什么」的通道）；`SubagentStop` hook 之后把最终 `result` 记入账本，供后续委派复用。委派时传 `shared_context="none"` 可完全隔离，用于需要独立判断的场景（如代码审查）。详见 [managers.md](managers.md#contextmgr--跨-agent-共享上下文)。

> 所有子 Agent 接收与主 Agent 相同的 schema。`submit_plan` 通过 `ToolAudience.MAIN_ONLY` 禁止子 Agent 执行；子 Agent 继承父 Agent 当前 `mode`，模式限制同样由授权层执行。

> 共享上下文的注入点刻意选在 `task_delegator` 而非 `ReminderMgr`：后者的 provider 只收 `(mode, is_subagent)`，要按委派过滤就得在进程级单例上存槽位，而并行委派会互相覆盖它。


### 子智能体执行边界

| 来源 | 执行器 | 工具边界与用途 |
|---|---|---|
| common | `explore` | 文件只读与网络调查，隔离搜索输出 |
| common | `general-purpose` | 未设置静态白名单，组合实际工具和技能完成独立任务 |
| common | `shell` | Shell 命令输出隔离，使用 fast 槽位 |
| coding | `coder` | 文件检索、编辑与命令验证，执行限定实现任务 |
| coding | `review` | 文件只读，独立核验改动 |
| onboard | `repository-map` | 报告写入、只读 Git、代码图索引与架构查询 |
| onboard | `evidence-analyst` | 报告写入、只读 Git、代码图查询；按技能分析证据 |
| onboard | `evidence-reviewer` | 核实/质量报告写入、只读 Git、代码图查询；独立形成判定 |

common 为所有角色的最低优先级层；删除角色同名定义后会使用 common 定义。米家复用通用执行器与领域技能。工具仍经过子 agent 隔离、feature 过滤与权限服务，不因加载 skill 扩大白名单。

onboard 专用执行器声明 `features: [file, skill]`，不继承主 agent 的 task 或 subagent feature。MCP 工具不自动注入，专用执行器显式列出所需工具；分析和审核白名单不包含索引工具。报告写入路径与只读 Git 用途由阶段契约及既有权限机制约束，不能将提示词范围误认为独立文件系统沙箱。

## 技能系统（Skills）

技能是**按需加载到调用者上下文的方法包**，由 `SkillMgr`（`src/mgr/skill_mgr.py`）加载，通过 `load_skill` 工具注入。适合"任务匹配时才需要的详细操作指南"，避免长期占用上下文。

### 多层扫描

`SkillMgr._load_all()`（`skill_mgr.py:44-88`）按低→高优先级扫描（同名 `namespace:name` 后者覆盖）：

```
共享 skills → 角色 skills → 全局 plugins → 全局 skills → 项目 plugins → 项目 skills
```

每个技能是一个含 `SKILL.md` 的目录（`rglob("SKILL.md")`）。技能名带命名空间前缀 `<namespace>:<name>`：共享与角色技能使用 `builtin`，全局和项目用户技能使用 `user`，插件技能使用插件名（`skill_mgr.py:16,99`）。

### `SKILL.md` 格式与注入

- frontmatter：`name`（缺省取父目录名）、`description`（缺省 `"没有说明内容"`）、`listed`（缺省 `true`；为 `false` 时可按精确名称加载但不进入通用目录）。
- `load_full_text(name)`（`skill_mgr.py:155`）返回包装文本：`<skill name=... skill_dir=...>` + body + 目录内其他文件的 `<skill-file path=... ref=... />` 清单 + `</skill>`。技能目录内的附属文件被登记为可引用资源（`skill_mgr.py:111-117`）。
- `prompt_section()` 生成已列出技能的名称、描述与加载指导；目录作为首次 chat 前的外部 user 上下文，正文只由 `load_skill` 工具结果进入调用者历史，二者都不进入 system/developer。Plan 控制技能使用 `listed: false`，因此普通模式目录不会泄露计划工作流名称。

> 技能系统受 `skill` feature 门控——角色未启用 `skill` 时 `SkillMgr` 与 `load_skill` 工具不生效。`coding` 提供内置工作流技能；用户也可在 `~/.agent/skills/` 或项目 `.agent/skills/` 自建技能。`onboard` 生成的任务范式属于项目用户技能，重启并切回启用 `skill` 的角色后以 `user:onboard-<task-slug>` 名称加载。


### 方法复用与委派

主 agent 默认连续推进工作；需要独立上下文、输出隔离或独立核验时才选择执行器。委派正文提供目标、范围、必要输入、完整技能名与验收条件，子 agent 再调用 `load_skill`。不自动继承主 agent 已加载的技能正文。

`load_skill` 的 availability 为 `ToolAudience.ALL` 且要求 `skill` feature，因此主、子 agent 都可执行但仍受 feature 检查。加载技能不修改工具集、模式或委派权限；`plan-workflow`、`execute-plan` 不进入通用目录，只能由已知其精确名称的控制流程加载。

编码排障加载 `builtin:debugging`，普通文档同步随实现完成；米家控制、诊断和场景管理分别加载 `builtin:control-devices`、`builtin:diagnose-home`、`builtin:manage-scenes`。简单查询与命令无需额外工作流。技能不存在时返回包含可用名称的错误，不回退到其他技能。
