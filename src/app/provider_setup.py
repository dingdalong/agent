"""首次 LLM Provider 配置的业务编排：候选构造、严格验证与安全持久化。

调用链（由 bootstrap 在 ConfigManager 构造后 await）::

    maybe_run_provider_setup(config_mgr)
      ├─ 已有显式 Provider 配置 -> 直接返回
      ├─ stdin/stdout 非 TTY -> LLMConfigurationError（手工配置指引，不读 stdin）
      └─ TTY -> _run_setup_app(options, verify) -> SetupResult | None
                ├─ None（取消）-> LLMConfigurationError（未写入配置）
                └─ SetupResult -> await asyncio.to_thread(persist_setup, ...)
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Optional

from src.llm import LLMConfigurationError, get_provider

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager

_VERIFY_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """内置 Provider 候选（顺序即内置 llm_provider mapping 顺序）。"""

    name: str
    base_url: str
    requires_key: bool


@dataclass(frozen=True, slots=True)
class SetupResult:
    """向导确认的完整 Provider 配置结果；repr 不含 api_key。"""

    provider: str
    base_url: str
    api_key: "str | None" = field(repr=False)
    default_model: str


VerifyFunc = Callable[[ProviderOption, Optional[str], str], Awaitable[list[str]]]


def build_provider_options(config_mgr: ConfigManager) -> list[ProviderOption]:
    """从合并配置构造内置 Provider 候选，保持 mapping 顺序。

    Args:
        config_mgr: 配置管理器。

    Returns:
        已注册且 base_url 非空的 Provider 候选列表；仅 ollama 的 requires_key 为 False。

    Raises:
        LLMConfigurationError: llm_provider 非 mapping、provider 名/项/base_url 非法、
            配置了未注册 provider 名，或没有任何可配置候选。
    """
    providers = config_mgr.get_config("llm_provider")
    if not isinstance(providers, Mapping):
        raise LLMConfigurationError("llm_provider 必须是 mapping")
    options: list[ProviderOption] = []
    for name, provider_cfg in providers.items():
        if not isinstance(name, str) or not name.strip():
            raise LLMConfigurationError("llm_provider 的 provider name 必须是非空 str")
        provider_key = f"llm_provider.{name}"
        if not isinstance(provider_cfg, Mapping):
            raise LLMConfigurationError(f"{provider_key} 必须是 mapping")
        base_url = provider_cfg.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise LLMConfigurationError(f"{provider_key}.base_url 必须是非空 str")
        try:
            get_provider(name)
        except ValueError as exc:
            raise LLMConfigurationError(f"{provider_key} 配置了未知 provider 名") from exc
        options.append(
            ProviderOption(name=name, base_url=base_url, requires_key=name != "ollama")
        )
    if not options:
        raise LLMConfigurationError("没有可配置的 LLM Provider 候选，请检查 llm_provider 配置")
    return options


async def verify_provider(
    option: ProviderOption,
    api_key: "str | None",
    base_url: str,
    *,
    timeout: float = _VERIFY_TIMEOUT_SECONDS,
    user_agent: str = "",
) -> list[str]:
    """严格验证 Provider 连通性：真实调用其自身 list_models，无静态回退。

    不经过 LLMMgr，不拼接任何 secret 到异常。Ollama 空 key 仅在调用时传非秘密
    占位 "ollama"，调用结果与入参 api_key 均不改写。

    Args:
        option: 候选 Provider。
        api_key: 密钥；Ollama 为空时调用仅传占位 "ollama"。
        base_url: 用户确认的 API 根地址。
        timeout: SDK 请求与外层等待的超时秒数。
        user_agent: 自定义 User-Agent。

    Returns:
        按首次出现去重、再按大小写不敏感稳定字母序排序的非空模型 ID 列表。

    Raises:
        LLMConfigurationError: 返回值不是 list、含非空 str 之外元素或为空列表。
        asyncio.CancelledError / KeyboardInterrupt / SystemExit: 原样传播。
        Exception: 底层 SDK/网络异常原样传播，由 UI 用 classify_llm_error 分类展示。
    """
    ProviderClass = get_provider(option.name)
    call_key = api_key
    if option.name == "ollama" and not call_key:
        call_key = "ollama"
    discovered = await ProviderClass.list_models(
        call_key,
        base_url=base_url,
        timeout=timeout,
        user_agent=user_agent,
    )
    return _normalize_discovered_models(discovered)


def _normalize_discovered_models(models: object) -> list[str]:
    """校验模型发现结果：必须是非空 list、元素为非空 str，去重后稳定排序。"""
    if not isinstance(models, list):
        raise LLMConfigurationError("list_models 返回值必须是 list")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, model in enumerate(models):
        if not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError(f"list_models[{index}] 必须是非空 str")
        if model not in seen:
            seen.add(model)
            normalized.append(model)
    if not normalized:
        raise LLMConfigurationError("list_models 返回了空列表")
    return sorted(normalized, key=lambda model: (model.casefold(), model))


async def _run_setup_app(
    options: list[ProviderOption],
    verify: VerifyFunc,
) -> SetupResult | None:
    """运行独立 SetupApp；返回用户确认的 SetupResult，取消时返回 None。

    SetupApp 构造契约（供 src/interfaces/tui/provider_setup.py 实现）::

        SetupApp(options: list[ProviderOption], verify: VerifyFunc)
        await app.run_async() -> SetupResult | None

    其中 verify(option, api_key, base_url) 为可等待调用，返回规范化模型列表；
    api_key 为 None 表示未输入（Ollama 场景）。任意状态 Ctrl+C/取消返回 None。

    Args:
        options: 候选 Provider 列表。
        verify: 严格验证回调。

    Returns:
        SetupResult 或取消时的 None。
    """
    from src.interfaces.tui.plain import normalize_line_input
    from src.interfaces.tui.provider_setup import SetupApp

    # Textual 启动时会把当前终端属性快照为恢复基线，先规范化避免快照到异常残留
    # 的 raw 模式（与主 TUI 启动约定一致）。
    normalize_line_input()
    return await SetupApp(options=options, verify=verify).run_async()

def _persist_failure_message(config_mgr: ConfigManager) -> str:
    """构造持久化失败的安全可操作指引（不含原始异常文本或任何 key 值）。

    Args:
        config_mgr: 配置管理器。

    Returns:
        指向全局 config.yaml / .env 与目录权限、磁盘空间的排查提示。
    """
    return (
        "写入 LLM Provider 配置失败。请检查全局 "
        f"{config_mgr.global_dir / 'config.yaml'} 中 llm 与 llm_provider 段是否为合法对象、"
        f"{config_mgr.global_dir / '.env'} 及配置目录是否可写（目录权限、磁盘空间），"
        "修正后重新运行配置向导。"
    )


async def maybe_run_provider_setup(config_mgr: ConfigManager) -> None:
    """无显式 Provider 配置时运行首次配置向导并安全持久化。

    Args:
        config_mgr: 配置管理器。

    Returns:
        None。

    Raises:
        LLMConfigurationError: 非 TTY 缺配置、向导取消、持久化校验/后置检查失败，
            或持久化期间出现预期配置/文件异常（ValueError/OSError 已转安全错误）。
        asyncio.CancelledError / KeyboardInterrupt / SystemExit: 原样传播。
    """
    if config_mgr.has_explicit_provider_config():
        return
    options = build_provider_options(config_mgr)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LLMConfigurationError(_non_tty_message(config_mgr, options))
    llm_cfg = config_mgr.get_config("llm")
    user_agent = llm_cfg.get("user_agent", "") if isinstance(llm_cfg, Mapping) else ""
    verify = partial(verify_provider, timeout=_VERIFY_TIMEOUT_SECONDS, user_agent=user_agent)
    result = await _run_setup_app(options, verify)
    if result is None:
        raise LLMConfigurationError("已取消 LLM Provider 配置向导，未写入任何配置，请重新运行")
    # 同步文件 I/O 与 reload 全部放入线程，避免阻塞预启动 Textual 事件循环。
    try:
        await asyncio.to_thread(persist_setup, config_mgr, result)
    except (ValueError, OSError) as exc:
        # 预期配置/文件异常（如全局 config.yaml 结构非法、磁盘满或目录不可写）
        # 转成安全可操作的配置错误；原始异常仅保留在因果链，不拼入用户消息。
        raise LLMConfigurationError(_persist_failure_message(config_mgr)) from exc


def persist_setup(config_mgr: ConfigManager, result: SetupResult) -> None:
    """同步持久化向导结果：先全局 llm.default，再一次性写 Provider 环境变量。

    写入顺序（避免留下会让下次跳过向导的半套凭据）::

      1. 校验 result 的 provider 是候选、URL 非空、云 provider key 非空、默认模型非空；
      2. 原子写全局 llm.default；
      3. reload 后确认有效 llm.default 等于所选模型；被可信项目覆盖时在写 env 前抛错；
      4. 一次 set_global_env 写 {PROVIDER}_API_URL，云 provider 同批写 API_KEY
         （Ollama 只写 URL）；
      5. reload 后确认有效 llm.default、base_url 与（云）api_key 与 result 一致。

    本函数不日志、不输出、不 repr key。

    Args:
        config_mgr: 配置管理器。
        result: 向导确认的配置结果。

    Raises:
        LLMConfigurationError: 校验失败、项目覆盖冲突或 reload 后置检查不一致
            （消息均不含 key）。
    """
    option = _validate_result(config_mgr, result)
    config_mgr.set_config("llm.default", result.default_model, "global")
    config_mgr.reload()
    llm_cfg = config_mgr.get_config("llm")
    effective_default = llm_cfg.get("default") if isinstance(llm_cfg, Mapping) else None
    if effective_default != result.default_model:
        raise LLMConfigurationError(
            f"全局默认模型 {result.default_model!r} 与配置生效值 {effective_default!r} 不一致"
            "（可能被项目 config.yaml 的 llm.default 覆盖）。为避免写入后跳过首次配置向导，"
            "已终止且未写入 Provider 环境变量。请调整全局/项目 config.yaml 的 llm.default 后重试。"
        )
    env_values: dict[str, str] = {f"{result.provider.upper()}_API_URL": result.base_url}
    if option.requires_key:
        env_values[f"{result.provider.upper()}_API_KEY"] = result.api_key or ""
    config_mgr.set_global_env(env_values)
    config_mgr.reload()
    _check_persisted(config_mgr, result, option)


def _validate_result(config_mgr: ConfigManager, result: SetupResult) -> ProviderOption:
    """校验 result 合法性并返回其候选 ProviderOption。

    Raises:
        LLMConfigurationError: provider 不是候选、URL 为空、云 provider key 为空或
            默认模型为空（消息不含 key 值）。
    """
    options = build_provider_options(config_mgr)
    option = next(
        (candidate for candidate in options if candidate.name == result.provider),
        None,
    )
    if option is None:
        raise LLMConfigurationError(f"provider {result.provider!r} 不是可配置的候选 Provider")
    if not isinstance(result.base_url, str) or not result.base_url.strip():
        raise LLMConfigurationError("base_url 必须是非空 str")
    if option.requires_key and (
        not isinstance(result.api_key, str) or not result.api_key.strip()
    ):
        raise LLMConfigurationError(f"{result.provider} 的 API key 不能为空")
    if not isinstance(result.default_model, str) or not result.default_model.strip():
        raise LLMConfigurationError("default_model 必须是非空 str")
    return option


def _check_persisted(
    config_mgr: ConfigManager,
    result: SetupResult,
    option: ProviderOption,
) -> None:
    """reload 后置检查：有效 llm.default、base_url 与（云）api_key 必须与 result 一致。

    Raises:
        LLMConfigurationError: 任一后置检查不一致（消息不含 key 值）。
    """
    providers = config_mgr.get_config("llm_provider")
    if not isinstance(providers, Mapping):
        raise LLMConfigurationError("reload 后无法读取 llm_provider 配置")
    provider_cfg = providers.get(result.provider)
    if not isinstance(provider_cfg, Mapping):
        raise LLMConfigurationError(f"reload 后无法读取 provider {result.provider!r} 的配置")
    llm_cfg = config_mgr.get_config("llm")
    if not isinstance(llm_cfg, Mapping) or llm_cfg.get("default") != result.default_model:
        raise LLMConfigurationError("reload 后 llm.default 与所选默认模型不一致，请检查全局配置")
    if provider_cfg.get("base_url") != result.base_url:
        raise LLMConfigurationError(
            f"reload 后 {result.provider!r} 的 base_url 与所选值不一致，"
            "全局 .env 写入可能未生效，请检查后重试"
        )
    if option.requires_key and provider_cfg.get("api_key") != result.api_key:
        raise LLMConfigurationError(
            f"reload 后 {result.provider!r} 的 API key 与所选值不一致，"
            "全局 .env 写入可能未生效，请检查后重试"
        )


def _non_tty_message(config_mgr: ConfigManager, options: list[ProviderOption]) -> str:
    """构造非 TTY 手工配置指引（含实际路径与变量命名，不含任何秘密）。"""
    candidates = "、".join(option.name for option in options)
    return (
        "未检测到 LLM Provider 配置，且当前不是交互式终端（stdin/stdout 非 TTY），"
        "无法启动配置向导。请手工配置后重新运行本程序：\n"
        f"  1. 在 {config_mgr.global_dir / '.env'} 中写入 {{NAME}}_API_URL 与 {{NAME}}_API_KEY"
        "（NAME 为 provider 名，如 DEEPSEEK；Ollama 只需 OLLAMA_API_URL）；\n"
        f"  2. 在 {config_mgr.global_dir / 'config.yaml'} 中设置 llm.default 为可用的默认模型；\n"
        "  3. 重新运行本程序。\n"
        f"内置候选 Provider：{candidates}"
    )
