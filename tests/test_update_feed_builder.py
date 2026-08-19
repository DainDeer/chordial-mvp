"""the feed builder's refusals (sol's 7b round): the manifest's claims
must match the bytes it points at. a stale build or a wrong-arch bundle
would be correctly signed and correctly poisonous - endless update
prompts, or an app the machine can't run - so the builder verifies the
Info.plist version and the executable's mach-o arch before shipping,
and never lets another platform's leftover win on mtime.
"""
import importlib.util
import io
import plistlib
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SPEC = importlib.util.spec_from_file_location(
    "make_latest_json",
    Path(__file__).resolve().parents[1] / "packaging" / "make_latest_json.py")
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)

ARM64_HEAD = (0xFEEDFACF).to_bytes(4, "little") + \
    (0x0100000C).to_bytes(4, "little")
X86_64_HEAD = (0xFEEDFACF).to_bytes(4, "little") + \
    (0x01000007).to_bytes(4, "little")
FAT_HEAD = b"\xca\xfe\xba\xbe" + b"\x00\x00\x00\x02"


def _fake_app_targz(path: Path, version: str, exe_head: bytes) -> None:
    """a minimal chordial.app.tar.gz: Info.plist + the main executable."""
    info = plistlib.dumps({
        "CFBundleShortVersionString": version,
        "CFBundleExecutable": "chordial-app",
    })
    exe = exe_head + b"\x00" * 24
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (
                ("chordial.app/Contents/Info.plist", info),
                ("chordial.app/Contents/MacOS/chordial-app", exe)):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))


def test_matching_version_and_arch_pass(tmp_path):
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", ARM64_HEAD)
    builder.verify_artifact(artifact, "0.2.0", "darwin-aarch64")


def test_universal_binary_passes_either_arch(tmp_path):
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", FAT_HEAD)
    builder.verify_artifact(artifact, "0.2.0", "darwin-aarch64")
    builder.verify_artifact(artifact, "0.2.0", "darwin-x86_64")


def test_stale_version_is_refused(tmp_path):
    """the endless-prompt trap: manifest says 0.3.0, bytes are 0.2.0 -
    every install would still report 0.2.0 and be offered 0.3.0 forever."""
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", ARM64_HEAD)
    with pytest.raises(SystemExit, match="version mismatch"):
        builder.verify_artifact(artifact, "0.3.0", "darwin-aarch64")


def test_wrong_arch_is_refused(tmp_path):
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", X86_64_HEAD)
    with pytest.raises(SystemExit, match="arch mismatch"):
        builder.verify_artifact(artifact, "0.2.0", "darwin-aarch64")


def test_not_a_macho_is_refused(tmp_path):
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", b"#!/bin/sh")
    with pytest.raises(SystemExit, match="not a mach-o"):
        builder.verify_artifact(artifact, "0.2.0", "darwin-aarch64")


def test_non_darwin_platforms_are_honestly_unverified(tmp_path, capsys):
    artifact = tmp_path / "chordial.msi.zip"
    artifact.write_bytes(b"not inspected")
    builder.verify_artifact(artifact, "0.2.0", "windows-x86_64")
    assert "UNVERIFIED" in capsys.readouterr().out


def test_selection_filters_by_platform_key(tmp_path):
    """a windows leftover with a newer mtime must never win the darwin
    slot (and vice versa)."""
    mac = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(mac, "0.2.0", ARM64_HEAD)
    (tmp_path / "chordial.app.tar.gz.sig").write_text("sig-mac")
    win = tmp_path / "chordial.msi.zip"
    win.write_bytes(b"windows bytes")
    (tmp_path / "chordial.msi.zip.sig").write_text("sig-win")
    import os
    os.utime(win, (9999999999, 9999999999))  # newest by far

    artifact, sig = builder.find_updater_artifact(tmp_path, "darwin-aarch64")
    assert artifact == mac
    assert sig.read_text() == "sig-mac"
    artifact, _ = builder.find_updater_artifact(tmp_path, "windows-x86_64")
    assert artifact == win


def test_missing_signature_is_refused(tmp_path):
    artifact = tmp_path / "chordial.app.tar.gz"
    _fake_app_targz(artifact, "0.2.0", ARM64_HEAD)
    with pytest.raises(SystemExit, match="no .sig"):
        builder.find_updater_artifact(tmp_path, "darwin-aarch64")
