"""run the sidecar:  poetry run python -m src.sidecar

env:
    SIDECAR_DB       sqlite path (default: chordial_sidecar.db in the cwd)
    SIDECAR_PORT     loopback port (default: 8485)
"""
import asyncio
import logging
import os

from aiohttp import web

from src.sidecar.server import SIDECAR_HOST, SIDECAR_PORT, SidecarService
from src.sidecar.store import SidecarStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sidecar")


async def main() -> None:
    store = SidecarStore(os.getenv("SIDECAR_DB", "chordial_sidecar.db"))
    service = SidecarService(store)
    runner = web.AppRunner(service.build_app())
    await runner.setup()
    site = web.TCPSite(runner, SIDECAR_HOST, SIDECAR_PORT)
    await site.start()
    logger.info("sidecar on http://%s:%s (the deer is home)",
                SIDECAR_HOST, SIDECAR_PORT)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
