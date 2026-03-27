# Plan 模块重构设计

## 概述

将 plan 模块从"生成+执行一体"重构为"轻量编排层"：plan 只负责生成和管理计划，执行完全委托给 GraphEngine。不考虑兼容性和迁移过渡。

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 架构方向 | 精简为轻量编排层 | plan executor 与 GraphEngine 大量重复（并行执行、依赖解析、step 路由），消除重复 |
| 触发方式 | orchestrator 自动判断 + /plan 斜杠命令 | 灵活，简单任务自动，复杂任务可手动 |
| 确认流程 | 保留完整流程（澄清→生成→确认/调整→执行） | 多步计划涉及工具调用，用户确认是安全底线 |
| 变量语法 | 保留 $step_id.field，compiler 层转换为 state 读取 | LLM 和用户可读，底层统一到 RunContext.state |

## 架构

### 数据流

```
用户输入
  → orchestrator 判断是否需要 plan（或 /plan 触发）
    → planner: 澄清 → 生成 Plan → 用户确认/调整
      → compiler: Plan → CompiledGraph（解析 $step_id.field 为 state 读取）
        → GraphEngine.run(compiled_graph) → 结果返回用户
```

### 文件结构

```
src/plan/
  models.py      # Plan, Step 数据模型（瘦身）
  planner.py     # LLM 生成/澄清/调整计划（保留，微调）
  compiler.py    # 新增：Plan → CompiledGraph 转换 + 变量解析
  exceptions.py  # 保留，精简
```

## 组件设计

### 1. models.py — 数据模型

Step 简化，去掉 `action` 字段，用字段有无区分类型：

```python
class Step(BaseModel):
    id: str                          # 唯一标识，如 "search"
    description: str                 # 人类可读描述
    tool_name: str | None = None     # 有值 → FunctionNode（工具调用）
    tool_args: dict = {}             # 工具参数，支持 $step_id.field
    agent_name: str | None = None    # 有值 → AgentNode（子任务委托给 agent）
    agent_prompt: str | None = None  # agent 的指令
    depends_on: list[str] = []       # 依赖的 step id

class Plan(BaseModel):
    steps: list[Step]
    context: dict = {}               # 初始上下文
```

对比当前变化：
- 删除 `action` 字段（"tool" / "subtask" / "user_input"）
- `subtask` 变成 `agent_name` + `agent_prompt`，可指定委托给哪个 agent
- 去掉 `user_input` 类型——需要用户输入的场景由 agent 的 tool loop 自然处理
- 删除 `subtask_prompt` 字段，替换为 `agent_prompt`

### 2. compiler.py — Plan → CompiledGraph 转换

新增核心组件：

```python
class PlanCompiler:
    def __init__(self, agent_registry: AgentRegistry, tool_router: ToolRouter): ...

    def compile(self, plan: Plan) -> CompiledGraph:
        """Plan → CompiledGraph，三步走：
        1. 每个 Step → GraphNode
           - tool_name 有值 → FunctionNode（包装为调用 tool_router 的函数）
           - agent_name 有值 → AgentNode（从 registry 取 agent）
        2. depends_on → Edge
           - 同层无依赖的 steps → ParallelGroup（自动并行）
           - 有依赖关系的 → 顺序 Edge
        3. 变量解析
           - 扫描 tool_args 中的 $step_id.field
           - 生成包装函数：执行前从 state[step_id][field] 读取，替换参数
        """
```

变量解析示例：

```python
# Plan 中 LLM 生成的：
Step(id="translate", tool_name="translate",
     tool_args={"text": "$search.results", "lang": "zh"})

# Compiler 生成的 FunctionNode 包装函数：
async def translate_wrapper(ctx: RunContext):
    args = {"text": ctx.state["search"]["results"], "lang": "zh"}
    return await tool_router.execute("translate", args)
```

复用现有 `resolve_variables` 的递归解析逻辑，底层数据源从 plan context dict 换成 `RunContext.state`。

### 3. planner.py — 计划生成

改动较小：

- `generate_plan` 签名新增 `available_agents: list[str]` 参数
- system prompt 调整：去掉 `action` 字段说明，改为 `tool_name` / `agent_name` 区分；添加可用 agent 列表
- `adjust_plan` 同步加入 `available_agents` 参数
- `check_clarification_needed` 和 `classify_user_feedback` 不变

### 4. exceptions.py — 异常精简

- 保留：`PlanError`、`JSONParseError`、`APIGenerationError`
- 删除：`DependencyError`、`StepExecutionError`、`VariableResolutionError`（属于执行层，由 GraphEngine 处理）
- 新增：`CompileError`（compiler 转换失败：引用不存在的 tool/agent、循环依赖等）

### 5. main.py — 集成

两个触发入口统一到同一个流程：

**a) /plan 斜杠命令**：创建 `skills/plan/SKILL.md`，注册为 slash command。

**b) orchestrator 自动判断**：注册 `create_plan` tool，orchestrator instructions 中说明复杂多步任务应调用此工具。

**c) 计划流程函数**：

```python
async def run_plan_flow(
    user_input: str,
    tool_router: ToolRouter,
    agent_registry: AgentRegistry,
    engine: GraphEngine,
) -> str:
    # 1. 澄清循环（最多 PLAN_MAX_CLARIFICATION_ROUNDS 轮）
    # 2. generate_plan()
    # 3. 展示计划给用户，等待确认/调整（最多 PLAN_MAX_ADJUSTMENTS 轮）
    # 4. PlanCompiler.compile(plan) → CompiledGraph
    # 5. engine.run(graph) → 返回结果
```

计划流程用普通异步函数而非 graph node，因为涉及多轮用户交互（澄清、确认），graph 更适合执行阶段的编排。

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/plan/models.py` | 修改 | Step 瘦身：去 action，加 agent_name/agent_prompt |
| `src/plan/planner.py` | 修改 | 加 available_agents 参数，调整 prompt |
| `src/plan/compiler.py` | 新增 | Plan → CompiledGraph 转换 + 变量解析 |
| `src/plan/executor.py` | 删除 | 执行逻辑由 GraphEngine 替代 |
| `src/plan/exceptions.py` | 修改 | 精简 + 加 CompileError |
| `src/plan/__init__.py` | 修改 | 更新导出 |
| `main.py` | 修改 | 加 run_plan_flow + 两个触发入口 |
| `skills/plan/SKILL.md` | 新增 | /plan 斜杠命令 |
| `config.py` | 修改 | 删除 PLAN_DEFAULT_TIMEOUT、PLAN_MAX_VARIABLE_DEPTH |
| `tests/plan/test_executor.py` | 删除 | 对应 executor 删除 |
| `tests/plan/test_compiler.py` | 新增 | compiler 测试 |
| `tests/plan/test_models.py` | 修改 | 适配新 Step 模型 |
| `tests/plan/test_planner.py` | 修改 | 适配新参数 |
