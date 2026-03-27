# 贡献指南

## 开发环境搭建

```bash
git clone <repo-url>
cd aitest
uv sync
```

配置 `.env` 文件（参见 [配置详解](guide/configuration.md)）。

## 代码规范

### 架构原则

- **Protocol 优先** — 新功能先定义 Protocol 接口，再实现具体类
- **分层依赖** — 低层模块不得导入高层模块（Layer 0 → 1 → 2 → 3）
- **异步优先** — 优先使用 async/await
- **集中组装** — 具体实现只在 `src/app/bootstrap.py` 中实例化
- **类型安全** — 所有函数签名必须有类型注解，优先使用 Protocol / TypedDict / dataclass / Pydantic model 定义数据结构

### 添加新模块

1. 在对应层定义 Protocol（如 `src/xxx/base.py`）
2. 实现具体类
3. 在 `src/app/bootstrap.py` 中实例化并注入
4. 编写测试
5. 更新相关文档

### 添加新工具

```python
# src/tools/builtin/my_tool.py
from pydantic import BaseModel
from src.tools.decorator import tool

class MyToolInput(BaseModel):
    param: str

@tool(model=MyToolInput, description="工具描述")
async def my_tool(args: MyToolInput) -> str:
    return f"结果: {args.param}"
```

工具会在启动时被 `discover_tools()` 自动发现和注册。

## 测试

```bash
uv run pytest              # 运行所有测试（跳过慢速测试）
uv run pytest -m slow      # 运行慢速/集成测试
uv run pytest -v           # 详细输出
```

测试目录结构镜像 `src/`：

```
tests/
├── llm/
├── agents/
├── memory/
├── tools/
├── graph/
├── plan/
├── mcp/
├── skills/
├── guardrails/
├── interfaces/
└── test_app.py
```

## 提交规范

使用语义化前缀：

| 前缀 | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | Bug 修复 |
| `refactor:` | 重构（不改变行为） |
| `test:` | 测试相关 |
| `docs:` | 文档相关 |
