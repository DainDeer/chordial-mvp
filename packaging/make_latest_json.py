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

the artifact is VERIFIED before it ships (sol's 7b round): the manifest's
claims must match the bytes. a stale build (old version) or wrong-arch
bundle would be correctly signed and correctly poisonous - an endless
update prompt, or a binary the machine can't run - so on macOS the
Info.plist version and the main executable's mach-o architecture are
checked against what the manifest is about to promise. mismatch = refusal.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = ROOT / "app" / "src-tauri"
BUNDLE_DIR = TAURI_DIR / "target" / "release" / "bundle"
OUT_DIR = ROOT / "packaging" / "dist" / "updates"

DEFAULT_BASE_URL = "https://api.internetcreature.dev/app/updates"

# each OS ships exactly these updater-artifact shapes; the filter keeps a
# leftover bundle from another platform's build out of this platform's slot
_PLATFORM_EXTS = {
    "darwin": (".app.tar.gz",),
    "windows": (".msi.zip", ".nsis.zip"),
    "linux": (".AppImage.tar.gz",),
}

# mach-o header constants for the arch check (64-bit little-endian header;
# a universal binary opens with the big-endian FAT magic instead)
_MACHO_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC_BYTES = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")
_CPU_TYPES = {"aarch64": 0x0100000C, "x86_64": 0x01000007}


def host_platform_key() -> str:
    """tauri's updater platform key for the machine that built the bundle
    (e.g. darwin-aarch64). cross-builds should pass --platform."""
    triple = subprocess.run(
        ["rustc", "-Vv"], capture_output=True, text=True, check=True)
    host = next(line.split()[1] for line in triple.stdout.splitlines()
                if line.startswith("host:"))
    arch, _, _ = host.partition("-")
    if "apple-darwin" in host:
        return f"darwin-{arch}"
    if "windows" in host:
        return f"windows-{arch}"
    return f"linux-{arch}"


def find_updater_artifact(bundle_dir: Path,
                          platform_key: str) -> tuple[Path, Path]:
    """the updater bundle + its signature for THIS platform key - other
    platforms' leftovers never win on mtime."""
    os_name = platform_key.split("-", 1)[0]
    exts = _PLATFORM_EXTS.get(os_name)
    if exts is None:
        sys.exit(f"unknown platform key {platform_key!r}")
    candidates = [p for ext in exts for p in bundle_dir.rglob(f"*{ext}")]
    if not candidates:
        sys.exit(f"no {' / '.join(exts)} updater artifact under "
                 f"{bundle_dir} - run `npm run tauri build` with "
                 "TAURI_SIGNING_PRIVATE_KEY set (docs/PACKAGING.md)")
    artifact = max(candidates, key=lambda p: p.stat().st_mtime)
    sig = artifact.with_name(artifact.name + ".sig")
    if not sig.exists():
        sys.exit(f"{artifact.name} has no .sig beside it - the build ran "
                 "without the signing key")
    return artifact, sig


def verify_artifact(artifact: Path, version: str, platform_key: str) -> None:
    """refuse to ship bytes that don't match the manifest's claims. macOS
    gets the real check (Info.plist version + mach-o arch of the main
    executable); other platforms are honestly unverified for now."""
    if not platform_key.startswith("darwin"):
        print(f"note: {platform_key} artifact contents are UNVERIFIED - "
              "the version/arch check only knows .app bundles so far")
        return
    arch = platform_key.split("-", 1)[1]
    want_cpu = _CPU_TYPES.get(arch)
    if want_cpu is None:
        sys.exit(f"unknown darwin arch {arch!r}")
    with tarfile.open(artifact, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        plist_name = next(
            (n for n in members if n.endswith(".app/Contents/Info.plist")),
            None)
        if plist_name is None:
            sys.exit(f"{artifact.name} has no Info.plist - not an .app?")
        plist_file = tar.extractfile(members[plist_name])
        assert plist_file is not None
        info = plistlib.load(plist_file)
        built = info.get("CFBundleShortVersionString")
        if built != version:
            sys.exit(
                f"version mismatch: tauri.conf.json says {version} but "
                f"{artifact.name} was built as {built} - a stale bundle. "
                "rebuild before publishing.")
        exe = info.get("CFBundleExecutable")
        exe_name = next(
            (n for n in members
             if n.endswith(f".app/Contents/MacOS/{exe}")), None)
        if exe_name is None:
            sys.exit(f"{artifact.name} is missing its executable {exe!r}")
        exe_file = tar.extractfile(members[exe_name])
        assert exe_file is not None
        head = exe_file.read(8)
        if head[:4] in _FAT_MAGIC_BYTES:
            return  # universal binary carries every arch
        magic = int.from_bytes(head[:4], "little")
        if magic != _MACHO_MAGIC_64:
            sys.exit(f"{exe!r} inside {artifact.name} is not a mach-o "
                     "binary - refusing to ship it")
        cpu = int.from_bytes(head[4:8], "little")
        if cpu != want_cpu:
            got = next((k for k, v in _CPU_TYPES.items() if v == cpu),
                       hex(cpu))
            sys.exit(
                f"arch mismatch: manifest would claim {arch} but "
                f"{artifact.name} contains a {got} binary - the wrong "
                "machine would download an app it cannot run.")


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
    artifact, sig = find_updater_artifact(BUNDLE_DIR, platform_key)
    verify_artifact(artifact, version, platform_key)

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
    print(f"  latest.json -> {version} [{platform_key}] (verified)")
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
