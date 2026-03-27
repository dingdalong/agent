"""Agent 入口。"""

import asyncio

from src.app import create_app


async def main():
    app = await create_app()
    try:
        await app.run()
    finally:
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
