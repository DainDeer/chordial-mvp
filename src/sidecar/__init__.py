"""the sidecar: chordial's on-device half (docs/ROOMS_DESIGN.md section 10).

a separate process from the chordial server - it runs ON the person's
machine, beside the tauri shell, and owns everything that must be instant
and offline: the focus-block clock, the deer's presence, and (phase 4b-ii)
the activity collectors' consumer. it keeps its own small sqlite state and
speaks to the outside world in exactly two directions:

- DOWN to the shell: a loopback-only aiohttp server (state, session
  start/stop, a websocket the deer window listens on)
- UP to the chordial server: the outbox pump, delivering derived events
  (focus_block.completed, ...) through the sync contract with at-least-once
  retries against the server's exactly-once apply

it deliberately holds NO llm keys and makes no model calls - its words are
authored lines (src/sidecar/lines.py). generated ambient language arrives in
a later phase as a server-batched pool, never as a key in this process.
"""
