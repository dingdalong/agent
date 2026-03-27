# main.py 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 main.py 从 345 行的单体文件拆分为职责清晰的模块，实现 I/O 解耦以支持未来 Web 接入。

**Architecture:** 引入 `UserInterface` 协议实现 I/O 抽象，将 Agent 定义、计划编排、应用启动分别移入独立模块，最终 main.py 仅保留 ~15 行入口代码。核心类 `AgentApp` 接收 `UserInterface` 注入，Web 接入时只需提供新的 interface 实现。

**Tech Stack:** Python 3.13, Pydantic, asyncio, pytest

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/interfaces/__init__.py` | Package exports |
| Create | `src/interfaces/base.py` | `UserInterface` 协议定义 |
| Create | `src/interfaces/cli.py` | CLI 实现 |
| Create | `tests/interfaces/__init__.py` | Test package |
| Create | `tests/interfaces/test_cli.py` | CLIInterface 测试 |
| Create | `src/agents/deps.py` | `AgentDeps` 模型 |
| Modify | `src/agents/__init__.py` | 新增 `AgentDeps` 导出 |
| Create | `src/agents/definitions.py` | Agent 定义 + 图构建 |
| Create | `tests/agents/test_definitions.py` | definitions 测试 |
| Create | `src/plan/flow.py` | `PlanFlow` 计划编排 |
| Modify | `src/plan/__init__.py` | 新增 `PlanFlow` 导出 |
| Create | `tests/plan/test_flow.py` | PlanFlow 测试 |
| Create | `src/app.py` | `AgentApp` 应用核心 |
| Create | `tests/test_app.py` | AgentApp 测试 |
| Rewrite | `main.py` | 瘦入口 (~15行) |
| Modify | `tests/core/test_main.py` | 更新/重写 main 测试 |
| Create | `src/memory/utils.py` | `build_collection_name` 工具函数 |

---

### Task 1: UserInterface 协议 + CLIInterface

**Files:**
- Create: `src/interfaces/__init__.py`
- Create: `src/interfaces/base.py`
- Create: `src/interfaces/cli.py`
- Create: `tests/interfaces/__init__.py`
- Create: `tests/interfaces/test_cli.py`

- [ ] **Step 1: Write failing test for CLIInterface**

```python
# tests/interfaces/__init__.py
(empty)

# tests/interfaces/test_cli.py
"""Tests for CLIInterface."""
import pytest
from unittest.mock import patch, AsyncMock
from src.interfaces.base import UserInterface
from src.interfaces.cli import CLIInterface


class TestCLIInterface:

    def test_implements_protocol(self):
        cli = CLIInterface()
        assert isinstance(cli, UserInterface)

    @pytest.mark.asyncio
    async def test_display(self, capsys):
        cli = CLIInterface()
        await cli.display("hello world")
        captured = capsys.readouterr()
        assert captured.out == "hello world"

    @pytest.mark.asyncio
    async def test_prompt(self):
        cli = CLIInterface()
        with patch("asyncio.to_thread", new_callable=AsyncMock, return_value="user reply"):
            result = await cli.prompt("Enter: ")
            assert result == "user reply"

    @pytest.mark.asyncio
    async def test_confirm_yes(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="y"):
            assert await cli.confirm("Continue?") is True

    @pytest.mark.asyncio
    async def test_confirm_no(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="n"):
            assert await cli.confirm("Continue?") is False

    @pytest.mark.asyncio
    async def test_confirm_chinese(self):
        cli = CLIInterface()
        with patch.object(cli, "prompt", new_callable=AsyncMock, return_value="确认"):
            assert await cli.confirm("Continue?") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/interfaces/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.interfaces'`

- [ ] **Step 3: Implement UserInterface protocol and CLIInterface**

```python
# src/interfaces/__init__.py
"""Interfaces — I/O 抽象层。"""

from src.interfaces.base import UserInterface
from src.interfaces.cli import CLIInterface

__all__ = ["UserInterface", "CLIInterface"]
```

```python
# src/interfaces/base.py
"""UserInterface 协议 — 抽象所有用户交互操作。"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class UserInterface(Protocol):
    """I/O 抽象协议，CLI 和 Web 各自实现。"""

    async def prompt(self, message: str) -> str:
        """获取用户输入，message 作为提示语。"""
        ...

    async def display(self, message: str) -> None:
        """展示信息给用户。"""
        ...

    async def confirm(self, message: str) -> bool:
        """请求用户确认，返回 True/False。"""
        ...
```

```python
# src/interfaces/cli.py
"""CLIInterface — 命令行交互实现。"""

import asyncio

from src.interfaces.base import UserInterface


class CLIInterface:
    """基于标准输入/输出的 CLI 交互实现。"""

    async def prompt(self, message: str) -> str:
        return await asyncio.to_thread(input, message)

    async def display(self, message: str) -> None:
        print(message, end="", flush=True)

    async def confirm(self, message: str) -> bool:
        response = await self.prompt(f"{message} (y/n): ")
        return response.strip().lower() in ("y", "yes", "确认")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/interfaces/test_cli.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/interfaces/ tests/interfaces/
git commit -m "feat: add UserInterface protocol and CLIInterface"
```

---

### Task 2: AgentDeps 模型

**Files:**
- Create: `src/agents/deps.py`
- Modify: `src/agents/__init__.py`
- Test: `tests/agents/test_context.py` (existing, verify no break)

- [ ] **Step 1: Write failing test for AgentDeps**

Add to existing `tests/agents/test_context.py`:

```python
# 在文件末尾追加

from src.agents.deps import AgentDeps


class TestAgentDeps:

    def test_default_fields_are_none(self):
        deps = AgentDeps()
        assert deps.tool_router is None
        assert deps.agent_registry is None
        assert deps.graph_engine is None
        assert deps.ui is None

    def test_accepts_arbitrary_types(self):
        """AgentDeps should accept non-serializable types via ConfigDict."""
        class FakeRouter:
            pass
        deps = AgentDeps(tool_router=FakeRouter(), ui=FakeRouter())
        assert deps.tool_router is not None
        assert deps.ui is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agents/test_context.py::TestAgentDeps -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agents.deps'`

- [ ] **Step 3: Create AgentDeps module**

```python
# src/agents/deps.py
"""AgentDeps — Agent 运行时外部依赖模型。"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentDeps(BaseModel):
    """外部依赖：传递给 AgentRunner、PlanFlow 等组件。

    Attributes:
        tool_router: 工具路由器
        agent_registry: Agent 注册表
        graph_engine: 图执行引擎
        ui: UserInterface 实例，用于 I/O 操作
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    tool_router: Any = None
    agent_registry: Any = None
    graph_engine: Any = None
    ui: Any = None
```

- [ ] **Step 4: Update agents __init__.py to export AgentDeps**

在 `src/agents/__init__.py` 中添加导入和导出：

在 import 区域添加:
```python
from src.agents.deps import AgentDeps
```

在 `__all__` 列表中 `"EmptyDeps"` 之后添加:
```python
    "AgentDeps",
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/agents/test_context.py -v`
Expected: All tests PASS (包括原有测试和新增的 TestAgentDeps)

- [ ] **Step 6: Commit**

```bash
git add src/agents/deps.py src/agents/__init__.py tests/agents/test_context.py
git commit -m "feat: extract AgentDeps to src/agents/deps.py with ui field"
```

---

### Task 3: Agent 定义模块

**Files:**
- Create: `src/agents/definitions.py`
- Create: `tests/agents/test_definitions.py`

- [ ] **Step 1: Write failing test for definitions**

```python
# tests/agents/test_definitions.py
"""Tests for agent definitions and graph building."""
from unittest.mock import AsyncMock
from src.agents import AgentRegistry
from src.agents.definitions import build_default_graph, build_skill_graph


class TestBuildDefaultGraph:

    def test_registers_all_agents(self):
        registry = AgentRegistry()
        build_default_graph(registry)
        expected = {"weather_agent", "calendar_agent", "email_agent", "orchestrator", "planner"}
        actual = {a.name for a in registry.all_agents()}
        assert actual == expected

    def test_returns_compiled_graph(self):
        registry = AgentRegistry()
        graph = build_default_graph(registry)
        assert graph is not None
        assert hasattr(graph, "entry")

    def test_entry_is_orchestrator(self):
        registry = AgentRegistry()
        graph = build_default_graph(registry)
        assert graph.entry == "orchestrator"


class TestBuildSkillGraph:

    def test_injects_skill_content_into_orchestrator(self):
        registry = AgentRegistry()
        build_skill_graph(registry, "你是一个代码助手。")
        orchestrator = registry.get("orchestrator")
        assert "你是一个代码助手。" in orchestrator.instructions

    def test_returns_compiled_graph(self):
        registry = AgentRegistry()
        graph = build_skill_graph(registry, "skill content")
        assert graph is not None
        assert graph.entry == "orchestrator"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agents/test_definitions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agents.definitions'`

- [ ] **Step 3: Implement definitions module**

```python
# src/agents/definitions.py
"""Agent 定义与图构建。

提供默认 agent 集合（orchestrator + specialist agents + planner）的定义和图构建。
"""

from __future__ import annotations

from src.agents.agent import Agent
from src.agents.registry import AgentRegistry
from src.agents.context import RunContext
from src.agents.graph.types import NodeResult, CompiledGraph
from src.agents.graph.builder import GraphBuilder


_ORCHESTRATOR_BASE_INSTRUCTIONS = (
    "你是一个智能助手。根据用户的请求选择合适的操作：\n"
    "- 天气相关问题，交给 weather_agent\n"
    "- 日历/日程相关问题，交给 calendar_agent\n"
    "- 邮件相关问题，交给 email_agent\n"
    "- 需要多步骤协作的复杂任务（如查天气然后发邮件），交给 planner\n"
    "- 其他问题，直接回答用户\n"
)

_SPECIALIST_AGENTS = [
    Agent(
        name="weather_agent",
        description="处理天气查询",
        instructions="你是天气助手。使用 get_weather 工具查询天气信息并回复用户。",
        tools=["get_weather"],
    ),
    Agent(
        name="calendar_agent",
        description="管理日历事件",
        instructions="你是日历助手。使用 create_event 工具帮用户管理日历事件。",
        tools=["create_event"],
    ),
    Agent(
        name="email_agent",
        description="发送邮件",
        instructions="你是邮件助手。使用 send_email 工具帮用户发送邮件。",
        tools=["send_email"],
    ),
]

_PLANNER_AGENT = Agent(
    name="planner",
    description="处理需要多步骤的复杂任务，生成计划并按步骤执行",
    instructions="",  # 不会被 AgentRunner 使用，由 FunctionNode 接管
)


def _make_planner_node_fn():
    """创建 planner 的 FunctionNode 执行函数。

    通过 ctx.deps 延迟获取 PlanFlow 所需的依赖，避免循环导入。
    """

    async def planner_node_fn(ctx: RunContext) -> NodeResult:
        from src.plan.flow import PlanFlow

        plan_flow = PlanFlow(
            tool_router=ctx.deps.tool_router,
            agent_registry=ctx.deps.agent_registry,
            engine=ctx.deps.graph_engine,
            ui=ctx.deps.ui,
        )
        result = await plan_flow.run(ctx.input)
        return NodeResult(output=result)

    return planner_node_fn


def _register_and_build(
    registry: AgentRegistry,
    skill_content: str | None = None,
) -> CompiledGraph:
    """注册 agents 并构建图。内部共享逻辑。"""
    # 注册 specialist agents
    for agent in _SPECIALIST_AGENTS:
        registry.register(agent)

    # 构建 orchestrator instructions
    instructions = _ORCHESTRATOR_BASE_INSTRUCTIONS
    if skill_content:
        instructions = f"{skill_content}\n\n{instructions}"

    orchestrator = Agent(
        name="orchestrator",
        description="总控 Agent，负责路由和直接回答",
        instructions=instructions,
        handoffs=["weather_agent", "calendar_agent", "email_agent", "planner"],
    )
    registry.register(orchestrator)
    registry.register(_PLANNER_AGENT)

    # 构建图
    graph = (
        GraphBuilder()
        .add_agent("orchestrator", orchestrator)
        .add_function("planner", _make_planner_node_fn())
        .set_entry("orchestrator")
        .compile()
    )
    return graph


def build_default_graph(registry: AgentRegistry) -> CompiledGraph:
    """构建默认 agent 图（无 skill 注入）。"""
    return _register_and_build(registry)


def build_skill_graph(registry: AgentRegistry, skill_content: str) -> CompiledGraph:
    """构建带 skill 内容注入的 agent 图。"""
    return _register_and_build(registry, skill_content=skill_content)
```

- [ ] **Step 4: Run tests to verify**

Run: `python -m pytest tests/agents/test_definitions.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agents/definitions.py tests/agents/test_definitions.py
git commit -m "feat: extract agent definitions to src/agents/definitions.py"
```

---

### Task 4: PlanFlow 计划编排

**Files:**
- Create: `src/plan/flow.py`
- Modify: `src/plan/__init__.py`
- Create: `tests/plan/test_flow.py`

- [ ] **Step 1: Write failing test for PlanFlow**

```python
# tests/plan/test_flow.py
"""Tests for PlanFlow orchestration."""
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from src.plan.flow import PlanFlow
from src.plan.models import Plan, Step


def _make_flow(ui=None):
    """Create a PlanFlow with mocked dependencies."""
    return PlanFlow(
        tool_router=Mock(get_all_schemas=Mock(return_value=[])),
        agent_registry=Mock(all_agents=Mock(return_value=[])),
        engine=Mock(),
        ui=ui or AsyncMock(),
    )


class TestFormatPlan:

    def test_tool_step(self):
        plan = Plan(steps=[
            Step(id="s1", description="查询天气", tool_name="get_weather", tool_args={"city": "广州"}),
        ])
        result = PlanFlow.format_plan(plan)
        assert "[工具]" in result
        assert "get_weather" in result

    def test_agent_step(self):
        plan = Plan(steps=[
            Step(id="s1", description="发邮件", agent_name="email_agent"),
        ])
        result = PlanFlow.format_plan(plan)
        assert "[Agent]" in result
        assert "email_agent" in result

    def test_step_with_deps(self):
        plan = Plan(steps=[
            Step(id="s1", description="查询天气", tool_name="get_weather", tool_args={}),
            Step(id="s2", description="发邮件", agent_name="email_agent", depends_on=["s1"]),
        ])
        result = PlanFlow.format_plan(plan)
        assert "依赖" in result
        assert "s1" in result


class TestPlanFlowRun:

    @pytest.mark.asyncio
    async def test_returns_string_when_no_plan_needed(self):
        flow = _make_flow()
        with patch("src.plan.flow.check_clarification_needed", new_callable=AsyncMock, return_value=None), \
             patch("src.plan.flow.generate_plan", new_callable=AsyncMock, return_value=None):
            result = await flow.run("你好")
            assert isinstance(result, str)
            assert "不需要" in result

    @pytest.mark.asyncio
    async def test_uses_ui_for_clarification(self):
        ui = AsyncMock()
        ui.prompt = AsyncMock(return_value="广州")
        flow = _make_flow(ui=ui)

        with patch("src.plan.flow.check_clarification_needed", new_callable=AsyncMock, side_effect=[
            "请问是哪个城市？",  # 第一轮需要澄清
            None,               # 第二轮信息充足
        ]), \
             patch("src.plan.flow.generate_plan", new_callable=AsyncMock, return_value=None):
            await flow.run("查天气")
            ui.display.assert_called()  # 展示了澄清问题
            ui.prompt.assert_called()   # 请求了用户输入
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/plan/test_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.plan.flow'`

- [ ] **Step 3: Implement PlanFlow**

```python
# src/plan/flow.py
"""PlanFlow — 计划编排流程。

完整流程：澄清 → 生成 → 确认/调整 → 编译 → 执行。
所有用户交互通过 UserInterface 协议，不直接依赖 CLI。
"""

from __future__ import annotations

from src.agents.context import RunContext, DictState
from src.agents.deps import AgentDeps
from src.agents.registry import AgentRegistry
from src.agents.graph.engine import GraphEngine
from src.plan.models import Plan
from src.plan.planner import (
    generate_plan,
    adjust_plan,
    classify_user_feedback,
    check_clarification_needed,
)
from src.plan.compiler import PlanCompiler
from src.tools.router import ToolRouter
from config import PLAN_MAX_CLARIFICATION_ROUNDS, PLAN_MAX_ADJUSTMENTS


class PlanFlow:
    """计划编排流程：澄清 → 生成 → 确认 → 编译 → 执行。"""

    def __init__(
        self,
        tool_router: ToolRouter,
        agent_registry: AgentRegistry,
        engine: GraphEngine,
        ui,
    ):
        self.tool_router = tool_router
        self.agent_registry = agent_registry
        self.engine = engine
        self.ui = ui

    async def run(self, user_input: str) -> str:
        """执行完整计划流程，返回结果文本。"""
        available_tools = self.tool_router.get_all_schemas()
        available_agents = [a.name for a in self.agent_registry.all_agents()]

        # 1. 澄清循环
        gathered = ""
        for _ in range(PLAN_MAX_CLARIFICATION_ROUNDS):
            question = await check_clarification_needed(user_input, gathered)
            if question is None:
                break
            await self.ui.display(f"\n{question}\n")
            answer = await self.ui.prompt("\n你: ")
            gathered += f"\n{question}\n回答: {answer}"

        # 2. 生成计划
        context = gathered if gathered else ""
        plan = await generate_plan(user_input, available_tools, available_agents, context)
        if plan is None:
            return "这个请求不需要多步计划，我直接回答。"

        # 3. 确认/调整循环
        for _ in range(PLAN_MAX_ADJUSTMENTS):
            plan_display = self.format_plan(plan)
            await self.ui.display(f"\n执行计划：\n{plan_display}\n")
            feedback_input = await self.ui.prompt("\n确认执行？(输入 '确认' 或修改意见): ")

            action = await classify_user_feedback(feedback_input, plan)
            if action == "confirm":
                break
            plan = await adjust_plan(
                user_input, plan, feedback_input, available_tools, available_agents
            )

        # 4. 编译并执行
        compiler = PlanCompiler(self.agent_registry, self.tool_router)
        compiled_graph = compiler.compile(plan)

        ctx = RunContext(
            input=user_input,
            state=DictState(),
            deps=AgentDeps(
                tool_router=self.tool_router,
                agent_registry=self.agent_registry,
                graph_engine=self.engine,
                ui=self.ui,
            ),
        )
        result = await self.engine.run(compiled_graph, ctx)

        # 提取输出
        output = result.output
        if isinstance(output, dict):
            output = output.get("text", str(output))
        return str(output) if output else "计划执行完成。"

    @staticmethod
    def format_plan(plan: Plan) -> str:
        """格式化计划用于展示。"""
        lines = []
        for i, step in enumerate(plan.steps, 1):
            deps = f" (依赖: {', '.join(step.depends_on)})" if step.depends_on else ""
            if step.tool_name:
                lines.append(f"  {i}. [工具] {step.description} -> {step.tool_name}{deps}")
            elif step.agent_name:
                lines.append(f"  {i}. [Agent] {step.description} -> {step.agent_name}{deps}")
        return "\n".join(lines)
```

- [ ] **Step 4: Update plan __init__.py to export PlanFlow**

在 `src/plan/__init__.py` 中添加：

import 区域:
```python
from src.plan.flow import PlanFlow
```

`__all__` 列表末尾添加:
```python
    "PlanFlow",
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/plan/test_flow.py -v`
Expected: All 5 tests PASS

Run: `python -m pytest tests/plan/ -v`
Expected: All plan tests PASS (包括 test_models, test_compiler, test_planner, test_flow)

- [ ] **Step 6: Commit**

```bash
git add src/plan/flow.py src/plan/__init__.py tests/plan/test_flow.py
git commit -m "feat: extract PlanFlow to src/plan/flow.py with UI abstraction"
```

---

### Task 5: AgentApp 应用核心

**Files:**
- Create: `src/app.py`
- Create: `tests/test_app.py`

- [ ] **Step 1: Write failing test for AgentApp**

```python
# tests/test_app.py
"""Tests for AgentApp."""
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from src.app import AgentApp


def _make_mock_ui():
    ui = AsyncMock()
    ui.prompt = AsyncMock(return_value="exit")
    ui.display = AsyncMock()
    ui.confirm = AsyncMock(return_value=True)
    return ui


class TestAgentAppProcess:

    @pytest.mark.asyncio
    async def test_guardrail_blocks_dangerous_input(self):
        ui = _make_mock_ui()
        app = AgentApp(ui=ui)
        # Manually set up minimal state to test process()
        app.guardrail = Mock(check=Mock(return_value=(False, "不安全内容")))
        app.router = Mock()
        app.engine = Mock()
        app.graph = Mock()
        app.skill_manager = Mock()
        app.agent_registry = Mock()

        await app.process("rm -rf /")
        ui.display.assert_called()
        call_text = ui.display.call_args[0][0]
        assert "安全拦截" in call_text

    @pytest.mark.asyncio
    async def test_plan_command_no_request(self):
        ui = _make_mock_ui()
        app = AgentApp(ui=ui)
        app.guardrail = Mock(check=Mock(return_value=(True, "")))
        app.router = Mock()
        app.engine = Mock()
        app.graph = Mock()
        app.skill_manager = Mock()
        app.agent_registry = Mock()

        await app.process("/plan")
        ui.display.assert_called()
        call_text = ui.display.call_args[0][0]
        assert "/plan" in call_text


class TestAgentAppRun:

    @pytest.mark.asyncio
    async def test_exit_command_stops_loop(self):
        ui = _make_mock_ui()
        ui.prompt = AsyncMock(return_value="exit")
        app = AgentApp(ui=ui)
        # Mock setup components
        app.router = Mock()
        app.engine = Mock()
        app.graph = Mock()
        app.skill_manager = Mock()
        app.agent_registry = Mock()
        app.mcp_manager = Mock()
        app.guardrail = Mock()

        await app.run()
        # Should have displayed startup message and then exited
        ui.display.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.app'`

- [ ] **Step 3: Implement AgentApp**

```python
# src/app.py
"""AgentApp — 应用核心，组装所有组件并处理用户消息。"""

from __future__ import annotations

from pathlib import Path

from src.interfaces.base import UserInterface
from src.tools import (
    get_registry,
    discover_tools,
    ToolExecutor,
    ToolRouter,
    LocalToolProvider,
    sensitive_confirm_middleware,
    truncate_middleware,
    error_handler_middleware,
)
from src.mcp.provider import MCPToolProvider
from src.mcp.config import load_mcp_config
from src.mcp.manager import MCPManager
from src.skills.provider import SkillToolProvider
from src.skills import SkillManager
from src.core.guardrails import InputGuardrail
from src.agents import AgentRegistry, GraphEngine, RunContext, DictState
from src.agents.deps import AgentDeps
from src.agents.definitions import build_default_graph, build_skill_graph
from src.plan.flow import PlanFlow
from config import MCP_CONFIG_PATH, SKILLS_DIRS


class AgentApp:
    """应用核心：初始化组件、处理消息、管理生命周期。"""

    def __init__(self, ui: UserInterface):
        self.ui = ui
        self.guardrail = InputGuardrail()

    async def setup(self) -> None:
        """初始化所有组件：工具、MCP、Skills、Agent、图引擎。"""
        # 1. 发现并注册本地工具
        discover_tools("src.tools.builtin", Path("src/tools/builtin"))

        # 2. 构建本地工具执行管道
        registry = get_registry()
        executor = ToolExecutor(registry)
        middlewares = [
            error_handler_middleware(),
            sensitive_confirm_middleware(registry),
            truncate_middleware(2000),
        ]
        local_provider = LocalToolProvider(registry, executor, middlewares)

        # 3. 构建路由器
        self.router = ToolRouter()
        self.router.add_provider(local_provider)

        # 4. 初始化 MCP
        self.mcp_manager = MCPManager()
        await self.mcp_manager.connect_all(load_mcp_config(MCP_CONFIG_PATH))
        mcp_schemas = self.mcp_manager.get_tools_schemas()
        if mcp_schemas:
            self.router.add_provider(MCPToolProvider(self.mcp_manager))

        # 5. 初始化 Skills
        self.skill_manager = SkillManager(skill_dirs=SKILLS_DIRS)
        await self.skill_manager.discover()
        skill_count = len(self.skill_manager._skills)
        if skill_count:
            self.router.add_provider(SkillToolProvider(self.skill_manager))

        # 6. 构建 agent 注册表、图、引擎
        self.agent_registry = AgentRegistry()
        self.graph = build_default_graph(self.agent_registry)
        self.engine = GraphEngine(registry=self.agent_registry)

        # 7. 显示启动信息
        await self.ui.display("Agent 已启动，输入 'exit' 退出。\n")
        if mcp_schemas:
            await self.ui.display(f"已加载 {len(mcp_schemas)} 个 MCP 工具\n")
        if skill_count:
            await self.ui.display(f"已发现 {skill_count} 个 Skill\n")

    async def process(self, user_input: str) -> None:
        """处理单条用户消息：护栏 → /plan → /skill → 正常执行。"""
        # 1. 护栏检查
        passed, reason = self.guardrail.check(user_input)
        if not passed:
            await self.ui.display(f"\n[安全拦截] {reason}\n")
            return

        # 2. /plan 命令
        if user_input.strip().startswith("/plan"):
            plan_request = user_input.strip()[5:].strip()
            if not plan_request:
                await self.ui.display("\n请在 /plan 后输入你的请求，例如：/plan 查询广州天气并发邮件给同事\n")
                return
            plan_flow = PlanFlow(
                tool_router=self.router,
                agent_registry=self.agent_registry,
                engine=self.engine,
                ui=self.ui,
            )
            result = await plan_flow.run(plan_request)
            await self.ui.display(f"\n{result}\n")
            return

        # 3. Skill 斜杠命令
        skill_name = self.skill_manager.is_slash_command(user_input)
        if skill_name:
            skill_content = self.skill_manager.activate(skill_name)
            if skill_content:
                remaining = user_input[len(f"/{skill_name}"):].strip()
                actual_input = remaining or f"已激活 {skill_name} skill，请按指令执行。"
                skill_registry = AgentRegistry()
                skill_graph = build_skill_graph(skill_registry, skill_content)
                skill_engine = GraphEngine(registry=skill_registry)
                ctx = RunContext(
                    input=actual_input,
                    state=DictState(),
                    deps=AgentDeps(
                        tool_router=self.router,
                        agent_registry=skill_registry,
                        graph_engine=skill_engine,
                        ui=self.ui,
                    ),
                )
                result = await skill_engine.run(skill_graph, ctx)
                await self.ui.display(f"\n{result.output}\n")
                return

        # 4. 正常执行
        ctx = RunContext(
            input=user_input,
            state=DictState(),
            deps=AgentDeps(
                tool_router=self.router,
                agent_registry=self.agent_registry,
                graph_engine=self.engine,
                ui=self.ui,
            ),
        )
        result = await self.engine.run(self.graph, ctx)

        output = result.output
        if isinstance(output, dict):
            output = output.get("text", str(output))
        await self.ui.display(f"\n{output}\n")

    async def run(self) -> None:
        """CLI 主循环。Web 接入时直接调用 process() 而非 run()。"""
        while True:
            user_input = await self.ui.prompt("\n你: ")
            if user_input.strip().lower() in ("exit", "quit"):
                break
            await self.process(user_input)

    async def shutdown(self) -> None:
        """清理资源。"""
        await self.mcp_manager.disconnect_all()
```

- [ ] **Step 4: Run tests to verify**

Run: `python -m pytest tests/test_app.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/app.py tests/test_app.py
git commit -m "feat: add AgentApp as central application class"
```

---

### Task 6: 重写 main.py + 清理

**Files:**
- Rewrite: `main.py`
- Create: `src/memory/utils.py`
- Modify: `tests/core/test_main.py`

- [ ] **Step 1: Rewrite main.py as thin entry point**

```python
# main.py
"""Agent 入口。"""

import asyncio

from src.app import AgentApp
from src.interfaces.cli import CLIInterface


async def main():
    app = AgentApp(ui=CLIInterface())
    await app.setup()
    try:
        await app.run()
    finally:
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Move _build_collection_name to src/memory/utils.py**

```python
# src/memory/utils.py
"""Memory 工具函数。"""

import re


def build_collection_name(prefix: str, user_id: str | None) -> str:
    """构建 ChromaDB collection 名称，基于前缀和用户 ID。"""
    if not user_id:
        return prefix
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", user_id).strip("_").lower()
    if not sanitized:
        return prefix
    return f"{prefix}_{sanitized}"[:63].strip("_")
```

- [ ] **Step 3: Rewrite test_main.py**

```python
# tests/core/test_main.py
"""Tests for main.py entry point."""
import importlib
import main


class TestMainModule:

    def test_main_function_exists(self):
        assert hasattr(main, "main")
        assert callable(main.main)

    def test_main_is_coroutine_function(self):
        import asyncio
        assert asyncio.iscoroutinefunction(main.main)
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/core/test_main.py -v`
Expected: All tests PASS

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (全量回归)

- [ ] **Step 5: Commit**

```bash
git add main.py src/memory/utils.py tests/core/test_main.py
git commit -m "refactor: slim main.py to thin entry point, move helpers to modules"
```

---

### Task 7: 全量回归验证

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests PASS, no regressions

- [ ] **Step 2: Verify imports work end-to-end**

Run:
```bash
python -c "
from src.interfaces import UserInterface, CLIInterface
from src.agents import AgentDeps
from src.agents.definitions import build_default_graph, build_skill_graph
from src.plan.flow import PlanFlow
from src.app import AgentApp
from src.memory.utils import build_collection_name
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 3: Verify main.py line count**

Run: `wc -l main.py`
Expected: ~20 lines (目标 <25 行)

- [ ] **Step 4: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "fix: address any issues found during regression testing"
```
