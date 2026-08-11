# chordial desktop app 🧸

The Tauri shell for chordial — rooms, cycles, and the plushie council.
Design: [`docs/ROOMS_DESIGN.md`](../docs/ROOMS_DESIGN.md) (authoritative).

- **frontend:** React + TypeScript (Vite), in [`src/`](src/)
- **shell:** Tauri 2 (Rust), in [`src-tauri/`](src-tauri/) — will grow the
  collectors (frontmost bundle id, idle) and the deer/tray windows
- **sidecar:** the Python ambient engine is *not* bundled yet (phase 7);
  during development it runs side by side from the repo root

## dev

Prerequisites: Node, plus the Tauri prerequisites for macOS
(<https://tauri.app/start/prerequisites/> — Xcode CLT and Rust via
`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`).

```bash
npm install
npm run tauri dev
```

`npm run dev` alone runs the Vite frontend in a browser without the Tauri
shell — useful for pure UI work.
