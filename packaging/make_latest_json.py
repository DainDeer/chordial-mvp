#!/usr/bin/env python3
"""assemble the update feed (phase 7b: the box).

after `npm run tauri build` (with the signing key in the env), this
gathers the updater artifacts - the .app.tar.gz bundle and its minisign
.sig - into one deployable directory with the latest.json manifest the
tauri updater polls:

    python packaging/make_latest_json.py [--notes "..."] [--base-url URL]

output lands in packaging/dist/updates/ - rsync that directory to the
server's APP_UPDATES_DIR and the feed is live. no framework: the manifest
is four fields and a platforms map, and this script is the documentation.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = ROOT / "app" / "src-tauri"
BUNDLE_DIR = TAURI_DIR / "target" / "release" / "bundle"
OUT_DIR = ROOT / "packaging" / "dist" / "updates"

DEFAULT_BASE_URL = "https://api.internetcreature.dev/app/updates"


def host_platform_key() -> str:
    """tauri's updater platform key for the machine that built the bundle
    (e.g. darwin-aarch64). cross-builds should pass --platform."""
    triple = subprocess.run(
        ["rustc", "-Vv"], capture_output=True, text=True, check=True)
    host = next(line.split()[1] for line in triple.stdout.splitlines()
                if line.startswith("host:"))
    arch, _, os_part = host.partition("-")
    if "apple-darwin" in host:
        return f"darwin-{arch}"
    if "windows" in host:
        return f"windows-{arch}"
    return f"linux-{arch}"


def find_updater_artifact() -> tuple[Path, Path]:
    """the updater bundle + its signature, wherever this platform's
    bundler put them."""
    candidates = (list(BUNDLE_DIR.rglob("*.app.tar.gz"))
                  + list(BUNDLE_DIR.rglob("*.msi.zip"))
                  + list(BUNDLE_DIR.rglob("*.nsis.zip"))
                  + list(BUNDLE_DIR.rglob("*.AppImage.tar.gz")))
    if not candidates:
        sys.exit("no updater artifact under "
                 f"{BUNDLE_DIR} - run `npm run tauri build` with "
                 "TAURI_SIGNING_PRIVATE_KEY set (docs/PACKAGING.md)")
    artifact = max(candidates, key=lambda p: p.stat().st_mtime)
    sig = artifact.with_name(artifact.name + ".sig")
    if not sig.exists():
        sys.exit(f"{artifact.name} has no .sig beside it - the build ran "
                 "without the signing key")
    return artifact, sig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", default="", help="release notes line")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="public URL the feed is served from")
    parser.add_argument("--platform", default=None,
                        help="updater platform key (default: this host)")
    args = parser.parse_args()

    version = json.loads(
        (TAURI_DIR / "tauri.conf.json").read_text())["version"]
    platform_key = args.platform or host_platform_key()
    artifact, sig = find_updater_artifact()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # version the filename so an old manifest never points at new bytes
    shipped = OUT_DIR / f"chordial_{version}_{platform_key}{_ext(artifact)}"
    shutil.copyfile(artifact, shipped)

    manifest_path = OUT_DIR / "latest.json"
    manifest = (json.loads(manifest_path.read_text())
                if manifest_path.exists() else {})
    if manifest.get("version") != version:
        manifest = {"version": version, "platforms": {}}
    manifest["notes"] = args.notes or manifest.get("notes", "")
    manifest["pub_date"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    manifest["platforms"][platform_key] = {
        "signature": sig.read_text().strip(),
        "url": f"{args.base_url}/{shipped.name}",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"feed ready: {OUT_DIR}")
    print(f"  {shipped.name}  ({shipped.stat().st_size // 1_048_576} MB)")
    print(f"  latest.json -> {version} [{platform_key}]")
    print("deploy: rsync -av packaging/dist/updates/ "
          "<vps>:$APP_UPDATES_DIR/")


def _ext(path: Path) -> str:
    name = path.name
    for ext in (".app.tar.gz", ".msi.zip", ".nsis.zip", ".AppImage.tar.gz"):
        if name.endswith(ext):
            return ext
    return path.suffix


if __name__ == "__main__":
    main()
