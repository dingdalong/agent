"""静态环境基线采集 — 一次算好、全 agent 共用的仓库客观事实。

主 agent 与子 agent 需要共享仓库的静态环境事实，否则每个子 agent 开局都要先
`ls -la` 摸一遍仓库长什么样。本模块把这些
**整个会话不变**的客观事实一次性算好，由 `AgentApp._reset_session()` 存进
`AgentDeps.env_baseline`，`PromptMgr._build_environment_context()` 只做字符串拼接，
并作为带来源标记的外部 user 上下文进入历史。

两条不可违反的约束：

1. **必须在 bootstrap 侧算，不能在 PromptMgr 里算。** `PromptMgr.build()` 的调用点在
   `Agent._on_check_compact()` 这个 async 函数里，在那里跑 git 子进程 + 目录扫描会
   卡住事件循环（违反 CLAUDE.md 的异步/阻塞契约）；且每个子 agent 都新建自己的
   PromptMgr，一次 plan 流程 10+ 次委派就是 10+ 次重算。本模块全是同步 I/O，
   调用方必须用 `asyncio.to_thread` 卸载。
2. **产出必须对所有 agent 逐字节相同。** 这是它存在 `AgentDeps`（算一次）而非
   `PromptMgr`（每 agent 算一次）的第二个理由，也是为什么这里只放会话内稳定的事实
   （分支名、HEAD、目录结构），不放 `git status` 这类随时在变的输出。
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from src.common.frozen import clean_env

logger = logging.getLogger(__name__)

# 扫描目录树时跳过的目录名：构建产物、虚拟环境、各类缓存与 IDE 配置。
# 它们对"仓库长什么样"零信息量，却能轻易撑爆条数预算。
_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", "node_modules", "vendor",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", "bin", "obj",
    ".idea", ".vscode", ".agent", ".claude",
    "site-packages", ".next", ".nuxt", ".gradle", ".worktrees",
})

# 技术栈入口文件：存在与否直接告诉 LLM 这是什么语言的项目、用什么构建。
# 只报存在性，不读内容。
_STACK_MARKERS = (
    "pyproject.toml", "setup.py", "requirements.txt", "uv.lock", "Pipfile",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "CMakeLists.txt", "Makefile", "composer.json", "Gemfile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
)

_MAX_FIRST_LEVEL = 25      # 第 1 层最多列出的条目数
_MAX_SECOND_LEVEL = 8      # 每个父目录下第 2 层最多列出的子目录数
_MAX_TREE_LINES = 30       # 目录树最多行数
_MAX_TREE_CHARS = 800      # 目录树最多字符数
_MAX_TOTAL_CHARS = 1200    # 基线全文最多字符数（约 400 token）
_GIT_TIMEOUT = 2.0         # git 子进程超时秒数，超时即降级不阻塞启动


def _git_snapshot(workdir: Path) -> str | None:
    """采集 git 分支与 HEAD 短 hash。

    单次 `git rev-parse HEAD --abbrev-ref HEAD` 同时取两个值，避免两次子进程开销。
    参数顺序不能调换：`--abbrev-ref` 一旦出现就对**其后所有**参数生效，写成
    `--abbrev-ref HEAD --short HEAD` 会让两行都输出分支名。因此这里取完整 hash
    再自行截断。

    任何异常（非 git 仓库、git 不可用、超时、空仓库）一律返回 None 由调用方降级，
    不阻塞启动。

    Args:
        workdir: 用户工作目录。

    Returns:
        形如 "分支 `main`，HEAD `6613c07`" 的描述；不可用时返回 None。
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            # 冻结产物的动态库搜索路径被引导器改写过，子进程必须用 clean_env 构造
            env=clean_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    head, branch = lines[0][:7], lines[1]
    if branch == "HEAD":
        return f"detached HEAD `{head}`"
    return f"分支 `{branch}`，HEAD `{head}`"


def _scan_dir_names(path: Path, *, dirs_only: bool) -> tuple[list[str], list[str]]:
    """扫描单层目录，返回 (子目录名, 文件名) 两个已排序列表。

    跳过隐藏项与 _IGNORE_DIRS。任何 OSError（权限、竞态删除）返回空列表。

    Args:
        path: 要扫描的目录。
        dirs_only: 为 True 时不收集文件名（第 2 层只关心目录）。

    Returns:
        (子目录名列表, 文件名列表)，均已按名称排序。
    """
    dirs: list[str] = []
    files: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if name in _IGNORE_DIRS or name.endswith(".egg-info"):
                        continue
                    dirs.append(name)
                elif not dirs_only:
                    files.append(name)
    except OSError:
        return [], []
    return sorted(dirs), sorted(files)


def _top_level_tree(workdir: Path) -> list[str]:
    """构建深度 2 的目录结构摘要。

    第 1 层列出目录（第 2 层的子目录以 `{a, b, c}` 内联在同一行），第 2 层只看目录
    不看文件——深度 2 已足以让 LLM 判断"源码在 src/、测试在 tests/"，深度 3 起体积
    暴涨而边际信息骤降。

    Args:
        workdir: 用户工作目录。

    Returns:
        缩进两格的目录树文本行；超出行数或字符预算时截断并追加省略提示。
    """
    dirs, _ = _scan_dir_names(workdir, dirs_only=False)
    if not dirs:
        return []

    truncated_dirs = dirs[:_MAX_FIRST_LEVEL]
    lines: list[str] = []
    used_chars = 0
    for name in truncated_dirs:
        children, _ = _scan_dir_names(workdir / name, dirs_only=True)
        if children:
            shown = children[:_MAX_SECOND_LEVEL]
            suffix = ", …" if len(children) > _MAX_SECOND_LEVEL else ""
            line = f"  {name}/{{{', '.join(shown)}{suffix}}}"
        else:
            line = f"  {name}/"
        if len(lines) >= _MAX_TREE_LINES or used_chars + len(line) > _MAX_TREE_CHARS:
            lines.append(f"  …（另有 {len(dirs) - len(lines)} 个顶层目录未列出）")
            return lines
        lines.append(line)
        used_chars += len(line)

    if len(dirs) > len(truncated_dirs):
        lines.append(f"  …（另有 {len(dirs) - len(truncated_dirs)} 个顶层目录未列出）")
    return lines


def collect_env_baseline(workdir: Path, *, max_chars: int = _MAX_TOTAL_CHARS) -> str:
    """采集可直接嵌入 system prompt 的环境基线文本。

    全部为同步 I/O（git 子进程 + 目录扫描），**调用方必须用 asyncio.to_thread 卸载**，
    否则会冻结事件循环。同一 workdir 的连续两次调用结果必须完全相同（缓存友好性）。

    Args:
        workdir: 用户工作目录，接受 Path 或 str。
        max_chars: 产出文本的硬上限，超出即截断。

    Returns:
        多行文本（不含「# 运行环境」标题）；无任何可报告事实时返回空字符串。
    """
    workdir = Path(workdir)
    lines: list[str] = []

    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC")
    if shell:
        lines.append(f"shell：`{shell}`")

    git_info = _git_snapshot(workdir)
    if git_info:
        lines.append(f"git（会话启动时快照，之后可能已变化）：{git_info}")
    else:
        lines.append("git：非 git 仓库或 git 不可用")

    markers = [name for name in _STACK_MARKERS if (workdir / name).is_file()]
    if markers:
        lines.append("技术栈入口：" + "、".join(markers))

    tree = _top_level_tree(workdir)
    if tree:
        lines.append("顶层结构（深度 2，仅目录）：")
        lines.extend(tree)

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（环境基线已截断）"
    return text
