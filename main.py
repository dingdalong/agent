"""Agent 入口。"""

import asyncio

from app.bootstrap import create_app

async def main():
    app = await create_app()
    try:
        await app.run()
    finally:
        await app.shutdown()

def cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    cli()
