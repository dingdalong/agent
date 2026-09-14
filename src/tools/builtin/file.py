"""文本补丁工具。文件发现、搜索与读取统一由 exec_command 承担。"""
from pydantic import BaseModel, Field

from src.tools.decorator import tool
from src.tools.policy import AccessKind, DataFlow, ToolPolicy


class ApplyPatch(BaseModel):
    patch: str = Field(..., description='*** Begin Patch / *** Add|Update|Delete File: path / @@ 上下文 / *** End Patch；Update 可带 *** Move to: path')


@tool(model=ApplyPatch, description='通过唯一文本上下文新增、修改、删除或移动文本文件。行号不参与定位；不匹配时不写入。', policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL), feature='file', modes=('execute',))
def apply_patch(patch, deps, authorization):
    from src.mgr.patch import apply_patch as apply
    return apply(patch, deps.permission_mgr.path_resolver, authorization)
