"""SetupApp 独立配置向导 TUI 的功能集中测试。

headless 运行（app.run_test），verify 全部注入 stub，不访问网络。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import Vertical
from textual.widgets import Input

from src.app.provider_setup import ProviderOption
from src.interfaces.tui.provider_setup import SetupApp
from src.interfaces.tui.widgets import KeyboardOptionList, SelectionStatic

_DEEPSEEK = ProviderOption(
    name="deepseek",
    base_url="https://api.deepseek.test/v1",
    requires_key=True,
)
_OLLAMA = ProviderOption(
    name="ollama",
    base_url="http://127.0.0.1:8001/v1",
    requires_key=False,
)


class StubVerify:
    """可控 verify 桩：记录调用、可阻塞、可抛错、可返回空列表。"""

    def __init__(self, models=None, error=None) -> None:
        self.models = (
            ["deepseek-v4-flash", "deepseek-v4-pro"] if models is None else list(models)
        )
        self.error = error
        self.calls: list[tuple[ProviderOption, str | None, str]] = []
        self.cancelled = asyncio.Event()
        self._gate: asyncio.Event | None = None

    async def __call__(self, option, api_key, base_url) -> list[str]:
        self.calls.append((option, api_key, base_url))
        if self._gate is not None:
            try:
                await self._gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.error is not None:
            raise self.error
        return list(self.models)

    def gate(self) -> asyncio.Event:
        """返回阻塞事件；release() 后 verify 才继续。"""
        self._gate = asyncio.Event()
        return self._gate

    def release(self) -> None:
        if self._gate is not None:
            self._gate.set()


async def _choose_provider(app, pilot, index: int = 0) -> None:
    """高亮第 index 个 Provider 并回车选中。"""
    app.query_one("#provider-list", KeyboardOptionList).highlighted = index
    await pilot.press("enter")


async def _wait_until(predicate, pilot) -> None:
    for _ in range(500):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition not met in time")


def _feedback(app) -> str:
    return str(app.query_one("#setup-error", SelectionStatic).content)


# ---------- provider 与凭据 ----------


def test_provider_order_default_highlight_and_url_prefill() -> None:
    """候选顺序即展示顺序、默认高亮第一项；选择后 URL 预填并聚焦输入框。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            provider_list = app.query_one("#provider-list", KeyboardOptionList)
            assert provider_list.option_count == 2
            assert provider_list.get_option_at_index(0).prompt == "deepseek"
            assert provider_list.get_option_at_index(1).prompt == "ollama"
            assert provider_list.highlighted == 0
            assert provider_list.has_focus
            await pilot.press("enter")
            url_input = app.query_one("#url-input", Input)
            assert url_input.value == _DEEPSEEK.base_url
            assert url_input.has_focus
            assert app.query_one("#key-input", Input).value == ""

    asyncio.run(scenario())


def test_key_input_uses_password_masking() -> None:
    """key 输入框启用 password 掩码，值本身不脱敏。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            key_input = app.query_one("#key-input", Input)
            assert key_input.password is True
            key_input.value = "sk-test-123"
            assert key_input.value == "sk-test-123"

    asyncio.run(scenario())


def test_cloud_empty_key_blocks_submit_and_keeps_credentials() -> None:
    """云 Provider key 为空：显示错误、不调用 verify、停留在 credentials。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            await pilot.press("enter")  # url 输入框聚焦，key 为空
            assert "API Key 不能为空" in _feedback(app)
            assert stub.calls == []
            url_input = app.query_one("#url-input", Input)
            assert url_input.has_focus
            assert url_input.disabled is False

    asyncio.run(scenario())


def test_ollama_empty_key_submits_none_and_reaches_model() -> None:
    """Ollama 空 key 合法：verify 收到 None，进入模型选择。"""

    async def scenario() -> None:
        stub = StubVerify(models=["qwen3.6"])
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 1)
            assert app.query_one("#url-input", Input).value == _OLLAMA.base_url
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert stub.calls == [(_OLLAMA, None, _OLLAMA.base_url)]
            assert app.query_one("#model-list", KeyboardOptionList).option_count == 1

    asyncio.run(scenario())

# ---------- 键盘纵向导航 ----------


def test_arrow_navigation_between_url_key_and_provider_list() -> None:
    """URL Down → key；key Up → URL；URL Up → Provider 列表。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            key_input = app.query_one("#key-input", Input)
            assert url_input.has_focus
            await pilot.press("down")
            assert key_input.has_focus
            await pilot.press("up")
            assert url_input.has_focus
            await pilot.press("up")
            assert app.query_one("#provider-list", KeyboardOptionList).has_focus

    asyncio.run(scenario())


def test_back_to_provider_list_keeps_arrows_and_enter_reselects() -> None:
    """返回 Provider 列表后方向键导航保持；回车重新选择并清空 key、清除错误。"""

    async def scenario() -> None:
        openai = ProviderOption(
            name="openai",
            base_url="https://api.openai.test/v1",
            requires_key=True,
        )
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA, openai], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            url_input.value = ""
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")  # 触发“API 地址不能为空”
            assert "API 地址不能为空" in _feedback(app)
            await pilot.press("up")
            provider_list = app.query_one("#provider-list", KeyboardOptionList)
            assert provider_list.has_focus
            assert provider_list.highlighted == 0
            await pilot.press("down")
            assert provider_list.highlighted == 1
            await pilot.press("down")
            assert provider_list.highlighted == 2  # 连续 Down 由列表自身处理
            assert provider_list.has_focus
            await pilot.press("up")
            assert provider_list.highlighted == 1
            await pilot.press("enter")
            assert url_input.value == _OLLAMA.base_url
            assert app.query_one("#key-input", Input).value == ""
            assert _feedback(app) == ""
            assert url_input.has_focus

    asyncio.run(scenario())


def test_key_down_stays_in_place_and_does_not_submit() -> None:
    """Key 上 Down 停留原地：不循环、不触发 verify。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            key_input = app.query_one("#key-input", Input)
            key_input.value = "sk-test-123"
            await pilot.press("down")
            await pilot.press("down")
            assert key_input.has_focus
            assert stub.calls == []
            assert _feedback(app) == ""

    asyncio.run(scenario())


def test_left_right_keys_move_cursor_without_changing_focus() -> None:
    """URL/key 中左右键仍移动光标，不改变焦点。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            url_input.value = "abc"
            url_input.cursor_position = 3
            await pilot.press("left")
            assert url_input.has_focus
            assert url_input.cursor_position == 2
            await pilot.press("right")
            assert url_input.cursor_position == 3
            await pilot.press("down")
            key_input = app.query_one("#key-input", Input)
            assert key_input.has_focus
            key_input.value = "xyz"
            key_input.cursor_position = 3
            await pilot.press("left")
            assert key_input.has_focus
            assert key_input.cursor_position == 2

    asyncio.run(scenario())


# ---------- 成功与失败 ----------


def test_success_returns_result_and_trims_inputs() -> None:
    """完整成功流：trim 后透传 verify，模型列表有序，选中后返回 SetupResult。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#url-input", Input).value = "  https://custom.test/v1  "
            app.query_one("#key-input", Input).value = "  sk-test-123  "
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            model_list = app.query_one("#model-list", KeyboardOptionList)
            assert model_list.highlighted == 0
            assert model_list.has_focus
            assert [
                model_list.get_option_at_index(i).prompt
                for i in range(model_list.option_count)
            ] == ["deepseek-v4-flash", "deepseek-v4-pro"]
            assert app.query_one("#provider-panel", Vertical).display is False
            await pilot.press("enter")
        assert stub.calls == [(_DEEPSEEK, "sk-test-123", "https://custom.test/v1")]
        assert app._return_value is not None
        assert app._return_value.provider == "deepseek"
        assert app._return_value.base_url == "https://custom.test/v1"
        assert app._return_value.default_model == "deepseek-v4-flash"
        assert getattr(app._return_value, "api" + "_key") == stub.calls[0][1]

    asyncio.run(scenario())


def test_verify_error_sanitized_keeps_inputs_and_retry_succeeds() -> None:
    """失败消息脱敏、输入保留；修复后重试成功。"""

    async def scenario() -> None:
        stub = StubVerify(error=RuntimeError("boom sk-test-secret"))
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#url-input", Input).value = "https://custom.test/v1"
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(
                lambda: bool(_feedback(app)) and "正在验证" not in _feedback(app), pilot
            )
            message = _feedback(app)
            assert message
            assert "sk-test-secret" not in message
            assert "sk-test-123" not in message
            assert app.query_one("#url-input", Input).value == "https://custom.test/v1"
            assert app.query_one("#key-input", Input).value == "sk-test-123"
            assert app.query_one("#key-input", Input).disabled is False
            stub.error = None
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            await pilot.press("enter")
        assert app._return_value is not None
        assert app._return_value.provider == "deepseek"
        assert app._return_value.default_model == "deepseek-v4-flash"
        assert len(stub.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_verify_async_propagates_control_flow(exc) -> None:
    """KeyboardInterrupt/SystemExit 不进入 UI 脱敏流程，原样抛出。"""

    async def scenario() -> None:
        stub = StubVerify(error=exc)
        app = SetupApp(options=[_DEEPSEEK], verify=stub)
        with pytest.raises(type(exc)):
            await app._verify_async(_DEEPSEEK, "sk-test-123", _DEEPSEEK.base_url)

    asyncio.run(scenario())


def test_empty_models_are_safe_error_and_stay_in_credentials() -> None:
    """verify 防御性返回空列表：显示安全错误、不进模型、保留输入。"""

    async def scenario() -> None:
        stub = StubVerify(models=[])
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(
                lambda: bool(_feedback(app)) and "正在验证" not in _feedback(app), pilot
            )
            assert "未发现可用模型" in _feedback(app)
            assert app.query_one("#model-panel", Vertical).display is False
            assert app.query_one("#provider-panel", Vertical).display is True
            assert app.query_one("#key-input", Input).disabled is False
            assert app.query_one("#key-input", Input).value == "sk-test-123"
            assert stub.calls == [(_DEEPSEEK, "sk-test-123", _DEEPSEEK.base_url)]

    asyncio.run(scenario())


def test_double_enter_submits_once() -> None:
    """verifying 态防重复提交：连续 Enter 只调用一次 verify。"""

    async def scenario() -> None:
        stub = StubVerify()
        stub.gate()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(lambda: bool(stub.calls), pilot)
            await pilot.press("enter")  # 第二次提交应被忽略
            await pilot.pause(0.05)
            assert len(stub.calls) == 1
            stub.release()
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert len(stub.calls) == 1

    asyncio.run(scenario())


# ---------- Esc 后退与快捷键 ----------


@pytest.mark.parametrize("state", ["provider", "credentials", "verifying", "model"])
def test_ctrl_c_exits_none_and_cancels_verify_from_every_state(state: str) -> None:
    """Ctrl+C 在任意态均返回 None；verifying 态还取消后台验证任务。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            if state in ("credentials", "verifying", "model"):
                await _choose_provider(app, pilot, 0)
            if state in ("verifying", "model"):
                stub.gate()
                app.query_one("#key-input", Input).value = "sk-test-123"
                await pilot.press("enter")
            if state == "verifying":
                await _wait_until(lambda: bool(stub.calls), pilot)
            if state == "model":
                stub.release()
                await _wait_until(
                    lambda: app.query_one("#model-panel", Vertical).display, pilot
                )
            await pilot.press("ctrl+c")
            if state == "verifying":
                await _wait_until(lambda: stub.cancelled.is_set(), pilot)
                assert stub.cancelled.is_set()
            await _wait_until(lambda: app._state == "exit", pilot)
        assert app._return_value is None

    asyncio.run(scenario())


def test_escape_in_provider_list_is_noop() -> None:
    """provider 态 Esc 不退出，列表仍可正常选择 Provider。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            provider_list = app.query_one("#provider-list", KeyboardOptionList)
            provider_panel = app.query_one("#provider-panel", Vertical)
            model_panel = app.query_one("#model-panel", Vertical)
            assert provider_list.has_focus
            assert provider_panel.display is True
            assert model_panel.display is False

            await pilot.press("escape")

            assert app._state == "provider"
            assert provider_list.has_focus
            assert provider_panel.display is True
            assert model_panel.display is False

            await pilot.press("enter")
            url_input = app.query_one("#url-input", Input)
            assert app._state == "credentials"
            assert url_input.value == _DEEPSEEK.base_url
            assert url_input.has_focus

    asyncio.run(scenario())


def test_escape_from_credentials_returns_to_provider_list() -> None:
    """credentials 态 Esc 返回 Provider 列表并允许重新选择。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            url_input.value = ""
            await pilot.press("enter")
            assert _feedback(app) == "API 地址不能为空"

            await pilot.press("escape")

            provider_list = app.query_one("#provider-list", KeyboardOptionList)
            assert app._state == "provider"
            assert provider_list.has_focus
            assert _feedback(app) == ""

            await pilot.press("enter")
            assert app._state == "credentials"
            assert url_input.value == _DEEPSEEK.base_url
            assert url_input.has_focus

    asyncio.run(scenario())


def test_escape_from_verifying_cancels_and_returns_to_credentials() -> None:
    """verifying 态 Esc 取消验证、恢复凭据输入，并允许重新提交。"""

    async def scenario() -> None:
        stub = StubVerify()
        stub.gate()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            key_input = app.query_one("#key-input", Input)
            url_input.value = "https://custom.test/v1"
            key_input.value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(lambda: bool(stub.calls), pilot)

            await pilot.press("escape")
            await _wait_until(lambda: stub.cancelled.is_set(), pilot)

            assert stub.cancelled.is_set()
            assert app._state == "credentials"
            assert url_input.disabled is False
            assert key_input.disabled is False
            assert url_input.value == "https://custom.test/v1"
            assert key_input.value == "sk-test-123"
            assert "正在验证" not in _feedback(app)
            assert url_input.has_focus

            stub.release()
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert app._state == "model"
            assert app.query_one("#model-panel", Vertical).display is True

    asyncio.run(scenario())


def test_stale_verify_result_is_ignored_after_resubmit() -> None:
    """旧验证吞掉取消并返回结果时，不得覆盖重新提交的新验证任务。"""

    async def scenario() -> None:
        calls: list[tuple[ProviderOption, str | None, str]] = []
        stale_cancelled = asyncio.Event()
        stale_release = asyncio.Event()
        fresh_release = asyncio.Event()

        async def verify(option, api_key, base_url) -> list[str]:
            calls.append((option, api_key, base_url))
            if len(calls) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    stale_cancelled.set()
                    await stale_release.wait()
                return ["stale-model"]
            await fresh_release.wait()
            return ["fresh-model"]

        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=verify)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            key_input = app.query_one("#key-input", Input)
            key_input.value = "sk-old"
            await pilot.press("enter")
            await _wait_until(lambda: len(calls) == 1, pilot)
            stale_task = app._verify_task
            assert stale_task is not None

            await pilot.press("escape")
            await _wait_until(stale_cancelled.is_set, pilot)
            url_input.value = "https://fresh.test/v1"
            key_input.value = "sk-fresh"
            await pilot.press("enter")
            await _wait_until(lambda: len(calls) == 2, pilot)
            fresh_task = app._verify_task
            assert fresh_task is not None
            assert fresh_task is not stale_task

            stale_release.set()
            await _wait_until(stale_task.done, pilot)
            await pilot.pause()

            model_list = app.query_one("#model-list", KeyboardOptionList)
            assert app._state == "verifying"
            assert app._verify_task is fresh_task
            assert app.query_one("#model-panel", Vertical).display is False
            assert [
                model_list.get_option_at_index(index).prompt
                for index in range(model_list.option_count)
            ] == []

            fresh_release.set()
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert app._state == "model"
            assert [
                model_list.get_option_at_index(index).prompt
                for index in range(model_list.option_count)
            ] == ["fresh-model"]

    asyncio.run(scenario())


def test_stale_verify_error_is_ignored_after_resubmit() -> None:
    """旧验证吞掉取消并晚抛错时，不得污染新验证的空模型反馈。"""

    async def scenario() -> None:
        calls: list[tuple[ProviderOption, str | None, str]] = []
        stale_cancelled = asyncio.Event()
        stale_release = asyncio.Event()
        fresh_release = asyncio.Event()

        async def verify(option, api_key, base_url) -> list[str]:
            calls.append((option, api_key, base_url))
            if len(calls) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    stale_cancelled.set()
                    await stale_release.wait()
                raise TimeoutError("stale boom")
            await fresh_release.wait()
            return []

        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=verify)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(lambda: len(calls) == 1, pilot)
            stale_task = app._verify_task
            assert stale_task is not None

            await pilot.press("escape")
            await _wait_until(stale_cancelled.is_set, pilot)
            await pilot.press("enter")
            await _wait_until(lambda: len(calls) == 2, pilot)
            fresh_task = app._verify_task
            assert fresh_task is not None
            assert fresh_task is not stale_task

            stale_release.set()
            await _wait_until(stale_task.done, pilot)
            await pilot.pause()
            assert app._state == "verifying"
            assert app._verify_task is fresh_task

            fresh_release.set()
            await _wait_until(lambda: app._state == "credentials", pilot)
            assert _feedback(app) == "未发现可用模型"
            assert "stale" not in _feedback(app)

    asyncio.run(scenario())


def test_escape_from_model_returns_to_credentials() -> None:
    """model 态 Esc 恢复凭据页，并允许再次验证进入模型页。"""

    async def scenario() -> None:
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=StubVerify())
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            url_input = app.query_one("#url-input", Input)
            key_input = app.query_one("#key-input", Input)
            key_input.value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )

            await pilot.press("escape")

            assert app._state == "credentials"
            assert app.query_one("#model-panel", Vertical).display is False
            assert app.query_one("#provider-panel", Vertical).display is True
            assert url_input.disabled is False
            assert key_input.disabled is False
            assert url_input.has_focus

            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert app._state == "model"
            assert app.query_one("#model-panel", Vertical).display is True

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["provider", "credentials", "verifying", "model"])
def test_ctrl_q_does_not_exit(state: str) -> None:
    """Ctrl+Q 在任意态均无效果，界面仍可操作，Ctrl+C 仍可取消。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            if state in ("credentials", "verifying", "model"):
                await _choose_provider(app, pilot, 0)
            if state in ("verifying", "model"):
                stub.gate()
                app.query_one("#key-input", Input).value = "sk-test-123"
                await pilot.press("enter")
                await _wait_until(lambda: bool(stub.calls), pilot)
            if state == "model":
                stub.release()
                await _wait_until(
                    lambda: app.query_one("#model-panel", Vertical).display, pilot
                )

            verify_task = app._verify_task
            focused = app.focused
            await pilot.press("ctrl+q")
            await pilot.pause()

            assert app._state == state
            assert app.focused is focused
            assert app.query_one("#provider-panel", Vertical).display is (state != "model")
            assert app.query_one("#model-panel", Vertical).display is (state == "model")
            if state == "verifying":
                assert verify_task is not None
                assert app._verify_task is verify_task
                assert verify_task.done() is False
                assert stub.cancelled.is_set() is False

            if state == "provider":
                await pilot.press("enter")
                assert app._state == "credentials"
                assert app.query_one("#url-input", Input).has_focus
            elif state == "credentials":
                app.query_one("#key-input", Input).value = "sk-test-123"
                await pilot.press("enter")
                await _wait_until(
                    lambda: app.query_one("#model-panel", Vertical).display, pilot
                )
            elif state == "verifying":
                stub.release()
                await _wait_until(
                    lambda: app.query_one("#model-panel", Vertical).display, pilot
                )
                assert stub.cancelled.is_set() is False
            else:
                model_list = app.query_one("#model-list", KeyboardOptionList)
                assert model_list.highlighted == 0
                await pilot.press("down")
                assert model_list.highlighted == 1

            await pilot.press("ctrl+c")
            await _wait_until(lambda: app._state == "exit", pilot)
        assert app._return_value is None

    asyncio.run(scenario())


# ---------- 布局与命名 ----------


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
def test_layout_no_overlap_at_common_sizes(size: tuple[int, int]) -> None:
    """80x24 与 100x30 下关键控件 region 不重叠且不越界。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            ids = [
                "#setup-title",
                "#provider-list",
                "#url-label",
                "#url-input",
                "#key-label",
                "#key-input",
                "#setup-error",
            ]
            regions = [app.query_one(widget_id).region for widget_id in ids]
            for index in range(len(regions) - 1):
                assert regions[index].bottom <= regions[index + 1].y, (
                    size,
                    ids[index],
                    regions[index],
                    regions[index + 1],
                )
            assert regions[-1].bottom <= app.screen.size.height
            await _choose_provider(app, pilot, 0)
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            await pilot.pause()
            title = app.query_one("#model-title")
            model_list = app.query_one("#model-list", KeyboardOptionList)
            assert title.region.bottom <= model_list.region.y
            assert model_list.region.bottom <= app.screen.size.height

    asyncio.run(scenario())


def test_source_has_no_legacy_names() -> None:
    """实现源码不得出现被禁止的旧命名。"""
    source = (
        Path(__file__).resolve().parents[2]
        / "src/interfaces/tui/provider_setup.py"
    ).read_text()
    assert "ProviderSetupApp" not in source
    assert "ProviderSetupResult" not in source

# ---------- URL 空值校验 ----------


@pytest.mark.parametrize("blank_url", ["", "   "])
def test_cloud_blank_url_blocks_submit_and_keeps_credentials(blank_url: str) -> None:
    """云 Provider URL 为空/空白：显示“API 地址不能为空”、不调用 verify、停留 credentials。"""

    async def scenario() -> None:
        stub = StubVerify()
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 0)
            app.query_one("#url-input", Input).value = blank_url
            app.query_one("#key-input", Input).value = "sk-test-123"
            await pilot.press("enter")
            assert _feedback(app) == "API 地址不能为空"
            assert stub.calls == []
            assert app.query_one("#provider-panel", Vertical).display is True
            assert app.query_one("#model-panel", Vertical).display is False
            url_input = app.query_one("#url-input", Input)
            assert url_input.has_focus
            assert url_input.disabled is False
            assert app.query_one("#key-input", Input).disabled is False

    asyncio.run(scenario())


# ---------- Ollama 完整成功结果 ----------


def test_ollama_success_result_none_key_and_repr_hides_key() -> None:
    """Ollama 完整成功：空 key 提交、返回模型、选中退出；SetupResult.api_key 为 None
    且 repr 不含 api_key 字段。"""

    async def scenario() -> None:
        stub = StubVerify(models=["qwen3.6", "qwen3.5"])
        app = SetupApp(options=[_DEEPSEEK, _OLLAMA], verify=stub)
        async with app.run_test(size=(80, 24)) as pilot:
            await _choose_provider(app, pilot, 1)
            await pilot.press("enter")
            await _wait_until(
                lambda: app.query_one("#model-panel", Vertical).display, pilot
            )
            assert stub.calls == [(_OLLAMA, None, _OLLAMA.base_url)]
            assert app.query_one("#model-list", KeyboardOptionList).option_count == 2
            await pilot.press("enter")
        result = app._return_value
        assert result is not None
        assert result.provider == "ollama"
        assert result.base_url == _OLLAMA.base_url
        assert result.api_key is None
        assert result.default_model == "qwen3.6"
        assert "api_key" not in repr(result)
        assert "qwen3.6" in repr(result)

    asyncio.run(scenario())
