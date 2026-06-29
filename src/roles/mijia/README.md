# 米家智能家居角色

基于 Agent 框架内置 `mijia` 角色的启动配置示例。

## 快速开始

```bash
# 1. 设置米家账号凭证
export MI_USER_ID="your_mi_user_id"
export MI_TOKEN="your_mi_token"

# 2. 启动
uv run python main.py --workdir examples/mijia
```

## 目录说明

```
mijia/
├── .agent/
│   └── config.yaml    # role: mijia
└── README.md
```

角色资产（子 agent、技能、人设、AGENT.md）位于 `src/roles/mijia/`，
与 `src/roles/coding/` 平级，均为内置角色。

## MCP 工具名回填

子 agent 的 `tools:` 白名单依赖米家 MCP server 暴露的具体工具名。
连接 MCP server 后运行一次应用，从日志中找到注册的 `mcp__mijia__*` 名称，
回填到 `src/roles/mijia/agents/*.md` 的 `tools:` 字段。

## 切换回编程角色

不加 `--workdir` 即可：

```bash
uv run python main.py
```
