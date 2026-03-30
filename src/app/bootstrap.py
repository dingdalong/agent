"""应用组装 — 读配置、创建组件、注入依赖。"""

from __future__ import annotations

import logging
from pathlib import Path

from src.config import load_config
from src.llm.openai import OpenAIProvider
from src.interfaces.cli import CLIInterface
from src.tools.decorator import get_registry
from src.tools.discovery import discover_tools
from src.tools.executor import ToolExecutor
from src.tools.router import ToolRouter, LocalToolProvider
from src.tools.middleware import (
    error_handler_middleware,
    sensitive_confirm_middleware,
    truncate_middleware,
)
from src.mcp.config import load_mcp_config
from src.mcp.manager import MCPManager
from src.mcp.provider import MCPToolProvider
from src.skills.manager import SkillManager
from src.skills.provider import SkillToolProvider
from src.guardrails import InputGuardrail
from src.agents import AgentRegistry, AgentRunner
from src.graph import GraphEngine
from src.agents.deps import AgentDeps
from src.memory import ChromaMemoryStore, ConversationBuffer
from src.memory.utils import build_collection_name
from src.app.presets import build_default_graph
from src.app.app import AgentApp

logger = logging.getLogger(__name__)


async def create_app(config_path: str = "config.yaml") -> AgentApp:
    """应用组装入口 — 整个框架唯一的具体实现实例化点。

    组装流程：
    1. 加载 config.yaml + .env
    2. 创建 LLM provider（OpenAIProvider）
    3. 发现并注册本地工具，构建中间件管道
    4. 连接 MCP 服务器，注册外部工具
    5. 发现技能
    6. 注册预设智能体，构建默认图
    7. 组装 AgentDeps 依赖容器
    8. 返回 AgentApp 实例
    """
    raw = load_config(config_path)
    llm_cfg = raw.get("llm", {})
    ui = CLIInterface()

    # 1. LLM
    llm = OpenAIProvider(
        api_key=llm_cfg.get("api_key", ""),
        base_url=llm_cfg.get("base_url", ""),
        model=llm_cfg.get("model", ""),
        concurrency=llm_cfg.get("concurrency", 5),
        max_retries=llm_cfg.get("max_retries", 3),
        on_chunk=ui.display,
    )

    # 2. Tools
    discover_tools("src.tools.builtin", Path("src/tools/builtin"))
    registry = get_registry()
    executor = ToolExecutor(registry)
    middlewares = [
        error_handler_middleware(),
        sensitive_confirm_middleware(registry, ui),
        truncate_middleware(raw.get("tools", {}).get("max_output_length", 2000)),
    ]
    tool_router = ToolRouter()
    tool_router.add_provider(LocalToolProvider(registry, executor, middlewares))

    # 3. MCP
    mcp_config_path = raw.get("mcp", {}).get("config_path", "mcp_servers.json")
    mcp_manager = MCPManager()
    await mcp_manager.connect_all(load_mcp_config(mcp_config_path))
    if mcp_manager.get_tools_schemas():
        tool_router.add_provider(MCPToolProvider(mcp_manager))

    # 4. Skills
    skill_dirs = raw.get("skills", {}).get("dirs", ["skills/", ".agents/skills/"])
    skill_manager = SkillManager(skill_dirs=skill_dirs)
    await skill_manager.discover()
    if skill_manager._skills:
        tool_router.add_provider(SkillToolProvider(skill_manager))

    # 5. Memory
    embedding_cfg = raw.get("embedding", {})
    memory_cfg = raw.get("memory", {})
    user_cfg = raw.get("user", {})
    memory_store = None
    conversation_buffer = None

    if (
        memory_cfg.get("provider") == "chroma"
        and embedding_cfg.get("model")
        and embedding_cfg.get("base_url")
    ):
        try:
            collection_name = build_collection_name(
                "memories", user_cfg.get("id", "default_user")
            )
            memory_store = ChromaMemoryStore(
                embedding_model=embedding_cfg["model"],
                embedding_url=embedding_cfg["base_url"],
                collection_name=collection_name,
                persist_dir=memory_cfg.get("path", "./chroma_data"),
                llm=llm,
            )
            logger.info("[记忆系统] ChromaMemoryStore 已初始化")
        except Exception:
            logger.warning("[记忆系统] 初始化失败，降级为无记忆模式", exc_info=True)
            memory_store = None

    if memory_store is not None:
        agent_cfg_buf = raw.get("agents", {})
        conversation_buffer = ConversationBuffer(
            max_rounds=agent_cfg_buf.get("max_conversation_rounds", 10),
            max_tokens=agent_cfg_buf.get("max_conversation_tokens", 4096),
        )

    # 5.5 Tool Categories
    from src.tools.categories import load_categories, CategoryResolver

    categories_path = raw.get("tools", {}).get(
        "categories_path", "tool_categories.json"
    )
    category_resolver = None
    category_summaries: list[dict[str, str]] = []

    categories = load_categories(categories_path)
    if categories:
        category_resolver = CategoryResolver(categories)
        category_summaries = category_resolver.get_all_summaries()
        logger.info("[工具分类] 加载 %d 个类别", len(categories))
    else:
        logger.info("[工具分类] 未找到分类配置，跳过")

    # 6. Agents
    agent_cfg = raw.get("agents", {})
    agent_registry = AgentRegistry()
    if category_resolver:
        agent_registry.set_category_resolver(category_resolver)

    runner = AgentRunner(
        registry=agent_registry,
        max_tool_rounds=agent_cfg.get("max_tool_rounds", 10),
    )
    graph = build_default_graph(
        agent_registry,
        runner=runner,
        category_summaries=category_summaries,
    )
    engine = GraphEngine(max_handoff_depth=agent_cfg.get("max_handoffs", 10))

    # 7. Deps
    deps = AgentDeps(
        llm=llm,
        tool_router=tool_router,
        agent_registry=agent_registry,
        graph_engine=engine,
        ui=ui,
        memory=memory_store,
    )

    # 7.5 Delegate Tool Provider
    from src.tools.delegate import DelegateToolProvider

    if category_resolver:
        delegate_provider = DelegateToolProvider(
            resolver=category_resolver,
            runner=runner,
            registry=agent_registry,
            deps=deps,
        )
        tool_router.add_provider(delegate_provider)

    return AgentApp(
        deps=deps,
        ui=ui,
        guardrail=InputGuardrail(),
        tool_router=tool_router,
        agent_registry=agent_registry,
        engine=engine,
        graph=graph,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        runner=runner,
        conversation_buffer=conversation_buffer,
    )
