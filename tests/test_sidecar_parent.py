"""the parent watch (src/sidecar/parent.py): the sidecar follows the shell
out. pins the first-boxed-launch orphan: the shell's kill reaches only
pyinstaller's bootloader, so the interpreter must notice the shell's
absence itself."""

import asyncio
import os
import subprocess
import sys

import pytest

from src.sidecar import parent


# --- the env contract ---------------------------------------------------

def test_no_pid_in_env_means_no_watch():
    assert parent.shell_pid_from_env({}) is None


@pytest.mark.parametrize("raw", ["", "abc", "-1", "0", " "])
def test_garbage_pid_means_no_watch(raw):
    assert parent.shell_pid_from_env({parent.SHELL_PID_ENV: raw}) is None


def test_valid_pid_is_read():
    assert parent.shell_pid_from_env({parent.SHELL_PID_ENV: " 4242 "}) == 4242


# --- liveness -------------------------------------------------------------

def test_our_own_pid_is_alive():
    assert parent.pid_alive(os.getpid()) is True


def test_an_exited_process_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # reaped: the pid is free (and on unix no longer a zombie)
    assert parent.pid_alive(proc.pid) is False


def test_parent_changed_is_false_while_our_parent_lives():
    assert parent.parent_changed(os.getppid()) is False


# --- the watch ------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_watch_follows_the_shell_out_exactly_once():
    alive_answers = iter([True, True, False, False])
    gone = []
    _run(parent.watch(
        999_999, lambda: gone.append(1),
        alive=lambda pid: next(alive_answers),
        interval=0))
    assert gone == [1]


def test_watch_keeps_waiting_while_the_shell_lives():
    async def scenario():
        gone = []
        task = asyncio.ensure_future(parent.watch(
            999_999, lambda: gone.append(1),
            alive=lambda pid: True,
            initial_ppid=12345,
            ppid_moved=lambda initial: False,
            interval=0))
        await asyncio.sleep(0.05)
        assert gone == [], "shell alive, ppid unchanged - must not fire"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _run(scenario())


def test_watch_fires_when_the_bootloader_dies_under_a_live_shell_pid():
    # the unix second signal: the shell's SIGKILL landed on the bootloader
    # (our parent moved) even though something still answers to the
    # shell's pid number
    gone = []
    _run(parent.watch(
        999_999, lambda: gone.append(1),
        alive=lambda pid: True,
        initial_ppid=12345,
        ppid_moved=lambda initial: True,
        interval=0))
    assert gone == [1]


def test_watch_checks_the_pid_it_was_given():
    seen = []

    def alive(pid):
        seen.append(pid)
        return False

    _run(parent.watch(777, lambda: None, alive=alive, interval=0))
    assert seen == [777]


# --- the shell can see who a sidecar follows -----------------------------------

def test_state_reports_the_shell_pid(tmp_path):
    # a new shell probing the port reads shell_pid to tell a departing
    # sidecar (its shell gone) from one worth adopting; a dev-terminal
    # sidecar reports null
    from aiohttp.test_utils import TestClient, TestServer
    from src.sidecar.server import SidecarService
    from src.sidecar.store import SidecarStore

    async def flow():
        for pid in (None, 4242):
            store = SidecarStore(tmp_path / f"s{pid}.db")
            service = SidecarService(store, shell_pid=pid)
            client = TestClient(TestServer(service.build_app()))
            await client.start_server()
            try:
                state = await (await client.get("/v1/state")).json()
                assert state["shell_pid"] == pid
            finally:
                await client.close()
                store.close()
    _run(flow())
