# -*- mode: python ; coding: utf-8 -*-
# the sidecar, boxed (phase 7b): one self-contained binary for the tauri
# shell's externalBin. built by packaging/build_sidecar.sh, which renames
# the output to chordial-sidecar-<target-triple> where tauri expects it.
#
# the import surface is deliberately tiny - src/sidecar/* plus aiohttp -
# and must STAY tiny: this binary ships to the person's machine, so the
# server's world (config.py, the llm providers, the database layer) has no
# business inside it. no hiddenimports means pyinstaller's own analysis is
# the honest inventory of what the box carries.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(ROOT, 'src', 'sidecar', '__main__.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='chordial-sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # windows: no console window pops behind the deer. NOTE the onefile
    # binary self-extracts before python even starts - measured ~6-8s from
    # spawn to the port binding on an m-series mac. everything that talks
    # to the sidecar already tolerates that (the deer window reconnects
    # with backoff; the shell probes before spawning, never after).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
