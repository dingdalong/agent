from types import SimpleNamespace
import pytest

from src.mgr.data_guard import DataGuard
from src.mgr.permission_mgr import PermissionManager
from src.mgr.process_mgr import ProcessMgr
from src.mgr.tools_mgr import ToolsMgr
from src.mgr.features import ALL_FEATURES
from src.mode import RunMode


@pytest.fixture
def runtime(tmp_path, request):
    import sys
    if sys.platform == "win32" and request.node.get_closest_marker("integration"):
        pytest.skip("真实 Shell 沙箱仅支持 macOS/Linux")
    guard = DataGuard({'test': 'sentinel-secret'})
    deps = SimpleNamespace(session_id='session', workdir=tmp_path, data_guard=guard,
        config_mgr=SimpleNamespace(environment={}), hooks_mgr=None, event_bus=None,
        turn_clock=None, context_mgr=None, process_mgr=ProcessMgr(), tools_mgr=ToolsMgr(),
        permission_mgr=PermissionManager(str(tmp_path), None, None, guard))
    agent = SimpleNamespace(uuid='main', agent_type='main', is_subagent=False,
        mode=RunMode.PLAN, features=set(ALL_FEATURES), tools=None, history=[], deps=deps,
        llm=SimpleNamespace(estimate_tokens=lambda messages: sum(len(m.get('content','').encode()) for m in messages)))
    yield deps, agent
    deps.tools_mgr.reload()
