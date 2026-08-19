# operations — running the council for real 🧮

Phase 7c of the rooms overhaul ([`ROOMS_DESIGN.md`](ROOMS_DESIGN.md) §10):
the multi-user *invariants* (tenant scoping, revocable device credentials,
scoped websockets, the sync quota) landed in phases 1–2 — this phase is the
operator's side of running them: metering, budgets, backups, load, and the
distribution gatekeeping. Built in slices; each section below says whether
its piece exists yet.

## the meter (built)

Every model call has been a `usage_log` row since v3 (user, helper, role,
model, four token classes). The meter is the first thing that reads it:
an operator dashboard, and the per-user token budgets §10 promised.

### the dashboard

Set `OPS_TOKEN` (any long random string — `openssl rand -hex 24`) in the
server's env and `/ops` exists; unset, the routes don't (same pattern as
the update feed). Open `https://api.internetcreature.dev/ops`, paste the
token once per tab (sessionStorage only), and you get: per-day, per-user,
per-model, and per-role token aggregates over a 7/14/30-day window, plus
each user's live trailing-24h spend against the budget knobs — the same
arithmetic the gates enforce, so the dashboard can never disagree with
the behavior. Days are UTC (an operator view spanning users has no one
"local"); budgets are trailing-24h.

The page is inert html; the data endpoint (`/api/ops/usage`) is the thing
the bearer token guards, in constant time. The token is an operator
credential — don't reuse a user-facing secret for it.

### the budgets

Two knobs, both **off by default** (`0`) — a single-user rig stays
unmetered and byte-identical:

| knob | past it, what changes |
|---|---|
| `USER_TOKEN_BUDGET_SOFT_24H` | proactive outreach stops (a pulse gate, last in the stack). The user notices nothing except quiet. |
| `USER_TOKEN_BUDGET_HARD_24H` | conversational turns are refused with honest ephemeral copy ("resting its voice"). Clocks, sync, the deer, and every non-model surface keep working. |

Spend is measured over a **trailing 24h window** (the same rolling shape
as `SYNC_DAILY_EVENT_CAP`): no midnight cliff to time a burst against,
and the voice comes back gradually rather than at a bell. Budgeted
tokens = input + output; cache reads/writes are surfaced in the dashboard
but deliberately never counted — a budget that punished cache hits would
punish exactly the architecture that keeps the council affordable.

Honesty notes:

- Every enforcement point **fails open**: a broken meter never silences a
  check-in or a conversation (it logs and allows).
- Background utility work (curator, scorer) is bounded and rare and stays
  exempt; the decider rides the conversational turn it serves, so the
  hard gate already covers it.
- The refusal copy is ephemeral — never persisted, never in the cached
  prefix.
- Sensible shapes: `hard` comfortably above `soft` (soft is the quiet
  lever, hard is the stop). Starting guesses for a real deployment:
  soft 300k, hard 1M — tune off the dashboard's own numbers.

## the vault (existing, pointers)

- **Postgres**: `scripts/pg_backup.sh` is cron-ready (nightly pg_dump,
  custom format, keeps newest 14). Restore rehearsal:
  `createdb restore_test && pg_restore -d restore_test backups/chordial-<ts>.dump`.
- **The updater signing key** (`~/.tauri/chordial-updater.key`) is the
  single unrecoverable secret — see
  [`PACKAGING.md`](PACKAGING.md); the update feed itself is reproducible
  from builds and needs no backup.
- **Sidecar sqlite** lives on each user's device and syncs derived events
  up; it is deliberately not the server's to back up.

## the drill (not built yet)

Load testing against a scratch rig (N simulated users linking devices,
syncing outboxes, holding websockets, taking turns) and the tuning that
falls out of it. Next slice.

## the gate (decision pending)

macOS code-signing + notarization for the boxed app (the 7b deferral:
the .app runs locally unsigned; gatekeeper will refuse it on anyone
else's machine). Needs an Apple Developer Program membership ($99/yr) —
an operator decision, not a code one. Until then distribution is
"build it yourself" per [`PACKAGING.md`](PACKAGING.md).
