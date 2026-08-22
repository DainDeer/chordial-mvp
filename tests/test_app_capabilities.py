"""the shell's capability file, pinned (app/src-tauri/capabilities/default.json).

tauri's permission system refuses silently: a `data-tauri-drag-region`
without `core:window:allow-start-dragging` is simply a div that does
nothing - which is exactly how the deer window shipped in the first boxed
build (undecorated, always-on-top, and immovable over the link field).
`core:default` does NOT include that permission, so it must be listed by
hand, and this test keeps it listed. same for the window-state plugin's
permission: without it the remembered-position feature is inert.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = ROOT / "app" / "src-tauri" / "capabilities" / "default.json"


def _capability() -> dict:
    return json.loads(CAPABILITY.read_text())


def test_deer_window_is_draggable():
    cap = _capability()
    assert "deer" in cap["windows"]
    assert "core:window:allow-start-dragging" in cap["permissions"], (
        "the deer is undecorated - without start-dragging she can't be moved"
    )


def test_window_positions_are_remembered():
    cap = _capability()
    assert "main" in cap["windows"] and "deer" in cap["windows"]
    assert "window-state:default" in cap["permissions"]


def test_capability_covers_both_windows_only():
    # the capability is scoped to exactly the two windows the shell opens;
    # a new window label must opt in deliberately
    assert sorted(_capability()["windows"]) == ["deer", "main"]
