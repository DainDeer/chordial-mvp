# packaging — the box 📦

Phase 7b of the rooms overhaul ([`ROOMS_DESIGN.md`](ROOMS_DESIGN.md) §10 as-built):
one installable chordial desktop app — tauri shell, react frontend, and the
python sidecar frozen inside it — plus a signed self-update feed served by
the chordial server itself.

## what ships, and what never does

The box carries the **shell** (Rust), the **frontend** (built by vite), and
the **sidecar** (a PyInstaller onefile binary, ~10 MB, aiohttp + stdlib).
The sidecar's import surface is deliberately tiny and must stay that way:
no `config.py`, no LLM providers, no database layer, **no LLM keys on the
device, ever** — the spec ([`packaging/sidecar.spec`](../packaging/sidecar.spec))
uses no hiddenimports, so PyInstaller's own analysis is the honest
inventory of what goes in.

## one-time setup

- Tauri prerequisites + Node (see [`app/README.md`](../app/README.md)) and
  Python ≥3.10 + Poetry at the repo root (`poetry install` — pyinstaller is
  a dev dependency).
- The **updater signing keypair** lives OUTSIDE the repo at
  `~/.tauri/chordial-updater.key` (private) / `.key.pub` (public, pinned in
  `tauri.conf.json`). Generated once with
  `npm run tauri signer generate -- -w ~/.tauri/chordial-updater.key`.
  **Back the private key up somewhere safe.** Lose it and shipped installs
  can never accept another update (the pubkey pinned in every existing app
  will refuse anything you sign with a new key); leak it and anyone can
  sign an "update". It never goes in the repo.

## building a release

```bash
# 1. freeze the sidecar into app/src-tauri/binaries/<triple>
bash packaging/build_sidecar.sh

# 2. build the app with updater artifacts signed
cd app
export TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/chordial-updater.key)"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""
npm run tauri build
```

Outputs land under `app/src-tauri/target/release/bundle/`: the `.app` and
`.dmg` for installing by hand, and the updater pair — `.app.tar.gz` +
`.sig` — for the feed. (`tauri dev` also wants the sidecar binary from
step 1 to exist, even though debug builds never spawn it.)

Building headless (agent sessions, CI)? The `.dmg` step scripts Finder and
fails without a GUI session — `npm run tauri build -- --bundles app` skips
it and still produces the `.app` + signed updater pair, which is all the
feed needs.

To ship a new version: bump `version` in `app/src-tauri/tauri.conf.json`
(keep `package.json` and `Cargo.toml` in step), rebuild, publish.

## publishing to the update feed

The feed is a directory of signed bundles plus `latest.json`, served
read-only by the chordial server at `/app/updates/` when `APP_UPDATES_DIR`
is set — same origin the app already talks to
(`https://api.internetcreature.dev`), no third-party host.

```bash
# assemble packaging/dist/updates/ (bundle + sig + latest.json)
python packaging/make_latest_json.py --notes "what changed"

# deploy: copy it to the VPS directory APP_UPDATES_DIR points at
rsync -av packaging/dist/updates/ <vps>:/srv/chordial/app-updates/
```

Installed apps check the feed quietly ~15s after launch (silent when
current or unreachable) and on the tray's "check for updates". Both ask
before installing; the bundle signature is verified against the pinned
pubkey before anything is applied — a compromised feed can withhold
updates but never forge one.

## how the pieces run, packaged vs dev

|                       | dev (`tauri dev`, debug)                 | packaged (release)                                  |
| --------------------- | ---------------------------------------- | --------------------------------------------------- |
| sidecar               | your terminal: `poetry run python -m src.sidecar` | spawned + supervised by the shell (respawn w/ backoff, killed on quit) |
| sidecar state         | `chordial_sidecar.db` in the cwd         | `~/Library/Application Support/app.chordial.desktop/` (+ `sidecar.log`) |
| device token at rest  | webview localStorage                     | OS keychain (macOS Keychain / Windows Credential Manager) |
| updater               | off                                      | on (launch check + tray item)                       |

Details that keep this honest:

- **Spawn is adopt-first, and adoption is watched**: the shell probes
  `127.0.0.1:8485/v1/state` before spawning; a sidecar already up (second
  app launch, a dev terminal's) is adopted, never fought over the port —
  and a 15s watchdog takes over if that foreign process later vanishes.
  The onefile binary takes ~6–8s from spawn to the port binding
  (self-extraction) — the deer window's reconnect backoff already covers
  it.
- **Keychain graduation is one-way**: on first packaged run, a token from
  the localStorage era moves into the keychain and the plaintext copy is
  removed. A keychain that refuses degrades to localStorage with a console
  warning rather than bricking linking — and can never resurrect a revoked
  token: a localStorage token beats the keychain value on load (it only
  exists when the keychain missed a newer write), and a refused clear
  leaves a pending marker so the stuck value is never served. The deer's
  cross-window "token changed" signal rides a bare rev counter in
  localStorage — no secret in the event.
- **Update checks are single-flight, and the feed builder verifies the
  bytes**: `make_latest_json.py` refuses a bundle whose Info.plist version
  or executable architecture doesn't match what the manifest would claim —
  a stale-but-signed artifact means endless update prompts, a wrong-arch
  one means an app the machine can't run.
- **Windows is untested**: the collector is macOS-only (cfg-gated), the
  sidecar spec sets `console=False` for it, and the keyring/updater legs
  compile for it — but nobody has run the boxed app there yet.
