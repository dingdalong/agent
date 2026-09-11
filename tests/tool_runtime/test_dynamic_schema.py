import asyncio
import pytest
from pydantic import BaseModel, ConfigDict
from src.tools.decorator import ToolEntry
from src.tools.policy import ToolOrigin


def test_dynamic_tool_uses_server_schema():
    class Args(BaseModel):
        model_config = ConfigDict(extra='allow')
    entry = ToolEntry('mcp__test__lookup', lambda **kw: kw, Args, '', {
        'type': 'object', 'properties': {'query': {'type': 'string'}},
        'required': ['query'], 'additionalProperties': False,
    }, origin=ToolOrigin('mcp'))
    assert entry.validate_arguments({'query': 'symbol'}) == {'query': 'symbol'}
    for invalid in [{}, {'query': 12}, {'query': 'symbol', 'extra': True}]:
        with pytest.raises(ValueError):
            entry.validate_arguments(invalid)
    result = asyncio.run(entry({}, query='symbol'))
    assert result.status == 'success'
