# chordial desktop app 🧸

The Tauri shell for chordial — rooms, cycles, and the council.
Design: [`docs/ROOMS_DESIGN.md`](../docs/ROOMS_DESIGN.md) (authoritative).

- **frontend:** React + TypeScript (Vite), in [`src/`](src/)
- **shell:** Tauri 2 (Rust), in [`src-tauri/`](src-tauri/) — will grow the
  collectors (frontmost bundle id, idle) and the deer/tray windows
- **sidecar:** the Python ambient engine is *not* bundled yet (phase 7);
  during development it runs side by side from the repo root

## dev

Prerequisites: **Node `^20.19` or `>=22.12`** (required by the locked Vite
version), plus the Tauri prerequisites for macOS
(<https://tauri.app/start/prerequisites/> — Xcode CLT and Rust via
`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`).

The shell is a client: it needs the chordial server running beside it.
The full dev loop, from the repo root:

```bash
# terminal 1 - the server (a disposable sqlite state; see scripts/dev_db.py)
poetry run python scripts/dev_db.py fresh     # or: seed
DATABASE_URL=sqlite:///chordial_dev.db ENABLE_TELEGRAM=false \
  ENABLE_DISCORD=false poetry run python main.py

# terminal 2 - a one-time device link code (the no-telegram path)
poetry run python scripts/dev_db.py link-code

# terminal 3 - the shell
cd app && npm install && npm run tauri dev
```

Paste the code into the app's link screen and you're in. On a `fresh` db the
first message runs the real front-door introduction (vel greets you); `seed`
skips introductions with a lived-in workspace.

`npm run dev` alone runs the Vite frontend in a browser without the Tauri
shell — useful for pure UI work. `npm test` runs the frontend unit tests
(vitest); `npm run build` typechecks and bundles.

The frontend talks to `http://localhost:8484` by default; point it elsewhere
with `VITE_CHORDIAL_SERVER` in `app/.env.local` (it must also appear in the
server's `APP_ALLOWED_ORIGINS`).

## security posture

- Production CSP is restrictive (`default-src 'self'`). `connect-src`
  permits only IPC and the chordial server (currently the local dev server,
  `http://localhost:8484`; swap for the real server origin at deploy).
  The server allows the app's origins (`tauri://localhost`, the vite dev
  origin) via a scoped CORS allowlist on `/api/v1` only — configured with
  `APP_ALLOWED_ORIGINS` on the server side.
- The device bearer token lives in webview localStorage for dev-mode; phase 7
  packaging graduates it to the OS keychain (stronghold plugin).
- The template's opener plugin was removed (nothing opens external URLs yet).
  If/when the app needs to open links, re-add it with a scoped URL allowlist,
  not `opener:default`.
