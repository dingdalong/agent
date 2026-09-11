"""精确文件读取与上下文补丁。目录和搜索由 exec_command 承担。"""
from pydantic import BaseModel, Field

from src.tools.decorator import tool
from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolPolicy


class ReadFile(BaseModel):
    path: str
    offset: int = Field(1, ge=1, description='起始行号，从 1 开始')
    column: int = Field(0, ge=0, description='起始行内字符位置，从 0 开始；用于超长行续读')
    limit: int | None = Field(None, ge=1, description='可选源文件行数上限；省略时自动读到输出预算或 EOF')
    max_output_tokens: int | None = Field(None, ge=128, le=16000, description="近似输出预算，省略时使用统一配置（默认 10000）")


@tool(model=ReadFile, description='有界读取文本文件，返回实际范围和 EOF；大范围探索优先 exec_command。', policy=ToolPolicy(AccessKind.LOCAL_READ, DataFlow.LOCAL, (PathArgument('path', PathRole.READ),), True), parallel=True, feature='file')
def read_file(path, offset, column, limit, max_output_tokens, agent, authorization):
    return agent._file_mgr.read_file(path, authorization, offset=offset, column=column, limit=limit)


class ApplyPatch(BaseModel):
    patch: str = Field(..., description='*** Begin Patch / *** Add|Update|Delete File: path / @@ 上下文 / *** End Patch；Update 可带 *** Move to: path')


@tool(model=ApplyPatch, description='通过唯一文本上下文新增、修改、删除或移动文本文件。行号不参与定位；不匹配时不写入。', policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL), feature='file')
def apply_patch(patch, deps, authorization):
    from src.mgr.patch import apply_patch as apply
    return apply(patch, deps.permission_mgr.path_resolver, authorization)
