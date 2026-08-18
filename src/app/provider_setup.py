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
from src.mgr.role_mgr import (
    active_role_name,
    discover_roles,
    format_role_config_key,
    resolve_role_name,
    role_model_yaml_example,
)

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
    """向导确认的完整 Provider 配置结果；repr 不含 api_key。

    default_model 写入激活角色的 default 槽位，fast_model 写入 fast 槽位；
    两者可以是同一个模型。
    """

    provider: str
    base_url: str
    api_key: str | None = field(repr=False)
    default_model: str
    fast_model: str


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


def _setup_role_name(config_mgr: ConfigManager) -> str:
    """解析 setup 与随后 RoleMgr 共用的有效角色名。

    Args:
        config_mgr: 配置管理器，提供角色配置、目录与项目信任状态。

    Returns:
        已发现的配置角色名；配置角色不存在时返回 DEFAULT_ROLE。
    """
    roles = discover_roles(
        config_mgr.workdir,
        config_mgr.global_dir,
        config_mgr.project_trusted,
    )
    return resolve_role_name(active_role_name(config_mgr), roles)


def _persist_failure_message(config_mgr: ConfigManager) -> str:
    """构造持久化失败的安全可操作指引（不含原始异常文本或任何 key 值）。

    Args:
        config_mgr: 配置管理器。

    Returns:
        指向全局 config.yaml / .env 与目录权限、磁盘空间的排查提示。
    """
    return (
        "写入 LLM Provider 配置失败。请检查 role 与 llm_provider 段为合法对象，"
        "并检查配置目录可写（目录权限、磁盘空间）；"
        f"配置：{config_mgr.global_dir / 'config.yaml'}；"
        f"凭据：{config_mgr.global_dir / '.env'}。修正后重新运行配置向导。"
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
    """同步持久化向导结果：先全局角色模型双槽位，再一次性写 Provider 环境变量。

    写入顺序（避免留下会让下次跳过向导的半套凭据）::

      1. 校验 result 的 provider 是候选、URL 非空、云 provider key 非空、两个槽位模型非空；
      2. 原子写全局 role.<激活角色>.model —— 整体写父键 mapping，一并抹平该层残留的
         旧标量格式（点路径会因中间节点已是标量而被 ConfigManager 拒绝）；
      3. reload 后确认两个有效槽位都等于所选模型；被更高优先级层覆盖时在写 env 前抛错；
      4. 一次 set_global_env 写 {PROVIDER}_API_URL，云 provider 同批写 API_KEY
         （Ollama 只写 URL）；
      5. reload 后确认两个有效槽位、base_url 与（云）api_key 与 result 一致。

    本函数不日志、不输出、不 repr key。

    Args:
        config_mgr: 配置管理器。
        result: 向导确认的配置结果。

    Raises:
        LLMConfigurationError: 校验失败、更高优先级层覆盖冲突或 reload 后置检查不一致
            （消息均不含 key）。
    """
    option = _validate_result(config_mgr, result)
    role_name = _setup_role_name(config_mgr)
    model_key = format_role_config_key(role_name, "model")
    expected = _expected_slots(result)
    config_mgr.set_config_parts(
        ("role", role_name, "model"),
        dict(expected),
        "global",
    )
    config_mgr.reload()
    effective = _effective_slots(config_mgr, role_name)
    if effective != expected:
        raise LLMConfigurationError(
            f"角色模型槽位 {model_key}.default / {model_key}.fast 的所选值 "
            f"{result.default_model!r} / {result.fast_model!r} 与配置生效值 "
            f"{effective['default']!r} / {effective['fast']!r} 不一致"
            "（可能被更高优先级配置层，如项目 .agent/config.yaml 覆盖）。"
            "为避免写入后跳过首次配置向导，已终止且未写入 Provider 环境变量。"
            f"请移除覆盖 {model_key} 的更高优先级配置后重试。"
        )
    env_values: dict[str, str] = {f"{result.provider.upper()}_API_URL": result.base_url}
    if option.requires_key:
        env_values[f"{result.provider.upper()}_API_KEY"] = result.api_key or ""
    config_mgr.set_global_env(env_values)
    config_mgr.reload()
    _check_persisted(config_mgr, result, option, role_name)


def _expected_slots(result: SetupResult) -> dict[str, str]:
    """把向导结果转成角色模型双槽位 mapping。

    Args:
        result: 向导确认的配置结果。

    Returns:
        ``{"default": 默认模型, "fast": 快速模型}``。
    """
    return {"default": result.default_model, "fast": result.fast_model}


def _effective_slots(config_mgr: ConfigManager, role_name: str) -> dict[str, object]:
    """读取合并配置中角色模型父键的两个槽位有效值。

    Args:
        config_mgr: 配置管理器。
        role_name: 角色 mapping 的真实 key。

    Returns:
        以槽位名为键的有效值 mapping；父键缺失或不是 mapping（含旧标量格式）时
        两个槽位均为 None。
    """
    try:
        raw = config_mgr.get_config_parts(("role", role_name, "model"))
    except KeyError:
        raw = None
    if not isinstance(raw, Mapping):
        return {"default": None, "fast": None}
    return {"default": raw.get("default"), "fast": raw.get("fast")}


def _validate_result(config_mgr: ConfigManager, result: SetupResult) -> ProviderOption:
    """校验 result 合法性并返回其候选 ProviderOption。

    Raises:
        LLMConfigurationError: provider 不是候选、URL 为空、云 provider key 为空或
            任一槽位模型为空（消息不含 key 值）。
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
    if not isinstance(result.fast_model, str) or not result.fast_model.strip():
        raise LLMConfigurationError("fast_model 必须是非空 str")
    return option


def _check_persisted(
    config_mgr: ConfigManager,
    result: SetupResult,
    option: ProviderOption,
    role_name: str,
) -> None:
    """reload 后置检查：两个模型槽位、base_url 与（云）api_key 必须与 result 一致。

    Args:
        config_mgr: 配置管理器。
        result: 向导确认的配置结果。
        option: result 对应的候选 ProviderOption。
        role_name: 角色 mapping 的真实 key。

    Raises:
        LLMConfigurationError: 任一后置检查不一致（消息不含 key 值）。
    """
    providers = config_mgr.get_config("llm_provider")
    if not isinstance(providers, Mapping):
        raise LLMConfigurationError("reload 后无法读取 llm_provider 配置")
    provider_cfg = providers.get(result.provider)
    if not isinstance(provider_cfg, Mapping):
        raise LLMConfigurationError(f"reload 后无法读取 provider {result.provider!r} 的配置")
    if _effective_slots(config_mgr, role_name) != _expected_slots(result):
        raise LLMConfigurationError(
            f"reload 后 {format_role_config_key(role_name, 'model', 'default')} / "
            f"{format_role_config_key(role_name, 'model', 'fast')} "
            "与所选模型不一致，请检查全局配置"
        )
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
    """构造清洗成单行后仍完整、可粘贴且不含秘密的非 TTY 配置指引。

    Args:
        config_mgr: 配置管理器。
        options: 可配置的 Provider 候选。

    Returns:
        完整槽位键、流式 YAML、配置路径与凭据路径指引。
    """
    candidates = "、".join(option.name for option in options)
    role = _setup_role_name(config_mgr)
    model_key = format_role_config_key(role, "model")
    default_key = format_role_config_key(role, "model", "default")
    fast_key = format_role_config_key(role, "model", "fast")
    example = role_model_yaml_example(
        role,
        "<default-model-id>",
        "<fast-model-id>",
    )
    required_keys = (
        f"{default_key} 与 {fast_key}"
        if len(role) <= 48
        else f"{model_key} 下的 default 与 fast"
    )
    return (
        "未检测到 LLM Provider 配置，非 TTY 无法启动向导。必填 "
        f"{required_keys}。"
        f"YAML 样例：{example}。"
        f"配置：{config_mgr.global_dir / 'config.yaml'}；"
        f"凭据：{config_mgr.global_dir / '.env'}，写入 {{NAME}}_API_URL 与 "
        "{NAME}_API_KEY（Ollama 只需 OLLAMA_API_URL）。"
        f"候选 Provider：{candidates}。配置后重新运行。"
    )
