"""Guardrail + GuardrailResult 测试。"""
import pytest
from pydantic import BaseModel


@pytest.fixture
def make_context():
    from src.agents.context import RunContext, DictState, EmptyDeps

    def _make(input_text="test"):
        return RunContext(input=input_text, state=DictState(), deps=EmptyDeps())

    return _make


@pytest.mark.asyncio
async def test_guardrail_passes(make_context):
    from src.agents.guardrails import Guardrail, GuardrailResult

    async def always_pass(ctx, text):
        return GuardrailResult(passed=True)

    guard = Guardrail(name="pass_guard", check=always_pass)
    result = await guard.check(make_context(), "hello")
    assert result.passed is True


@pytest.mark.asyncio
async def test_guardrail_blocks(make_context):
    from src.agents.guardrails import Guardrail, GuardrailResult

    async def block_bad(ctx, text):
        if "bad" in text:
            return GuardrailResult(passed=False, message="Contains bad content", action="block")
        return GuardrailResult(passed=True)

    guard = Guardrail(name="bad_guard", check=block_bad)
    result = await guard.check(make_context(), "this is bad")
    assert result.passed is False
    assert result.action == "block"
    assert "bad" in result.message


@pytest.mark.asyncio
async def test_guardrail_warn(make_context):
    from src.agents.guardrails import Guardrail, GuardrailResult

    async def warn_check(ctx, text):
        return GuardrailResult(passed=False, message="warning", action="warn")

    guard = Guardrail(name="warn_guard", check=warn_check)
    result = await guard.check(make_context(), "something")
    assert result.passed is False
    assert result.action == "warn"


@pytest.mark.asyncio
async def test_run_guardrails_all_pass(make_context):
    from src.agents.guardrails import Guardrail, GuardrailResult, run_guardrails

    async def pass_check(ctx, text):
        return GuardrailResult(passed=True)

    guards = [
        Guardrail(name="g1", check=pass_check),
        Guardrail(name="g2", check=pass_check),
    ]
    result = await run_guardrails(guards, make_context(), "hello")
    assert result is None  # None means all passed


@pytest.mark.asyncio
async def test_run_guardrails_first_block_stops(make_context):
    from src.agents.guardrails import Guardrail, GuardrailResult, run_guardrails

    async def block_check(ctx, text):
        return GuardrailResult(passed=False, message="blocked", action="block")

    async def pass_check(ctx, text):
        return GuardrailResult(passed=True)

    guards = [
        Guardrail(name="blocker", check=block_check),
        Guardrail(name="passer", check=pass_check),
    ]
    result = await run_guardrails(guards, make_context(), "hello")
    assert result is not None
    assert result.passed is False
    assert result.action == "block"
