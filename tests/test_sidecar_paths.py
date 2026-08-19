"""where the boxed sidecar keeps its state (phase 7b: the box).

the contract: SIDECAR_DB always wins (the shell sets it when spawning the
bundled binary); a dev run stays in the cwd; a frozen binary run by hand
lands in the platform app-data dir - the SAME directory tauri computes for
the identifier, so a hand-run binary and a shell-spawned one see one world.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sidecar.paths import (  # noqa: E402
    APP_IDENTIFIER, data_dir, default_db_path)

HOME = Path("/home/fern")


def test_explicit_sidecar_db_always_wins():
    env = {"SIDECAR_DB": "/somewhere/else/side.db"}
    assert default_db_path(frozen=False, env=env) == \
        Path("/somewhere/else/side.db")
    assert default_db_path(frozen=True, env=env, platform="darwin",
                           home=HOME) == Path("/somewhere/else/side.db")


def test_dev_run_defaults_to_the_cwd():
    assert default_db_path(frozen=False, env={}) == \
        Path("chordial_sidecar.db")


def test_frozen_run_lands_in_the_app_data_dir():
    got = default_db_path(frozen=True, env={}, platform="darwin", home=HOME)
    assert got == (HOME / "Library" / "Application Support"
                   / APP_IDENTIFIER / "chordial_sidecar.db")


def test_data_dir_matches_tauri_per_platform():
    """tauri's app_data_dir for the same identifier: Application Support
    on macOS, roaming AppData on windows, XDG data home elsewhere."""
    assert data_dir(platform="darwin", env={}, home=HOME) == \
        HOME / "Library" / "Application Support" / APP_IDENTIFIER
    assert data_dir(platform="win32", env={"APPDATA": r"/c/Users/f/Roaming"},
                    home=HOME) == Path(r"/c/Users/f/Roaming") / APP_IDENTIFIER
    assert data_dir(platform="win32", env={}, home=HOME) == \
        HOME / "AppData" / "Roaming" / APP_IDENTIFIER
    assert data_dir(platform="linux", env={"XDG_DATA_HOME": "/data"},
                    home=HOME) == Path("/data") / APP_IDENTIFIER
    assert data_dir(platform="linux", env={}, home=HOME) == \
        HOME / ".local" / "share" / APP_IDENTIFIER


def test_identifier_matches_the_tauri_config():
    """the two halves compute the same directory only while the identifier
    stays in lockstep with tauri.conf.json - pin it."""
    import json
    conf = json.loads((Path(__file__).resolve().parents[1]
                       / "app" / "src-tauri" / "tauri.conf.json").read_text())
    assert conf["identifier"] == APP_IDENTIFIER
