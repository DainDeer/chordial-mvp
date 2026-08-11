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

```bash
npm install
npm run tauri dev
```

`npm run dev` alone runs the Vite frontend in a browser without the Tauri
shell — useful for pure UI work.

## security posture

- Production CSP is restrictive (`default-src 'self'`). `connect-src`
  permits only IPC and the chordial server (currently the local dev server,
  `http://localhost:8484`; swap for the real server origin at deploy).
  The server allows the app's origins (`tauri://localhost`, the vite dev
  origin) via a scoped CORS allowlist on `/api/v1` only — configured with
  `APP_ALLOWED_ORIGINS` on the server side.
- The template's opener plugin was removed (nothing opens external URLs yet).
  If/when the app needs to open links, re-add it with a scoped URL allowlist,
  not `opener:default`.
