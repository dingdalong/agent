"""Tests for refactored tools foundation modules."""
import pytest
from pydantic import BaseModel, Field

from src.tools.schemas import ToolDict
from src.tools.registry import ToolEntry, ToolRegistry
from src.tools.decorator import tool, get_registry


# === Helpers ===

class DummyModel(BaseModel):
    value: str = Field(description="test value")


def _make_entry(name: str = "dummy", sensitive: bool = False) -> ToolEntry:
    async def dummy_func(value: str) -> str:
        return value
    return ToolEntry(
        name=name,
        func=dummy_func,
        model=DummyModel,
        description="A dummy tool",
        parameters_schema=DummyModel.model_json_schema(),
        sensitive=sensitive,
        confirm_template=None,
    )


# === schemas tests ===

def test_tool_dict_type():
    td: ToolDict = {
        "type": "function",
        "function": {
            "name": "test",
            "description": "a test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    assert td["type"] == "function"
    assert td["function"]["name"] == "test"


# === registry tests ===

def test_registry_register_and_get():
    reg = ToolRegistry()
    entry = _make_entry("test_tool")
    reg.register(entry)
    assert reg.has("test_tool")
    assert reg.get("test_tool") is entry
    assert reg.get("nonexistent") is None


def test_registry_duplicate_skips():
    reg = ToolRegistry()
    entry1 = _make_entry("dup")
    entry2 = _make_entry("dup")
    reg.register(entry1)
    reg.register(entry2)
    assert reg.get("dup") is entry1
    assert len(reg.list_entries()) == 1


def test_registry_get_schemas():
    reg = ToolRegistry()
    reg.register(_make_entry("tool_a"))
    reg.register(_make_entry("tool_b"))
    schemas = reg.get_schemas()
    assert len(schemas) == 2
    names = {s["function"]["name"] for s in schemas}
    assert names == {"tool_a", "tool_b"}
    assert all(s["type"] == "function" for s in schemas)


# === decorator tests ===

def test_tool_decorator_registers():
    registry = get_registry()
    initial_count = len(registry.list_entries())

    class TestParams(BaseModel):
        x: int = Field(description="a number")

    @tool(model=TestParams, description="test decorator tool")
    async def _test_decorator_func(x: int) -> str:
        return str(x)

    assert registry.has("_test_decorator_func")
    entry = registry.get("_test_decorator_func")
    assert entry.description == "test decorator tool"
    assert entry.sensitive is False
    assert len(registry.list_entries()) == initial_count + 1


def test_tool_decorator_sensitive():
    class SensParams(BaseModel):
        target: str = Field(description="target")

    @tool(model=SensParams, description="sensitive tool", sensitive=True,
          confirm_template="操作 {target}")
    async def _test_sensitive_func(target: str) -> str:
        return target

    registry = get_registry()
    entry = registry.get("_test_sensitive_func")
    assert entry.sensitive is True
    assert entry.confirm_template == "操作 {target}"


def test_tool_decorator_custom_name():
    class NameParams(BaseModel):
        v: str = Field(description="value")

    @tool(model=NameParams, description="custom name", name="my_custom_tool")
    async def _some_internal_func(v: str) -> str:
        return v

    registry = get_registry()
    assert registry.has("my_custom_tool")
    assert not registry.has("_some_internal_func")


# === discovery tests ===

def test_discover_tools(tmp_path):
    import sys
    from src.tools.discovery import discover_tools

    pkg_dir = tmp_path / "fake_tools"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "sample.py").write_text("LOADED = True\n")

    sys.path.insert(0, str(tmp_path))
    try:
        discover_tools("fake_tools", pkg_dir)
        import fake_tools.sample
        assert fake_tools.sample.LOADED is True
    finally:
        sys.path.remove(str(tmp_path))


def test_discover_tools_skips_init(tmp_path):
    import sys
    from src.tools.discovery import discover_tools

    pkg_dir = tmp_path / "skip_test"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("INIT_LOADED = True\n")
    (pkg_dir / "real.py").write_text("REAL_LOADED = True\n")

    sys.path.insert(0, str(tmp_path))
    try:
        discover_tools("skip_test", pkg_dir)
        import skip_test.real
        assert skip_test.real.REAL_LOADED is True
    finally:
        sys.path.remove(str(tmp_path))


# === executor tests ===

@pytest.mark.asyncio
async def test_executor_validates_and_runs():
    from src.tools.executor import ToolExecutor as NewExecutor

    class AddModel(BaseModel):
        a: int = Field(description="first number")
        b: int = Field(description="second number")

    async def add_func(a: int, b: int) -> str:
        return f"result:{a + b}"

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="add", func=add_func, model=AddModel,
        description="Add two numbers", parameters_schema=AddModel.model_json_schema(),
    ))
    executor = NewExecutor(reg)
    result = await executor.execute("add", {"a": 3, "b": 4})
    assert result == "result:7"


@pytest.mark.asyncio
async def test_executor_validation_error():
    from src.tools.executor import ToolExecutor as NewExecutor

    class StrictModel(BaseModel):
        count: int = Field(description="must be int")

    async def noop(count: int) -> str:
        return "ok"

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="strict", func=noop, model=StrictModel,
        description="strict tool", parameters_schema=StrictModel.model_json_schema(),
    ))
    executor = NewExecutor(reg)
    with pytest.raises(ValueError, match="参数验证失败"):
        await executor.execute("strict", {"count": "not_a_number"})


@pytest.mark.asyncio
async def test_executor_unknown_tool():
    from src.tools.executor import ToolExecutor as NewExecutor
    executor = NewExecutor(ToolRegistry())
    with pytest.raises(ValueError, match="未注册的工具"):
        await executor.execute("nonexistent", {})


@pytest.mark.asyncio
async def test_executor_sync_function():
    from src.tools.executor import ToolExecutor as NewExecutor

    class EchoModel(BaseModel):
        msg: str = Field(description="message")

    def sync_echo(msg: str) -> str:
        return f"echo:{msg}"

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="sync_echo", func=sync_echo, model=EchoModel,
        description="sync echo", parameters_schema=EchoModel.model_json_schema(),
    ))
    executor = NewExecutor(reg)
    result = await executor.execute("sync_echo", {"msg": "hello"})
    assert result == "echo:hello"


# === middleware tests ===

from src.tools.middleware import build_pipeline, truncate_middleware, error_handler_middleware


@pytest.mark.asyncio
async def test_truncate_middleware():
    async def fake_execute(name: str, args: dict) -> str:
        return "x" * 100

    pipeline = build_pipeline(fake_execute, [truncate_middleware(max_length=50)])
    result = await pipeline("test", {})
    assert len(result) < 100
    assert "截断" in result


@pytest.mark.asyncio
async def test_truncate_middleware_short_result():
    async def fake_execute(name: str, args: dict) -> str:
        return "short"

    pipeline = build_pipeline(fake_execute, [truncate_middleware(max_length=50)])
    result = await pipeline("test", {})
    assert result == "short"


@pytest.mark.asyncio
async def test_error_handler_middleware():
    async def failing_execute(name: str, args: dict) -> str:
        raise RuntimeError("boom")

    pipeline = build_pipeline(failing_execute, [error_handler_middleware()])
    result = await pipeline("test", {})
    assert "执行出错" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_middleware_chain_order():
    async def failing_execute(name: str, args: dict) -> str:
        raise RuntimeError("inner error")

    pipeline = build_pipeline(
        failing_execute,
        [error_handler_middleware(), truncate_middleware(max_length=50)],
    )
    result = await pipeline("test", {})
    assert "执行出错" in result


# === router tests ===

from src.tools.router import ToolRouter, LocalToolProvider
from src.tools.executor import ToolExecutor as NewToolExecutor


@pytest.mark.asyncio
async def test_local_provider_can_handle():
    class M(BaseModel):
        v: str = Field(description="v")

    async def fn(v: str) -> str:
        return v

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="local_tool", func=fn, model=M,
        description="test", parameters_schema=M.model_json_schema(),
    ))
    executor = NewToolExecutor(reg)
    provider = LocalToolProvider(reg, executor, [error_handler_middleware()])
    assert provider.can_handle("local_tool") is True
    assert provider.can_handle("unknown") is False


@pytest.mark.asyncio
async def test_local_provider_execute():
    class M(BaseModel):
        v: str = Field(description="v")

    async def fn(v: str) -> str:
        return f"got:{v}"

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="echo", func=fn, model=M,
        description="echo", parameters_schema=M.model_json_schema(),
    ))
    executor = NewToolExecutor(reg)
    provider = LocalToolProvider(reg, executor, [error_handler_middleware()])
    result = await provider.execute("echo", {"v": "hello"})
    assert result == "got:hello"


@pytest.mark.asyncio
async def test_router_routes_to_provider():
    class M(BaseModel):
        v: str = Field(description="v")

    async def fn(v: str) -> str:
        return f"routed:{v}"

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="routed_tool", func=fn, model=M,
        description="test", parameters_schema=M.model_json_schema(),
    ))
    executor = NewToolExecutor(reg)
    provider = LocalToolProvider(reg, executor, [error_handler_middleware()])
    router = ToolRouter()
    router.add_provider(provider)
    result = await router.route("routed_tool", {"v": "test"})
    assert result == "routed:test"


@pytest.mark.asyncio
async def test_router_unknown_tool():
    router = ToolRouter()
    result = await router.route("nonexistent", {})
    assert "未找到" in result


def test_router_get_all_schemas():
    class M(BaseModel):
        v: str = Field(description="v")

    async def fn(v: str) -> str:
        return v

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="s1", func=fn, model=M,
        description="tool 1", parameters_schema=M.model_json_schema(),
    ))
    executor = NewToolExecutor(reg)
    provider = LocalToolProvider(reg, executor, [])
    router = ToolRouter()
    router.add_provider(provider)
    schemas = router.get_all_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "s1"


def test_router_is_sensitive():
    class M(BaseModel):
        v: str = Field(description="v")

    async def fn(v: str) -> str:
        return v

    reg = ToolRegistry()
    reg.register(ToolEntry(
        name="safe", func=fn, model=M,
        description="safe", parameters_schema=M.model_json_schema(), sensitive=False,
    ))
    reg.register(ToolEntry(
        name="danger", func=fn, model=M,
        description="danger", parameters_schema=M.model_json_schema(), sensitive=True,
    ))
    executor = NewToolExecutor(reg)
    provider = LocalToolProvider(reg, executor, [])
    router = ToolRouter()
    router.add_provider(provider)
    assert router.is_sensitive("safe") is False
    assert router.is_sensitive("danger") is True
    assert router.is_sensitive("nonexistent") is False
