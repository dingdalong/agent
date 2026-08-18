"""首次 LLM Provider 配置的独立 Textual 向导 App。

状态机（Esc 逐级后退，Ctrl+C 任意态取消退出）::

    provider -> credentials -> verifying -> model_default -> model_fast -> exit

Esc 在 provider 态无操作；credentials 回 Provider 列表；verifying 取消验证回凭据页；
model_default 回凭据页；model_fast 回 model_default（保留其所选高亮）。

model_default 与 model_fast 共用 verify 返回的同一份模型列表与 #model-list 控件，
只切换标题与高亮下标，不重新拉取模型；进入 model_fast 时默认高亮 model_default
所选下标，直接回车即两个槽位用同一模型。

credentials 态下 URL/Key 输入框用 Up/Down 纵向移动焦点（URL Up 回 Provider
列表，Key Down 停留原地）；回列表后 Up/Down 恢复为切换高亮，Enter 重新选择。

契约见 src/app/provider_setup.py 的 _run_setup_app：关键字构造
``SetupApp(options=..., verify=...)``，``await app.run_async()`` 经
``self.exit(result_or_none)`` 返回 SetupResult 或取消时的 None。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input
from textual.widgets.option_list import Option

from src.app.provider_setup import ProviderOption, SetupResult, VerifyFunc
from src.interfaces.tui.widgets import KeyboardOptionList, SelectionStatic
from src.llm.errors import classify_llm_error

_VERIFYING_TEXT = "正在验证…"

# 两个模型槽位屏的标题，说明各槽位的实际用途。
_MODEL_TITLES = {
    "model_default": "选择默认模型（default 槽位：主 Agent 与大部分子 agent）",
    "model_fast": "选择快速模型（fast 槽位：轻量子 agent 与智能权限裁决）",
}


class SetupInput(Input):
    """向导专用输入框：Up/Down 以优先绑定转发为 Navigate 消息，供 App 在
    Provider 列表与 URL/Key 输入框之间纵向移动焦点；编辑键行为保持不变。
    """

    BINDINGS = [
        Binding("up", "navigate_up", show=False, priority=True),
        Binding("down", "navigate_down", show=False, priority=True),
    ]

    class Navigate(Message):
        """请求沿 direction 把焦点移到相邻控件。"""

        def __init__(self, source_id: str, direction: Literal["up", "down"]) -> None:
            super().__init__()
            self.source_id = source_id
            self.direction = direction

    def action_navigate_up(self) -> None:
        self.post_message(self.Navigate(self.id or "", "up"))

    def action_navigate_down(self) -> None:
        self.post_message(self.Navigate(self.id or "", "down"))


class SetupApp(App[SetupResult | None], inherit_bindings=False):
    """独立运行的首次 Provider 配置向导。

    Args:
        options: 候选 Provider，顺序即展示顺序，首项默认高亮。
        verify: 严格验证回调；成功返回非空模型列表，失败抛异常（消息经
            classify_llm_error 安全化后展示）或防御性返回空列表。
    """

    DEFAULT_CSS = """
    SetupApp {
        background: $surface;
    }
    #provider-panel, #model-panel {
        height: 1fr;
        padding: 1 2;
    }
    #setup-title, #model-title {
        height: 1;
        color: $text;
        text-style: bold;
    }
    #provider-list {
        height: 7;
        margin-top: 1;
        border: round $primary;
    }
    #model-list {
        height: 1fr;
        margin-top: 1;
        border: round $primary;
    }
    #url-label, #key-label {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    Input {
        height: 3;
    }
    #setup-error {
        height: 2;
        color: $error;
    }
    #setup-error.verifying {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "back", show=False, priority=True),
        Binding("ctrl+c", "cancel", show=False, priority=True),
    ]

    def __init__(self, *, options: list[ProviderOption], verify: VerifyFunc) -> None:
        super().__init__()
        self._options = options
        self._verify = verify
        self._state = "provider"
        self._provider_index = 0
        self._url = ""
        self._api_key: str | None = None
        self._models: list[str] = []
        self._default_index = 0
        self._default_model = ""
        self._verify_task: asyncio.Task[tuple[list[str], str]] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-panel"):
            yield SelectionStatic("LLM Provider 配置", id="setup-title", markup=False)
            yield KeyboardOptionList(
                *[Option(option.name) for option in self._options],
                id="provider-list",
                markup=False,
            )
            yield SelectionStatic("API 地址", id="url-label", markup=False)
            yield SetupInput("", id="url-input", placeholder="https://api.example.com/v1")
            yield SelectionStatic("API Key（Ollama 可留空）", id="key-label", markup=False)
            yield SetupInput("", id="key-input", placeholder="输入 API Key", password=True)
            yield SelectionStatic("", id="setup-error", markup=False)
        with Vertical(id="model-panel"):
            yield SelectionStatic(
                _MODEL_TITLES["model_default"], id="model-title", markup=False
            )
            yield KeyboardOptionList(id="model-list", markup=False)

    def on_mount(self) -> None:
        provider_list = self.query_one("#provider-list", KeyboardOptionList)
        provider_list.highlighted = 0
        provider_list.focus()
        self.query_one("#model-panel", Vertical).display = False

    def on_option_list_option_selected(
        self,
        event: KeyboardOptionList.OptionSelected,
    ) -> None:
        if event.option_list.id == "provider-list" and self._state in (
            "provider",
            "credentials",
        ):
            self._choose_provider(event.option_index)
        elif event.option_list.id == "model-list" and self._state in (
            "model_default",
            "model_fast",
        ):
            self._choose_model(event.option_index)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit_credentials()

    def _on_setup_input_navigate(self, event: SetupInput.Navigate) -> None:
        """按来源输入框与方向在 Provider 列表/URL/Key 之间移动焦点。

        Key 的 Down 无目标，保持原地；其余未覆盖组合不移动焦点。
        """
        targets = {
            ("url-input", "up"): "#provider-list",
            ("url-input", "down"): "#key-input",
            ("key-input", "up"): "#url-input",
        }
        target = targets.get((event.source_id, event.direction))
        if target is not None:
            self.query_one(target).focus()

    def _choose_provider(self, index: int) -> None:
        """进入 credentials 态：URL 预填所选 Provider 的 base_url，清空旧 key。"""
        option = self._options[index]
        self._provider_index = index
        url_input = self.query_one("#url-input", Input)
        url_input.value = option.base_url
        self.query_one("#key-input", Input).value = ""
        self._state = "credentials"
        self._set_feedback("")
        url_input.focus()

    def _submit_credentials(self) -> None:
        """校验并提交凭据；合法则进入 verifying 态并后台运行 verify。"""
        if self._state != "credentials":
            return
        option = self._options[self._provider_index]
        url = self.query_one("#url-input", Input).value.strip()
        api_key = self.query_one("#key-input", Input).value.strip()
        if not url:
            self._set_feedback("API 地址不能为空")
            return
        if option.requires_key and not api_key:
            self._set_feedback(f"{option.name} 的 API Key 不能为空")
            return
        self._url = url
        self._api_key = api_key if api_key else None
        self._state = "verifying"
        self._set_inputs_enabled(False)
        self._set_feedback(_VERIFYING_TEXT, verifying=True)
        task = asyncio.create_task(
            self._verify_async(option, self._api_key, self._url),
            name="provider-setup-verify",
        )
        self._verify_task = task
        task.add_done_callback(self._on_verify_done)

    async def _verify_async(
        self,
        option: ProviderOption,
        api_key: str | None,
        url: str,
    ) -> tuple[list[str], str]:
        """运行 verify，返回模型列表及安全化错误消息。

        Esc 后退或 Ctrl+C 退出时取消验证任务；取消/中断/退出控制流原样传播。
        任务已取消时 _on_verify_done 不触碰 UI；其余普通异常经 classify_llm_error
        安全化后随任务结果返回。
        """
        try:
            return await self._verify(option, api_key, url), ""
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            return [], classify_llm_error(exc).message

    def _on_verify_done(self, task: asyncio.Task) -> None:
        if task is not self._verify_task or task.cancelled():
            return  # Esc 后退或 Ctrl+C 退出时已取消或替换任务
        self._finish_verify(*task.result())

    def _finish_verify(self, models: list[str], error: str) -> None:
        if self._state != "verifying":
            return
        self._verify_task = None
        if not models:
            # verify 抛错或防御性返回空列表：安全展示、保留输入、恢复 credentials。
            self._state = "credentials"
            self._set_inputs_enabled(True)
            self._set_feedback(error or "未发现可用模型")
            self.query_one("#url-input", Input).focus()
            return
        self._models = models
        self.query_one("#provider-panel", Vertical).display = False
        model_list = self.query_one("#model-list", KeyboardOptionList)
        model_list.clear_options()
        for model in models:
            model_list.add_option(Option(model))
        self.query_one("#model-panel", Vertical).display = True
        # 两个槽位共用这份列表：model_fast 屏只换标题与高亮，不再重新拉取模型。
        self._enter_model_slot("model_default", 0)

    def _choose_model(self, index: int) -> None:
        """按当前槽位屏处理模型选择。

        model_default 屏记录默认槽位所选并前进到 model_fast 屏；model_fast 屏
        携两个槽位模型退出。

        Args:
            index: 模型列表中被选中项的下标。
        """
        if self._state == "model_default":
            self._default_index = index
            self._default_model = self._models[index]
            self._enter_model_slot("model_fast", index)
            return
        self._state = "exit"
        self.exit(SetupResult(
            provider=self._options[self._provider_index].name,
            base_url=self._url,
            api_key=self._api_key,
            default_model=self._default_model,
            fast_model=self._models[index],
        ))

    def _enter_model_slot(self, state: str, highlighted: int) -> None:
        """切到指定模型槽位屏：只换标题与高亮下标，复用同一份模型列表控件。

        Args:
            state: 目标状态，``"model_default"`` 或 ``"model_fast"``。
            highlighted: 进入该屏时默认高亮的模型下标。
        """
        self._state = state
        self.query_one("#model-title", SelectionStatic).update(_MODEL_TITLES[state])
        model_list = self.query_one("#model-list", KeyboardOptionList)
        model_list.highlighted = highlighted
        model_list.focus()

    def action_back(self) -> None:
        """Esc：逐级后退 model_fast → model_default → credentials → provider。"""
        if self._state == "provider":
            return
        if self._state == "model_fast":
            # 回到 default 槽位屏并恢复其所选高亮，模型列表与凭据均不重置。
            self._enter_model_slot("model_default", self._default_index)
            return
        if self._verify_task is not None:
            self._verify_task.cancel()
            self._verify_task = None
        if self._state == "model_default":
            self.query_one("#model-panel", Vertical).display = False
            self.query_one("#provider-panel", Vertical).display = True
        self._set_inputs_enabled(True)
        self._set_feedback("")
        back = self._state in ("verifying", "model_default")
        self._state = "credentials" if back else "provider"
        self.query_one("#url-input" if back else "#provider-list").focus()

    def action_cancel(self) -> None:
        """Ctrl+C：取消正在运行的验证任务并以 None 退出。"""
        if self._verify_task is not None:
            self._verify_task.cancel()
            self._verify_task = None
        self._state = "exit"
        self.exit(None)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget_id in ("#url-input", "#key-input", "#provider-list"):
            self.query_one(widget_id).disabled = not enabled

    def _set_feedback(self, text: str, *, verifying: bool = False) -> None:
        feedback = self.query_one("#setup-error", SelectionStatic)
        feedback.update(text)
        feedback.set_class(verifying, "verifying")
