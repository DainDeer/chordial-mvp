"""the parent watch: the sidecar follows the shell out.

the shell kills its child on every exit path (app/src-tauri/src/sidecar.rs)
- but the child it holds is PyInstaller's onefile BOOTLOADER, a small
parent process that unpacks the bundle and then runs the real python
interpreter as ITS child. a SIGKILL on the bootloader can't be forwarded,
so the python process it started lives on, reparented to 1. found on the
first boxed launch: an orphaned sidecar eight minutes after the app had
quit, still holding the port (and the next launch would have adopted it,
masking the leak). a force-quit of the shell orphans both layers.

so the sidecar watches the SHELL itself, not the hand that spawned it: the
shell passes its own pid down as CHORDIAL_SHELL_PID, and when that pid is
gone the sidecar shuts down cleanly (runner cleanup, store close - better
than the SIGKILL it was meant to get). on unix a second signal rides along
for free: the bootloader is this process's parent, so the parent pid
changing (to 1 / launchd) means the shell's kill landed. no pid in the
env → no watch: a `python -m src.sidecar` in a dev terminal owns itself,
and a sidecar the shell merely ADOPTED was never handed a pid.

honest edge: the pid check is a liveness poll, so pid reuse inside one
poll interval could keep a sidecar alive past its shell; the parent-pid
signal covers the common path on unix, and the interval is short.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Callable, Mapping

logger = logging.getLogger("sidecar.parent")

SHELL_PID_ENV = "CHORDIAL_SHELL_PID"
POLL_SECONDS = 2.0


def shell_pid_from_env(env: Mapping[str, str]) -> int | None:
    """the shell's pid if it handed one down, else None (no watch)."""
    raw = env.get(SHELL_PID_ENV)
    if raw is None:
        return None
    try:
        pid = int(raw.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """is there a live process with this pid? a liveness probe, not an
    identity check (see the module docstring on reuse)."""
    if sys.platform.startswith("win"):
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists, just not ours to signal
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:  # pragma: no cover - windows only
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        # no such process (or one we can't even wait on - not the shell,
        # which runs as the same user)
        return False
    try:
        # signalled = the process has exited (a zombie handle still opens)
        return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def parent_changed(initial_ppid: int) -> bool:
    """unix: the process that spawned us is gone when our ppid moves
    (reparenting to 1 / launchd). windows keeps a stale ppid, so this is
    deliberately false there - the pid probe carries the watch."""
    if sys.platform.startswith("win"):
        return False
    return os.getppid() != initial_ppid


async def watch(
    shell_pid: int,
    on_gone: Callable[[], None],
    *,
    alive: Callable[[int], bool] = pid_alive,
    initial_ppid: int | None = None,
    ppid_moved: Callable[[int], bool] = parent_changed,
    interval: float = POLL_SECONDS,
) -> None:
    """poll until the shell is gone, then call on_gone exactly once."""
    while True:
        await asyncio.sleep(interval)
        if not alive(shell_pid):
            logger.info("the shell (pid %s) is gone - following it out",
                        shell_pid)
            on_gone()
            return
        if initial_ppid is not None and ppid_moved(initial_ppid):
            logger.info("our parent changed (was %s) - the shell's kill "
                        "landed on the bootloader; following it out",
                        initial_ppid)
            on_gone()
            return
