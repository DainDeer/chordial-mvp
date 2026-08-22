"""run the sidecar:  poetry run python -m src.sidecar

env:
    SIDECAR_DB       sqlite path (default: chordial_sidecar.db in the cwd
                     for a dev run; the app-data dir when frozen - see
                     src/sidecar/paths.py. the tauri shell sets this
                     explicitly when it spawns the bundled binary)
    SIDECAR_PORT     loopback port (default: 8485)
    CHORDIAL_SHELL_PID
                     the tauri shell's own pid, set by the shell when it
                     spawns the bundled binary. when that process is gone
                     the sidecar shuts itself down (src/sidecar/parent.py -
                     the shell's kill only reaches pyinstaller's bootloader,
                     never this interpreter). unset for a dev-terminal run:
                     no watch, you own it.

frozen into the app bundle (phase 7b), there is no terminal to read, so
logs also land in sidecar.log beside the database.
"""
import asyncio
import logging
import logging.handlers
import os

from aiohttp import web

from src.sidecar import parent
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
    shell_pid = parent.shell_pid_from_env(os.environ)
    service = SidecarService(store, shell_pid=shell_pid)
    runner = web.AppRunner(service.build_app())
    await runner.setup()
    site = web.TCPSite(runner, SIDECAR_HOST, SIDECAR_PORT)
    await site.start()
    logger.info("sidecar on http://%s:%s, state in %s (the deer is home)",
                SIDECAR_HOST, SIDECAR_PORT, db_path)
    stop = asyncio.Event()
    if shell_pid is not None:
        # the box: follow the shell out (its kill stops at the bootloader)
        logger.info("watching the shell (pid %s)", shell_pid)
        asyncio.create_task(parent.watch(
            shell_pid, stop.set, initial_ppid=os.getppid()))
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
