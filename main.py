"""Agent 入口。"""

import argparse
import asyncio

from app.bootstrap import create_app


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
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    """应用主函数。

    Args:
        args: 命令行参数。
    """
    app = await create_app(workdir_override=args.workdir)
    try:
        await app.run()
    finally:
        await app.shutdown()


def cli() -> None:
    """CLI 入口点。"""
    args = parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
