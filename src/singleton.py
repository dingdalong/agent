from src.config import config

from src.interfaces import CLIInterface
ui = CLIInterface()

from src.events import EventBus, EventLevel
event_bus = EventBus(level=EventLevel.from_str(config["events"].get("level", "progress")))

from src.todo import TodoManager
todo = TodoManager()

from src.llm.deepseek import DeepSeekProvider
default_llm_cfg = config["llm"]["default"]

llm_provider_cfg = config["llm_provider"][default_llm_cfg["provider"]]
llm = DeepSeekProvider(
    api_key = llm_provider_cfg["api_key"],
    base_url = llm_provider_cfg["base_url"],
    model = default_llm_cfg["model"],
    concurrency = default_llm_cfg["concurrency"],
    max_retries = default_llm_cfg["max_retries"],
)
