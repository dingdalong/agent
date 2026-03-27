# AI Agent Framework

从零构建的 Python AI Agent 框架，支持多智能体协作、长期记忆和动态规划。

## 特性

- **多智能体系统** — 编排器 + 专家智能体，支持 handoff 协作
- **向量记忆系统** — ChromaDB 存储，自动事实提取，重要性衰减
- **图执行引擎** — 顺序/并行节点执行，handoff 路由，执行追踪
- **动态规划系统** — LLM 驱动的多步骤计划生成、编译与执行
- **MCP 协议集成** — 通过 Model Context Protocol 接入外部工具
- **技能系统** — Markdown 定义技能，斜杠命令激活
- **输入安全守卫** — 正则匹配拦截危险输入
- **端口-适配器架构** — Protocol 接口定义，可扩展替换任意组件

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) — Python 包管理工具
- [Ollama](https://ollama.com/) — 本地 embedding 服务

### 安装

```bash
git clone <repo-url>
cd aitest
uv sync
```

### 配置

1. 创建 `.env` 文件，填入 API Key：

```bash
OPENAI_API_KEY=your-api-key-here
```

2. 编辑 `config.yaml` 配置 LLM 和 embedding 服务（默认使用 DeepSeek + Ollama）

3. 启动 Ollama 并拉取 embedding 模型：

```bash
ollama pull qwen3-embedding:0.6b
```

### 运行

```bash
uv run python main.py
```

## 项目结构

```
main.py                  # 入口
config.yaml              # 用户配置
src/
├── config.py            # 配置加载器
├── interfaces/          # 用户交互协议（CLI）
├── llm/                 # LLM 抽象层（OpenAI 兼容）
├── memory/              # 记忆系统（ChromaDB + 事实提取 + 衰减）
├── agents/              # 智能体模型、运行器、注册表
├── graph/               # 图执行引擎
├── plan/                # 动态规划系统
├── tools/               # 工具注册、执行、中间件
├── mcp/                 # MCP 协议客户端
├── skills/              # 技能系统
├── guardrails/          # 输入安全守卫
├── utils/               # 工具函数
└── app/                 # 应用组装与 REPL
tests/                   # 测试（镜像 src/ 结构）
docs/                    # 文档
```

## 文档

- [快速入门](docs/guide/getting-started.md)
- [配置详解](docs/guide/configuration.md)
- [技能与工具](docs/guide/skills-and-tools.md)
- [架构总览](docs/architecture/overview.md)
- [贡献指南](docs/contributing.md)

## License

MIT
