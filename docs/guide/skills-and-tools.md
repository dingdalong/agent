# 技能与工具

## 内置工具

### 计算器（calculator）

安全的数学表达式计算，基于 AST 解析（不使用 eval）。

```
你: 帮我算一下 (123 + 456) * 789
```

### 文件读写（file）

沙箱化的文件操作，限制在 `workspace/` 目录内。

```
你: 读取 workspace/notes.txt 的内容
你: 把这段内容写入 workspace/output.txt
```

## MCP 工具集成

通过 [Model Context Protocol](https://modelcontextprotocol.io/) 接入外部工具服务。

### 配置

编辑 `mcp_servers.json`：

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {}
    }
  }
}
```

每个 MCP 服务器需要指定：
- `transport`：通信方式（`stdio` 或 `http`）
- `command`：启动命令（stdio 模式）
- `args`：命令参数
- `env`：环境变量

### 添加新的 MCP 服务器

在 `mcpServers` 下添加新条目即可。启动时框架会自动连接所有配置的服务器并发现其工具。

## 技能系统

技能是通过 Markdown 文件定义的可激活指令集，为智能体注入特定领域的行为。

### 技能文件格式

在 `skills/` 目录下创建文件夹，包含 `SKILL.md`：

```
skills/
└── my-skill/
    └── SKILL.md
```

`SKILL.md` 格式：

```markdown
---
name: my-skill
description: 简短描述这个技能的用途
---

这里是技能的指令内容，会注入到智能体的 system prompt 中。
```

### 使用技能

通过斜杠命令激活：

```
你: /my-skill 请按照技能要求执行任务
```

技能激活后，框架会创建一个独立的智能体图，将技能指令注入编排器，然后执行用户请求。

### 内置技能示例

- `/code-review` — 代码审查
- `/translate` — 翻译

## 规划系统

处理需要多步骤的复杂任务。

### 使用方式

```
你: /plan 查询北京天气，然后把结果发邮件给同事
```

### 执行流程

1. **澄清阶段** — 如果请求不够清楚，系统会提问澄清
2. **计划生成** — LLM 生成包含多个步骤的执行计划
3. **确认/调整** — 用户确认计划或要求调整
4. **编译** — 计划编译为可执行的图（支持并行步骤）
5. **执行** — 图引擎按拓扑顺序执行各步骤
