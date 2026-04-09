from pydantic import BaseModel, ConfigDict
from src.llm import LLMProvider
from src.interfaces import UserInterface

class AgentDeps(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm: LLMProvider
    ui: UserInterface
