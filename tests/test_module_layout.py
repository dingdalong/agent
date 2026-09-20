"""验证 Manager 与公共模块的目录边界。"""

import ast
from dataclasses import fields, is_dataclass
from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC_DIR = ROOT / "src"
MGR_DIR = SRC_DIR / "mgr"
COMMON_DIR = SRC_DIR / "common"
AGENT_DIR = SRC_DIR / "agent"
TOOLS_DIR = SRC_DIR / "tools"
LLM_DIR = SRC_DIR / "llm"
INTERFACES_DIR = SRC_DIR / "interfaces"
COMMANDS_DIR = SRC_DIR / "commands"

# 运行期 import 的允许方向：上层可依赖下层，反向即层次错位。
LAYER_FORBIDDEN_IMPORTS = {
    "common": (
        "src.tools", "src.mgr", "src.agent", "src.app", "src.interfaces", "src.commands",
    ),
    "tools": ("src.mgr", "src.agent", "src.app", "src.interfaces", "src.commands"),
    "llm": ("src.mgr", "src.agent", "src.app", "src.interfaces", "src.commands"),
    "mgr": ("src.agent", "src.app", "src.interfaces", "src.commands"),
    "interfaces": ("src.app", "src.agent", "src.mgr", "src.commands"),
}

# agent 级部件的具体实现；构造它们需要 spec、deps 与装配顺序知识。
ASSEMBLED_PARTS = {
    "CompactMgr", "PromptMgr", "ReminderMgr", "SkillMgr", "SubAgentMgr", "TaskManager",
}


def _class_method(
    path: Path, class_name: str, method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """在模块文件中定位类方法定义。

    Args:
        path: Python 源文件。
        class_name: 类名。
        method_name: 方法名。

    Returns:
        方法定义的 AST 节点。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    return item
    raise AssertionError(f"{path.name} 缺少 {class_name}.{method_name}")


def _constructed_names(tree: ast.AST) -> set[str]:
    """收集一段代码里被调用的构造函数名。

    Args:
        tree: 待扫描的 AST 节点。

    Returns:
        被调用标识符的名字集合（`Name` 或 `Attribute` 末段）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _targets_deps(node: ast.AST) -> bool:
    """判断赋值目标是否直接挂在 deps 容器上（`deps.x` / `self.deps.x`）。

    只看「直接挂在」——`deps.session.id = ...` 更新的是会话态容器的字段，
    属于被允许的运行期写入。

    Args:
        node: 赋值目标节点。

    Returns:
        目标是否是 deps 自身字段。
    """
    if not isinstance(node, ast.Attribute):
        return False
    value = node.value
    if isinstance(value, ast.Name):
        return value.id == "deps"
    if isinstance(value, ast.Attribute):
        return value.attr == "deps"
    return False


def _assigned_targets(tree: ast.AST) -> list[ast.AST]:
    """收集一段代码里所有赋值的直接目标。"""
    targets: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets.append(node.target)
    return targets


def _is_type_checking_test(node: ast.AST) -> bool:
    """判断 `if` 的条件是否是 `TYPE_CHECKING` 开关。

    Args:
        node: `if` 语句的条件节点。

    Returns:
        条件是否形如 `TYPE_CHECKING` / `typing.TYPE_CHECKING`。
    """
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


def _module_imports(path: Path) -> list[tuple[str, set[str], int, bool]]:
    """收集模块的全部 import。

    Args:
        path: Python 源文件。

    Returns:
        `(模块名, 导入的符号集, 行号, 是否位于 TYPE_CHECKING 块内)` 列表；
        `import a.b` 记模块名为 `a.b`，`from a import b` 记模块名 `a`、符号 `b`。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, set[str], int, bool]] = []

    def visit(node: ast.AST, in_type_checking: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_tc = in_type_checking or (
                isinstance(child, ast.If) and _is_type_checking_test(child.test)
            )
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append((alias.name, set(), child.lineno, child_in_tc))
            elif isinstance(child, ast.ImportFrom):
                if child.module:
                    found.append(
                        (child.module, {alias.name for alias in child.names},
                         child.lineno, child_in_tc),
                    )
            visit(child, child_in_tc)

    visit(tree, False)
    return found


def _imports_agent_layer(module: str) -> bool:
    """判断模块名是否属于 agent 层或由 agent 层反向持有的 app 层。"""
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in ("src.agent", "src.app")
    )


def test_mgr_layer_does_not_import_agent_layer_at_runtime() -> None:
    """运行期 `src/mgr/**` 不得 import `src.agent`/`src.app`。

    注解期（`TYPE_CHECKING`）只允许引用 DI 容器类型，不构成运行期依赖：
    管理器因此可以只凭端口数据与协议被构造、测试和复用。
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno} {module}"
        for path in sorted(MGR_DIR.rglob("*.py"))
        for module, _names, lineno, in_tc in _module_imports(path)
        if not in_tc and _imports_agent_layer(module)
    ]
    assert not offenders


def test_mgr_layer_names_only_the_deps_container_when_type_checking() -> None:
    """`src/mgr/**` 的注解期依赖只允许是 `AgentDeps`，不得引用 `Agent` 状态机。"""
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno} {module}.{sorted(names)}"
        for path in sorted(MGR_DIR.rglob("*.py"))
        for module, names, lineno, in_tc in _module_imports(path)
        if in_tc and _imports_agent_layer(module) and names - {"AgentDeps"}
    ]
    assert not offenders


def test_subagent_mgr_delegates_through_explicit_ports() -> None:
    """委派路径只收事实快照与执行器端口，不再回调持有父 agent 私有字段。"""
    source = MGR_DIR / "subagent_mgr.py"
    text = source.read_text(encoding="utf-8")
    assert "parent_agent" not in text, "委派入口不得回读父 agent 实例"

    delegate = _class_method(source, "SubAgentMgr", "delegate")
    annotations = {
        arg.arg: ast.unparse(arg.annotation)
        for arg in delegate.args.kwonlyargs
    }
    assert set(annotations) >= {"parent", "executor"}
    assert annotations["parent"] == "ParentAgentFacts"
    assert annotations["executor"] == "SubagentExecutor"

    from src.mgr.subagent_mgr import SubAgentMgr

    assert not hasattr(SubAgentMgr, "task_delegator"), "旧委派入口应已删除"


def test_agent_exposes_manager_ports_as_read_only_facts() -> None:
    """`Agent` 以只读属性满足 Manager 端口，事实权威仍在 spec/assembly。"""
    import inspect

    from src.agent import Agent
    from src.mgr.common.agent_port import AgentCaller, ModeOwner
    from src.mgr.common.subagent_port import SubagentExecutor

    port_names: set[str] = set()
    for protocol in (AgentCaller, ModeOwner):
        port_names |= set(protocol.__annotations__)
    for name in sorted(port_names):
        assert (
            hasattr(Agent, name) or name in Agent.__dataclass_fields__
        ), f"Agent 缺少端口事实 {name}"

    derived = {"agent_type", "is_subagent", "features", "tools", "enable_thinking"}
    for name in sorted(derived):
        descriptor = inspect.getattr_static(Agent, name)
        assert isinstance(descriptor, property), f"Agent.{name} 必须是只读属性"
        assert descriptor.fset is None, f"Agent.{name} 不得提供 setter"
        assert name not in Agent.__dataclass_fields__, "派生事实不得再存一份实例字段"

    assert Agent.spawn_subagent is not None
    assert hasattr(SubagentExecutor, "spawn_subagent")


def test_delegation_ports_are_implemented_by_real_types() -> None:
    """委派端口不是文档：三个实现方必须结构上满足声明的契约。"""
    import inspect

    from src.agent import Agent
    from src.agent.states import RunResult
    from src.mgr.common.subagent_port import (
        SubagentOutcome,
        SubagentRun,
        TaskStatusWriter,
    )
    from src.mgr.task_mgr import TaskManager

    assert set(SubagentRun.__annotations__) >= {"uuid", "history"}
    assert set(SubagentOutcome.__annotations__) >= {"final_text", "llm_error"}
    for name in set(SubagentRun.__annotations__) | {"run"}:
        assert hasattr(Agent, name) or name in Agent.__dataclass_fields__, name
    for name in SubagentOutcome.__annotations__:
        assert name in RunResult.__dataclass_fields__, name

    writer_params = inspect.signature(TaskStatusWriter.update).parameters
    task_update = inspect.signature(TaskManager.update).parameters
    for name in ("task_id", "status", "owner"):
        assert name in writer_params, name
        assert name in task_update, name


def test_agent_deps_is_frozen_and_session_state_has_its_own_container() -> None:
    """进程级依赖不可变；会话级可变状态收在 `SessionContext` 里。"""
    from src.agent.agent import AgentDeps, SessionContext

    assert is_dataclass(AgentDeps)
    assert AgentDeps.__dataclass_params__.frozen, "AgentDeps 必须是 frozen dataclass"
    assert not SessionContext.__dataclass_params__.frozen, "会话态容器需要应对会话边界更新"
    names = {item.name for item in fields(AgentDeps)}
    assert "session" in names
    assert not ({"session_id", "session_state", "session_context", "env_baseline"} & names)
    session_names = {item.name for item in fields(SessionContext)}
    assert {"id", "state", "context", "env_baseline"} <= session_names


def test_runtime_code_never_writes_deps_fields() -> None:
    """除 bootstrap 外，运行期不得给 deps 赋字段；会话态更新走 `deps.session`。"""
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name == "bootstrap.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for target in _assigned_targets(tree):
            if _targets_deps(target):
                offenders.append(f"{path.relative_to(ROOT)}:{target.lineno}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            target_obj = node.args[0]
            is_deps = _targets_deps(target_obj) or (
                isinstance(target_obj, ast.Name) and target_obj.id == "deps"
            )
            if name == "setattr" and is_deps:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders


def test_agent_post_init_only_initializes_state() -> None:
    """`Agent.__post_init__` 不构造具体 Manager — 装配属于 `AgentAssembler`。"""
    method = _class_method(AGENT_DIR / "agent.py", "Agent", "__post_init__")
    assert _constructed_names(method) & ASSEMBLED_PARTS == set()


def test_agent_module_has_no_manager_construction() -> None:
    """agent 类的定义文件不含具体 Manager 构造，装配点只有 `assembly.py`。"""
    source = AGENT_DIR / "agent.py"
    assert _constructed_names(ast.parse(source.read_text(encoding="utf-8"))) & ASSEMBLED_PARTS == set()


def test_spec_and_assembly_are_frozen_dataclasses() -> None:
    """装配输入与装配产物都不可变，避免出现第二份权威。"""
    from src.agent.assembly import AgentAssembly
    from src.agent.spec import AgentSpec

    for cls in (AgentSpec, AgentAssembly):
        assert is_dataclass(cls)
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} 必须是 frozen dataclass"
    spec_fields = {item.name for item in fields(AgentSpec)}
    assembly_fields = {item.name for item in fields(AgentAssembly)}
    assert {"agent_type", "is_subagent", "features", "tools"} <= spec_fields
    assert {"llm", "model", "compact_mgr", "prompt_mgr"} <= assembly_fields


def test_mgr_top_level_contains_only_managers_and_packages() -> None:
    unexpected = {
        path.name
        for path in MGR_DIR.iterdir()
        if path.is_file() and path.name != "__init__.py" and path.suffix == ".py" and not path.stem.endswith("_mgr")
    }
    assert not unexpected


def test_common_modules_do_not_depend_on_mgr() -> None:
    offenders = []
    for path in COMMON_DIR.glob("*.py"):
        if "src.mgr" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders


def test_moved_modules_have_single_definition_location() -> None:
    moved = (
        "data_guard", "env_baseline", "features", "frozen", "patch", "path_resolver",
        "paths", "ripgrep", "sandbox", "secure_io", "session_state",
    )
    for name in moved:
        assert not (MGR_DIR / f"{name}.py").exists()
        assert (COMMON_DIR / f"{name}.py").exists()


def _module_name(path: Path) -> str:
    """把源文件路径映射为模块名；包 `__init__.py` 归属其包名。"""
    rel = path.relative_to(SRC_DIR.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _src_module_exists(dotted: str) -> bool:
    """判断点分名是否对应一个真实模块或包（供 `from pkg import sub` 解析）。"""
    base = SRC_DIR.parent.joinpath(*dotted.split("."))
    return (base.is_file() and base.suffix == ".py") or (base / "__init__.py").is_file()


def _runtime_import_graph() -> tuple[dict[str, set[str]], set[str]]:
    """构建 `src/**` 的运行期 import 图。

    Returns:
        `(模块 -> 运行期依赖, 参与环判定的真实模块集)`。`TYPE_CHECKING` 块内的注解
        依赖不计；包 `__init__.py` 只作声明面（re-export），不作为节点参与环判定。
    """
    graph: dict[str, set[str]] = {}
    real: set[str] = set()
    for path in sorted(SRC_DIR.rglob("*.py")):
        module = _module_name(path)
        if path.name != "__init__.py":
            real.add(module)
        deps: set[str] = set()
        for target, names, _lineno, in_type_checking in _module_imports(path):
            if in_type_checking or not target.startswith("src."):
                continue
            deps.add(target)
            deps.update(
                f"{target}.{name}" for name in names if _src_module_exists(f"{target}.{name}")
            )
        graph[module] = deps
    return graph, real


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """返回有向图的强连通分量，只保留规模 ≥2 的环。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for child in sorted(graph.get(node, ())):
            if child not in index:
                visit(child)
                low[node] = min(low[node], low[child])
            elif child in on_stack:
                low[node] = min(low[node], index[child])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return found


def _runtime_imports_of_layer(layer: str) -> list[tuple[str, str, int]]:
    """收集某层全部运行期 import。

    Returns:
        `(层内文件, 目标模块, 行号)` 列表；`TYPE_CHECKING` 块内的注解依赖不计。
    """
    found: list[tuple[str, str, int]] = []
    for path in sorted((SRC_DIR / layer).rglob("*.py")):
        for module, _names, lineno, in_type_checking in _module_imports(path):
            if not in_type_checking:
                found.append((str(path.relative_to(ROOT)), module, lineno))
    return found


def test_src_runtime_imports_are_acyclic() -> None:
    """运行期依赖图不得成环——环会让 import 顺序变成隐式契约。"""
    graph, real = _runtime_import_graph()
    cycles = _cycles({module: deps & real for module, deps in graph.items() if module in real})
    assert not cycles, f"存在运行期依赖环：{cycles}"


def test_layer_imports_follow_the_declared_direction() -> None:
    """各层运行期只依赖允许方向的下层，反向依赖即层次错位。"""
    offenders = [
        f"{source}:{lineno} {module}（{layer} 不得运行期依赖该层）"
        for layer, banned in LAYER_FORBIDDEN_IMPORTS.items()
        for source, module, lineno in _runtime_imports_of_layer(layer)
        if any(module == item or module.startswith(f"{item}.") for item in banned)
    ]
    assert not offenders


def test_shared_tool_vocabulary_lives_in_common() -> None:
    """工具策略、展示结构与记忆类型是跨层词汇，落在 `src/common/`；`src/tools/` 只留注册面。"""
    for name in ("policy", "display"):
        assert not (TOOLS_DIR / f"{name}.py").exists(), f"src/tools/{name}.py 应已下沉到 src/common"
    for name in ("tool_policy", "tool_display", "memory_types"):
        assert (COMMON_DIR / f"{name}.py").is_file(), f"缺少 src/common/{name}.py"


def test_registry_and_setup_contract_are_shared_leaves() -> None:
    """命令注册表与首次配置契约各自独立成模块，避免「实现 ↔ 管理器」「UI ↔ 编排」互指。"""
    assert (COMMANDS_DIR / "registry.py").is_file()
    assert (LLM_DIR / "setup_contract.py").is_file()
