"""有界命令与进程会话工具。"""
import time
import uuid

from pydantic import BaseModel, Field

from src.mgr.frozen import clean_env
from src.tools.decorator import tool
from src.tools.display import ToolResult
from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolPolicy


class AdditionalPermissions(BaseModel):
    writable_roots: list[str] = Field(default_factory=list, max_length=16)
    network: bool = False


class ExecCommand(BaseModel):
    stdin_open: bool = Field(False, description="需要后续 write_stdin 发送输入时才设为 true；默认 stdin 关闭")
    additional_permissions: AdditionalPermissions | None = None
    justification: str | None = None
    cmd: str = Field(..., min_length=1, description="Shell 命令；文件发现用 rg --files，搜索用 rg -n，读取已知区段用 sed -n；独立命令同轮调用")
    workdir: str | None = Field(None, description="工作目录，默认当前工作区")
    yield_time_ms: int = Field(10000, ge=250, le=30000, description="返回仍在运行的 session_id 前的等待时间；默认 10000 ms")
    timeout_ms: int = Field(300000, ge=1, le=600000)
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")


@tool(model=ExecCommand, description="执行命令；短暂等待后返回退出状态或 session_id。真实 Shell 在沙箱内执行；计划模式仅可写专用临时目录，执行模式另可写工作区；默认无网络，扩权须说明用途。", policy=ToolPolicy(AccessKind.REVIEW, DataFlow.DYNAMIC, (PathArgument('workdir', PathRole.READ),), detail_template='{cmd}'))
async def exec_command(cmd, workdir, yield_time_ms, timeout_ms, max_output_tokens, additional_permissions, justification, stdin_open, deps, agent, authorization):
    resolver = deps.permission_mgr.path_resolver
    for grant in authorization.path_grants:
        resolver.revalidate(grant, grant.path)
    cwd = resolver.resolve(workdir)
    if not cwd.is_dir():
        return ToolResult.failure('invalid_workdir', f'工作目录不存在：{cwd}')
    environment = deps.data_guard.safe_environment(clean_env(getattr(deps.config_mgr, 'environment', None)))
    started = time.monotonic()
    result = await deps.process_mgr.start((deps.session_id, str(agent.uuid)), cmd, cwd, environment, authorization.execution_policy, timeout_ms, yield_time_ms, stdin_open)
    if result.error_code is None:
        result.output_kind = "exec"
        result.chunk_id = uuid.uuid4().hex[:6]
        result.wall_time_seconds = time.monotonic() - started
    return result


class WriteStdin(BaseModel):
    model_config = {"json_schema_extra": {"not": {
        "required": ["terminate", "chars"],
        "properties": {"terminate": {"const": True}, "chars": {"minLength": 1}},
    }}}
    session_id: str
    chars: str = ''
    yield_time_ms: int = Field(5000, ge=250, le=300000, description="等待增量输出的时间；空轮询默认 5000 ms")
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")
    terminate: bool = False


@tool(model=WriteStdin, description="获取进程增量输出、发送 stdin 或终止；不重放已消费输出。", policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True))
async def write_stdin(session_id, chars, yield_time_ms, max_output_tokens, terminate, deps, agent):
    started = time.monotonic()
    result = await deps.process_mgr.poll((deps.session_id, str(agent.uuid)), session_id, chars, yield_time_ms, terminate, plan_active=agent.plan_active)
    if result.error_code is None:
        result.output_kind = "exec"
        result.chunk_id = uuid.uuid4().hex[:6]
        result.wall_time_seconds = time.monotonic() - started
    return result
