import logging
from src.app.app import AgentApp
from src.config import load_config
from src.interfaces import CLIInterface
from src.events import EventBus, EventLevel
from src.llm.openai import OpenAIProvider
from src.agent import AgentDeps

logger = logging.getLogger(__name__)

async def create_app() -> AgentApp:
    config = load_config()

    ui = CLIInterface()
    event_bus = EventBus(level=EventLevel.from_str(config["events"].get("level", "progress")))

    default_llm_cfg = config["llm"]["default"]

    llm_provider_cfg = config["llm_provider"][default_llm_cfg["provider"]]
    llm = OpenAIProvider(
        api_key = llm_provider_cfg["api_key"],
        base_url = llm_provider_cfg["base_url"],
        model = default_llm_cfg["model"],
        concurrency = default_llm_cfg["concurrency"],
        max_retries = default_llm_cfg["max_retries"],
        event_bus = event_bus,
    )

    deps = AgentDeps(
        llm = llm,
        ui = ui,
    )
    return AgentApp(
        deps = deps,
        event_bus = event_bus,
    )
