"""run the sidecar:  poetry run python -m src.sidecar

env:
    SIDECAR_DB       sqlite path (default: chordial_sidecar.db in the cwd
                     for a dev run; the app-data dir when frozen - see
                     src/sidecar/paths.py. the tauri shell sets this
                     explicitly when it spawns the bundled binary)
    SIDECAR_PORT     loopback port (default: 8485)

frozen into the app bundle (phase 7b), there is no terminal to read, so
logs also land in sidecar.log beside the database.
"""
import asyncio
import logging
import logging.handlers
import os

from aiohttp import web

from src.sidecar.paths import default_db_path, is_frozen
from src.sidecar.server import SIDECAR_HOST, SIDECAR_PORT, SidecarService
from src.sidecar.store import SidecarStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sidecar")


async def main() -> None:
    db_path = default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if is_frozen():
        handler = logging.handlers.RotatingFileHandler(
            db_path.parent / "sidecar.log",
            maxBytes=1_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logging.getLogger().addHandler(handler)
    store = SidecarStore(db_path)
    service = SidecarService(store)
    runner = web.AppRunner(service.build_app())
    await runner.setup()
    site = web.TCPSite(runner, SIDECAR_HOST, SIDECAR_PORT)
    await site.start()
    logger.info("sidecar on http://%s:%s, state in %s (the deer is home)",
                SIDECAR_HOST, SIDECAR_PORT, db_path)
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
