import logging
from src.app.app import AgentApp

logger = logging.getLogger(__name__)

async def create_app() -> AgentApp:
    return AgentApp()
