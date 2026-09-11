"""有界命令与进程会话工具。"""
from pydantic import BaseModel, Field

from src.mgr.frozen import clean_env
from src.tools.decorator import tool
from src.tools.display import ToolResult
from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolPolicy


class ExecCommand(BaseModel):
    cmd: str = Field(..., min_length=1, description="Shell 命令；文件发现用 rg --files，内容搜索用 rg -n；普通读取用 read_file；独立命令同轮调用")
    workdir: str | None = Field(None, description="工作目录，默认当前工作区")
    yield_time_ms: int = Field(1000, ge=0, le=30000)
    timeout_ms: int = Field(300000, ge=1, le=600000)
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")


@tool(model=ExecCommand, description="执行命令；短暂等待后返回退出状态或 session_id。计划模式支持只读命令、管道、&&、分号、开头 cd、路径通配符及 2>/dev/null、2>&1、1>&2；不支持变量、命令替换和写入。", policy=ToolPolicy(AccessKind.REVIEW, DataFlow.DYNAMIC, (PathArgument('workdir', PathRole.READ),), detail_template='{cmd}'))
async def exec_command(cmd, workdir, yield_time_ms, timeout_ms, max_output_tokens, deps, agent, authorization):
    resolver = deps.permission_mgr.path_resolver
    for grant in authorization.path_grants:
        resolver.revalidate(grant, grant.path)
    cwd = resolver.resolve(workdir)
    if not cwd.is_dir():
        return ToolResult.failure('invalid_workdir', f'工作目录不存在：{cwd}')
    environment = deps.data_guard.safe_environment(clean_env(getattr(deps.config_mgr, 'environment', None)))
    return await deps.process_mgr.start((deps.session_id, str(agent.uuid)), cmd, cwd, environment, authorization.command_plan, timeout_ms, yield_time_ms)


class WriteStdin(BaseModel):
    model_config = {"json_schema_extra": {"not": {
        "required": ["terminate", "chars"],
        "properties": {"terminate": {"const": True}, "chars": {"minLength": 1}},
    }}}
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
