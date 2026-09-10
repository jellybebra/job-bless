"""Compatibility entry point: HH login is now handled in the web panel."""
import asyncio
import sys
from src.config import Config
from src.web.app import serve


async def run_manual_login(config_path=None):
    await serve(Config.load(config_path))


if __name__ == "__main__":
    asyncio.run(run_manual_login(sys.argv[1] if len(sys.argv) > 1 else None))
