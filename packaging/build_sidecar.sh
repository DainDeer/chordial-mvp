#!/usr/bin/env bash
# freeze the sidecar (phase 7b: the box) and put the binary where tauri's
# externalBin expects it: app/src-tauri/binaries/chordial-sidecar-<triple>.
#
#   bash packaging/build_sidecar.sh          # host triple via rustc
#   bash packaging/build_sidecar.sh <triple> # cross-name explicitly
#
# run it once before `tauri dev` too - the tauri cli wants the file to
# exist even though debug builds never spawn it (the dev loop keeps the
# sidecar in its own terminal: poetry run python -m src.sidecar).
set -euo pipefail
cd "$(dirname "$0")/.."

TRIPLE="${1:-$(rustc -Vv | awk '/^host:/ {print $2}')}"

poetry run pyinstaller --noconfirm --clean \
  --distpath packaging/dist --workpath packaging/build \
  packaging/sidecar.spec

SRC="packaging/dist/chordial-sidecar"
DEST="app/src-tauri/binaries/chordial-sidecar-${TRIPLE}"
if [ -f "${SRC}.exe" ]; then
  SRC="${SRC}.exe"
  DEST="${DEST}.exe"
fi

mkdir -p app/src-tauri/binaries
cp "$SRC" "$DEST"
echo "sidecar boxed: $DEST"
