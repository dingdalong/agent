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
│   └── config.yaml
└── README.md
```

`config.yaml` 内容：

```yaml
role:
  default: mijia
```

角色资产（子 agent、技能、人设、AGENTS.md）位于 `src/roles/mijia/`，
与 `src/roles/coding/` 平级，均为内置角色。

## 设备工具与技能

连接米家 MCP server 后，主 agent 使用运行时实际注册的工具查询和控制设备。
控制、诊断、场景管理分别通过 `builtin:control-devices`、`builtin:diagnose-home`、
`builtin:manage-scenes` 加载方法；独立任务可由通用子 agent 使用同一技能执行。
工具缺失时报告缺口，不需要修改内置资源回填工具名。

## 切换回编程角色

不加 `--workdir` 即可：

```bash
uv run python main.py
```
