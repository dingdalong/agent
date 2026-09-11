"""有界命令与进程会话工具。"""
from pydantic import BaseModel, Field

from src.mgr.frozen import clean_env
from src.tools.decorator import tool
from src.tools.display import ToolResult
from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolPolicy


class ExecCommand(BaseModel):
    command: str = Field(..., min_length=1, description="Shell 命令；探索优先 rg/sed，并主动限制输出")
    workdir: str | None = Field(None, description="工作目录，默认当前工作区")
    yield_time_ms: int = Field(1000, ge=0, le=30000)
    timeout_ms: int = Field(300000, ge=1, le=600000)
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")


@tool(model=ExecCommand, description="执行命令；短暂等待后返回退出状态或 session_id。计划模式仅允许经参数校验的只读命令与安全管道。", policy=ToolPolicy(AccessKind.REVIEW, DataFlow.DYNAMIC, (PathArgument('workdir', PathRole.READ),), detail_template='{command}'))
async def exec_command(command, workdir, yield_time_ms, timeout_ms, max_output_tokens, deps, agent, authorization):
    resolver = deps.permission_mgr.path_resolver
    for grant in authorization.path_grants:
        resolver.revalidate(grant, grant.path)
    cwd = resolver.resolve(workdir)
    if not cwd.is_dir():
        return ToolResult.failure('invalid_workdir', f'工作目录不存在：{cwd}')
    environment = deps.data_guard.safe_environment(clean_env(getattr(deps.config_mgr, 'environment', None)))
    return await deps.process_mgr.start((deps.session_id, str(agent.uuid)), command, cwd, environment, authorization.command_plan, timeout_ms, yield_time_ms)


class WriteStdin(BaseModel):
    session_id: str
    chars: str = ''
    yield_time_ms: int = Field(1000, ge=0, le=30000)
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")
    terminate: bool = False


@tool(model=WriteStdin, description="获取进程增量输出、发送 stdin 或终止；不重放已消费输出。", policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True))
async def write_stdin(session_id, chars, yield_time_ms, max_output_tokens, terminate, deps, agent):
    if agent.plan_active and chars:
        return ToolResult.failure('permission_denied', '计划模式不发送 stdin')
    return await deps.process_mgr.poll((deps.session_id, str(agent.uuid)), session_id, chars, yield_time_ms, terminate)
