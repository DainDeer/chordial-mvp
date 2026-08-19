"""where the boxed sidecar keeps its state (phase 7b: the box).

run from the repo (dev), the sidecar is a good unix citizen: SIDECAR_DB
wins, else a file in the cwd. frozen into the app bundle (pyinstaller),
the cwd is wherever the shell spawned us - often somewhere read-only - so
state moves to the platform's per-app data directory: the SAME directory
the tauri shell computes for its identifier, because the shell passes
SIDECAR_DB explicitly when it spawns the binary and these fallbacks only
exist to keep a hand-run binary honest. if the two ever computed different
directories, a hand-run binary would see a different world than the
spawned one - that's why APP_IDENTIFIER matches tauri.conf.json exactly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

# must match `identifier` in app/src-tauri/tauri.conf.json - tauri derives
# its app_data_dir from it, and the shell hands us paths under that dir
APP_IDENTIFIER = "app.chordial.desktop"

DB_FILENAME = "chordial_sidecar.db"


def is_frozen() -> bool:
    """True inside a pyinstaller bundle (it sets sys.frozen)."""
    return bool(getattr(sys, "frozen", False))


def data_dir(platform: Optional[str] = None,
             env: Optional[Mapping[str, str]] = None,
             home: Optional[Path] = None) -> Path:
    """the platform's per-app data directory, matching what tauri computes
    for the same identifier (macOS Application Support, windows roaming
    AppData, XDG data home elsewhere)."""
    platform = platform if platform is not None else sys.platform
    env = os.environ if env is None else env
    home = home if home is not None else Path.home()
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    elif platform.startswith("win"):
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else home / ".local" / "share"
    return base / APP_IDENTIFIER


def default_db_path(frozen: Optional[bool] = None,
                    env: Optional[Mapping[str, str]] = None,
                    platform: Optional[str] = None,
                    home: Optional[Path] = None) -> Path:
    """where the sidecar's sqlite lives: SIDECAR_DB always wins (the shell
    sets it when it spawns the bundled binary); a dev run defaults to the
    cwd; a frozen binary run by hand defaults to the app-data dir."""
    env = os.environ if env is None else env
    explicit = env.get("SIDECAR_DB")
    if explicit:
        return Path(explicit)
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return Path(DB_FILENAME)
    return data_dir(platform=platform, env=env, home=home) / DB_FILENAME
