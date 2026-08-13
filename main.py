"""Agent 入口。"""

import argparse
import asyncio
import sys

from src.app.bootstrap import create_app
from src.llm import LLMConfigurationError
from src.mgr import ModelUnavailableError
from src.mgr.frozen import setup_tiktoken_cache


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        解析后的参数命名空间。
    """
    parser = argparse.ArgumentParser(description="AI Agent CLI")
    parser.add_argument(
        "--workdir",
        default=None,
        help="工作目录，默认为当前目录",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用 asyncio 调试模式：事件循环被任一回调占用超过 0.1s 即打印慢回调告警，"
             "用于排查在 async 中误跑同步阻塞工作的代码。默认关闭。",
    )
    parser.add_argument(
        "--copy-on-select",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="鼠标选中文本后立即复制；macOS 默认启用，其他平台默认关闭",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="核对随包资源、内置工具/命令注册与离线编码是否完好，输出 JSON 后退出；"
             "用于验证打包产物（冻结后这些失效是静默的）。",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    """应用主函数。

    Args:
        args: 命令行参数。
    """
    app = await create_app(
        workdir_override=args.workdir,
        copy_on_select=args.copy_on_select,
    )
    try:
        await app.run()
    finally:
        await app.shutdown()


def cli() -> None:
    """运行 CLI 并把可预期的 LLM 启动错误转换为非零退出。

    Returns:
        None。
    """
    args = parse_args()
    # 必须在任何 tiktoken 调用之前：冻结产物把编码文件预热在包内，指过去才能离线启动。
    setup_tiktoken_cache()
    if args.self_check:
        from src.app.self_check import run_self_check

        sys.exit(run_self_check())
    try:
        # debug=True 启用 asyncio 慢回调告警（阈值默认 0.1s），暴露阻塞事件循环的协程。
        asyncio.run(main(args), debug=args.debug)
    except KeyboardInterrupt:
        pass
    except (LLMConfigurationError, ModelUnavailableError) as exc:
        # LLM 配置或默认模型不可用：打印可操作提示并以非零码退出，不抛堆栈。
        print(f"\n启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
