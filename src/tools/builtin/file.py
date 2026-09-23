"""文本补丁工具。文件发现、搜索与读取统一由 exec_command 承担。"""
from pydantic import BaseModel, Field, field_validator

from src.tools.decorator import tool
from src.mode import RunMode
from src.common.tool_policy import AccessKind, DataFlow, ToolAvailability, ToolPolicy


class ApplyPatch(BaseModel):
    patch: str = Field(..., description='补丁文本；字段名必须是 patch（不是 input）；格式见本工具说明')

    @field_validator("patch")
    @classmethod
    def _validate_patch(cls, value: str) -> str:
        """在参数校验阶段判定补丁格式，避免结构错误被报成权限拒绝。

        Args:
            value: 模型给出的补丁文本。

        Returns:
            原样返回通过校验的补丁文本。

        Raises:
            ValueError: 补丁结构非法；由调用方归一化为 invalid_arguments。
        """
        from src.common.patch import parse_patch

        try:
            parse_patch(value)
        except ValueError as exc:
            raise ValueError(
                f"{exc}；补丁行必须以空格（上下文，原样保留）、+（新增）或 -（删除）开头"
            ) from exc
        return value


@tool(
    model=ApplyPatch,
    description=(
        '通过唯一文本上下文新增、修改、删除或移动文本文件。行号不参与定位；不匹配时不写入。'
        '结构：*** Begin Patch 包裹一个或多个 *** Add File: 路径 / *** Update File: 路径 / *** Delete File: 路径 段，'
        'Update 段内用 @@ 分隔上下文块，可带 *** Move to: 路径 与文件尾锚点 *** End of File，最后 *** End Patch。'
        '行首空格=上下文行（原样保留），+ =新增行，- =删除行；Add File 的内容行必须全部以 + 开头。'
        '示例（每行各占一行）：*** Begin Patch / *** Update File: src/a.py / @@ / -    return 1 / +    return 2 / *** End Patch。'
    ),
    policy=ToolPolicy(AccessKind.WORKSPACE_WRITE, DataFlow.LOCAL),
    availability=ToolAvailability(frozenset({RunMode.EXECUTE}), feature='file'),
)
def apply_patch(patch, deps, authorization):
    from src.common.patch import apply_patch as apply
    return apply(patch, deps.permission_mgr.path_resolver, authorization)
