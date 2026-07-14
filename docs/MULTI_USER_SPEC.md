# Multi-User Scaling Spec

*Drafted 2026-07-11. What it takes to move chordial from "Dain's single-tenant
companion" to a product that N strangers can sign up for — with the central
design decision analyzed: keep Notion as the task backend, go bespoke, or
hybrid.*

---

## 1. Where the app already is multi-user (more than you'd think)

The v2/v3 refactors accidentally did most of the multi-tenant groundwork:

- **Every domain table is keyed by `user_uuid`** — event log, memories,
  helper states, agenda snapshots, usage logs, agent traces. One `EventLog`
  per user, per-user scheduler delivery, per-user prompt-cache zones.
- **Platform identity is a real join table** (`PlatformIdentity`, unique on
  `(platform, platform_user_id)`) with a chat-first linking flow (single-use
  deep-link codes). Unknown senders never reach the AI — no drive-by API spend.
- **Cost attribution exists**: `UsageLog`/`AgentTrace` carry `user_uuid` and
  `helper_id`. The raw material for quotas and billing is already recorded.
- **Telegram bots are shared, not per-user**: one bot per helper serves all
  users. Telegram bots scale to arbitrarily many chats; nothing per-user needs
  provisioning on the platform side.
- **Provider layer already has concurrency guards** (`MAX_CONCURRENT_AI_CALLS`
  semaphore, iteration caps) — per-process knobs that survive multi-user as-is.

## 2. The single-tenant hardpoints (complete inventory)

| # | Hardpoint | Where | Severity |
|---|-----------|-------|----------|
| 1 | One Notion internal-integration token for the whole app | `Config.NOTION_API_KEY` | **Blocker** — every user would read/write Dain's workspace |
| 2 | Hardcoded database IDs (dainframe Tasks/Projects/Cycles) | `Config.NOTION_*_DB_ID`, `src/services/notion/schema.py` | **Blocker** |
| 3 | Schema encoded as exact property names + option vocabularies | `schema.py` ("the dainframe schema, encoded once") | **Blocker** for any user-owned workspace |
| 4 | One global Telegram group chat | `Config.TELEGRAM_GROUP_CHAT_ID` (env var; `/setup_group` just prints the id) | Blocker for v3 group-chat mode — needs a per-user `group_chat_id` column + `/setup_group` writing to DB |
| 5 | SQLite (WAL) single file | `DATABASE_URL` default | Fine to ~50 casual users; a real deployment wants Postgres (see §5) |
| 6 | Single asyncio process runs everything | `main.py` (interfaces + scheduler + orchestrator) | Fine to low thousands of users; the ceiling is AI-call throughput, not the process |
| 7 | No per-user cost limits | config has global semaphore only | **Launch blocker** for strangers — one chatty user = unbounded spend |
| 8 | No real account/auth beyond platform identity | — | Fine for chat-only; becomes a gap the moment there's a web UI or billing |
| 9 | Onboarding assumes a known human | intro flow exists, but no waitlist/invite gate, no ToS/privacy surface | Launch blocker for public signup |
| 10 | Pinned Notion API version `2022-06-28` | `Config.NOTION_API_VERSION` | Notion's 2025 "data sources" API redesign eventually forces an upgrade; any multi-user Notion build should start on the new version |

Items 1–3 are the Notion question. Everything else is independent of it and
has to happen regardless — that's §5–7.

---

## 3. The central decision: Notion vs bespoke vs hybrid

### Option A — Stay on Notion, make it multi-tenant

Each user connects their own Notion workspace; chordial provisions the
databases there and operates on them.

**What it takes:**

1. **Public OAuth integration.** Convert from internal token to Notion's
   OAuth2 flow: hosted redirect endpoint (this alone drags in a small web
   service + HTTPS + a domain), code→token exchange, encrypted per-user
   `access_token` storage, re-auth flow for revoked tokens. Notion tokens
   don't expire but users can disconnect at any time — every Notion call needs
   a "token dead → prompt re-link" path.
2. **Template duplication for provisioning.** Notion's OAuth flow supports
   designating a public template page that gets duplicated into the user's
   workspace during authorization (`duplicated_template_id` comes back in the
   token response). Build the chordial template (Tasks/Projects/Cycles — or
   the v3 Plans/Goals/Wins/Check-ins shape), then post-auth, walk the
   duplicated page to discover the child database IDs and store them per user
   (`notion_connections` table: token, workspace id, db ids, property-id map).
3. **Schema robustness rewrite.** `schema.py`'s exact-name matching is safe
   only because Dain doesn't rename his own properties. Strangers *will*
   rename "Status", delete options, add required properties. Mitigations:
   - store **property IDs** (stable across renames) captured at provision
     time, not names;
   - a startup/periodic **schema validation pass** per user with a
     "your board changed, here's what broke" nudge in chat;
   - tolerate missing optional properties everywhere.
   This converts `schema.py` from a constants file into a per-user schema-map
   layer — a real rewrite of all 512 lines of `notion_tools.py`'s assumptions.
4. **Per-user clients + rate limiting.** Rate limits (~3 req/s average) apply
   per token, so per-user workspaces don't contend with each other — good.
   But `snapshot_service.refresh` (3 queries/user) needs a per-user client
   pool and a jittered background schedule instead of the current global
   client singleton.
5. **Migration story you don't control** — the sleeper cost. The v3 Notion
   redesign (Projects→Plans) is planned as an interactive one-time script for
   *one* workspace. With N user-owned workspaces, every future schema change
   becomes a fleet migration executed over a rate-limited third-party API
   against boards users may have hand-edited. Chordial can never again change
   its task model cheaply.

**Pros:** Notion's UI, views, mobile apps, and sharing come free — zero UI
engineering. Power users keep their data in a tool they love. The agenda
digest / completion reconciler / tools all conceptually survive.

**Cons:** Onboarding requires a Notion account + OAuth + template dance (a
huge funnel filter — most people who want an AI companion do not use Notion).
User-editable schema is a permanent fragility tax. Every task read/write is a
third-party network call. Domain evolution is hostage to fleet migrations.
And chordial's product direction (v3 Wins ledger, check-ins, cycle balancing,
per-helper task attribution) keeps outgrowing what's comfortable to model in
user-owned Notion databases.

**Effort:** ~3–5 weeks: OAuth service + token lifecycle (1w), template +
provisioning + connection table (1w), schema-map rewrite of
schema.py/notion_tools/snapshot/reconciler (1–2w), validation/self-heal UX
(1w). Plus the permanent maintenance tax.

### Option B — Bespoke: native task tracking in chordial's own DB

Tasks/Plans/Cycles become first-class tables next to memories and events.

**What it takes:**

1. **Domain tables.** The schema is already fully specified — `schema.py`'s
   vocabularies and V3_DESIGN's Plans/Goals/Tasks/Wins/Check-ins section *are*
   the data model. Alembic migration, SQLAlchemy models, a `TaskManager` in
   the existing managers pattern. This is the easy part (~3–5 days), and it
   implements the Notion-v3 redesign natively instead of building it in
   Notion first.
2. **Tool rewrite (simpler than what exists).** `list/create/update` tools
   against local tables are plain queries — no property-JSON builders, no
   title→id resolution round-trips, no `invalidate_all()` cache dance. The
   agenda snapshot service collapses from a cached-Notion-view into a live
   query (the whole staleness machinery deletes). Completion reconciler
   queries get faster and simpler.
3. **The real cost: surface area users can *see*.** Chat-only task management
   genuinely works for capture and nudging (that's chordial's whole thesis)
   but users need at least one glanceable view. Tiers, cheapest first:
   - **Tier 0 — chat views**: formatted agenda/board replies on demand.
     Already effectively exists via the digest. Free.
   - **Tier 1 — read-only web dashboard**: FastAPI + server-rendered or tiny
     React page: today view, plan list, wins ledger. Auth via magic-link or
     Telegram login widget. ~1–2 weeks.
   - **Tier 2 — editable web UI**: drag/reorder, inline edits, plan builder.
     This is a product in itself; 4+ weeks and ongoing. **Defer.**
4. **Calendar integration** (independent of the Notion question — Notion
   never gave this either): Google Calendar OAuth for busy-awareness and
   due-date sync. Well-trodden (google-auth + calendar v3 API, per-user
   refresh tokens), ~1–2 weeks for read-only free/busy + event creation
   behind a tool. Worth doing in either option; listed here because bespoke
   makes chordial the system of record that syncs *out*.

**Pros:** zero third-party fragility, no OAuth-to-Notion funnel filter,
microsecond reads (agenda always fresh), schema evolution is a normal alembic
migration, per-helper/task attribution and Wins/Check-ins land exactly as
designed. Data locality also makes future features (streaks, analytics,
proactive triggers on task state) trivial.

**Cons:** you own the UI problem forever; users with existing Notion setups
can't see chordial tasks in Notion; "export my data" needs building (CSV/JSON
export is cheap insurance and should ship early).

**Effort to parity with today's Notion feature set:** ~2–3 weeks (tables +
tools + snapshot/reconciler rework + chat views), plus 1–2 weeks for the
Tier-1 dashboard. Notably **less** than Option A's 3–5 weeks, with a *lower*
maintenance tax rather than a higher one.

### Option C — Hybrid (recommended): native source of truth + optional integrations

Chordial's DB is the system of record (Option B). Notion becomes an
*optional, per-user integration* — as does Google Calendar. Concretely:

- Introduce a **`TaskStore` seam**: the tool handlers, snapshot service, and
  reconciler talk to a protocol; `NativeTaskStore` is the default;
  `NotionTaskStore` (today's code, lightly wrapped) stays alive for Dain's
  dainframe via a per-user backend flag. Nothing breaks on day one.
- New users get native storage — no Notion account required, onboarding stays
  pure chat.
- **Later, if demand proves it**: a one-way "mirror to Notion" sync (chordial
  → user's workspace via OAuth + template) for power users. One-way sync
  avoids nearly all of Option A's schema-drift pain: if the user mangles the
  mirror, re-provision it; truth is never lost.

This matches the codebase's existing philosophy (platform as provenance,
tools behind a registry, providers behind protocols) and matches the v3
roadmap: Notion-v3's Plans/Goals/Wins design gets built *natively* instead of
as a second Notion schema you'd then have to fleet-migrate.

### Decision table

| | A: Notion multi-tenant | B: Bespoke | C: Hybrid |
|---|---|---|---|
| Onboarding friction | High (Notion acct + OAuth + template) | None beyond chat | None; Notion opt-in |
| Engineering to launch | 3–5 wks | 2–3 wks (+1–2 dashboard) | 2–4 wks (seam adds a few days) |
| Ongoing fragility | High (user-owned schema, API drift) | Low | Low (mirror is disposable) |
| UI cost | Free (Notion) | Owned (tiered) | Owned, deferred |
| Schema evolution | Fleet migrations over 3rd-party API | Alembic | Alembic |
| Dain's dainframe | Keeps working | Needs migration | Keeps working via flag |
| Addressable users | Notion users only | Everyone | Everyone |

---

## 4. Identity, auth, and account lifecycle

- **Chat-only remains the primary identity**: `PlatformIdentity` is the login.
  No change needed for pure-chat users.
- **Web dashboard auth**: Telegram Login Widget (verifies against the bot
  token, maps straight onto `PlatformIdentity`) or email magic link. No
  passwords, ever.
- **Signup gating**: invite codes reusing the existing link-code machinery
  (single-use codes, `/start <code>`), plus a waitlist table. Gate *before*
  user creation so unknown senders keep costing zero.
- **Account lifecycle**: `delete my data` command (event log, memories, tasks,
  usage — cascade delete + confirmation), data export (JSON dump per user),
  ToS/privacy acknowledgment recorded at onboarding. These are table-stakes
  before strangers arrive; each is small.

## 5. Infrastructure: what actually breaks at each order of magnitude

**~10 users (friends & family):** nothing breaks. SQLite WAL + the current
single process is fine. Do items 4 and 7 from §2 (per-user group id, per-user
spend caps) and deploy somewhere supervised (systemd/fly.io/railway) with
`litestream` or cron backups of the .db.

**~100 users:** move to **Postgres** (`DATABASE_URL` already the only switch;
the WAL pragma hook is already sqlite-conditional; alembic chain must be
tested against pg — JSON columns and datetime defaults are the usual
suspects, ~2–4 days including a data-migration script). Telegram long-polling
still fine (one poller per helper bot regardless of user count). The
scheduler's per-user tick loop is O(N) trivial queries — fine. The binding
constraint becomes **AI spend**: with prompt caching, a moderately active
user runs a few dollars/month; 100 users ≈ real money. Per-user
daily/monthly token budgets enforced from `UsageLog` (deny proactive sends
first, then degrade to shorter context, then hard-cap) are the critical
control — build once at ~day scale granularity, ~2–3 days.

**~1,000 users:** single process still plausible (it's IO-bound), but split
for operability: (a) **web/OAuth service** (dashboard, calendar/Notion
callbacks), (b) **chat workers** — shard users across processes by
`user_uuid` hash; Telegram switches from polling to **webhooks** behind the
web service so any worker can receive any update and route by shard; (c)
**scheduler/curation worker** with a proper job queue (Postgres
`SELECT ... FOR UPDATE SKIP LOCKED` is plenty; no Redis needed yet).
`MAX_CONCURRENT_AI_CALLS` becomes per-worker; watch Anthropic org-level rate
limits and request a tier bump. Add real observability: structured logs,
Sentry, a cost dashboard off `UsageLog`.

**Beyond that:** the architecture (stateless workers + Postgres + queue)
scales horizontally; the problems become product/economics (billing —
Stripe + plan tiers on top of the quota system — and moderation/abuse), not
architecture.

## 6. Cost model sketch

Dominant cost is the chat model. Per active user per day, rough order:
a handful of exchanges + proactive touches ≈ 20–60k input tokens (heavily
cache-discounted — the frozen persona/tool zones are shared-prefix across
turns) + 2–5k output. Utility passes (curator, reconciler, director) are
haiku-priced noise. Ballpark **$1–5/user/month for a typical active user**,
long-tail power users 5–10×. This is why per-user budgets (§5) precede any
public signup, and why the eventual price point needs to clear ~$10/user/mo.
The ProactivityGate already bounds the worst *unprompted* spend — the caps
just need to stay config, per-user-overridable.

## 7. Phased plan

**Phase 0 — harden for >1 user (~1 wk):** Postgres migration + backups;
per-user `telegram_group_chat_id` (finish `/setup_group`); per-user spend
quotas from `UsageLog`; invite-code gate; deploy under supervision with
secrets management.

**Phase 1 — native task core (~2–3 wks):** `TaskStore` protocol; native
Plans/Goals/Tasks/Wins/Check-ins tables (this *is* the Notion-v3 build, done
natively); tools/snapshot/reconciler against the seam; Notion adapter behind
per-user flag (Dain unaffected); data export; delete-my-data.

**Phase 2 — self-serve onboarding (~1 wk):** the v3 storytelling intro is
already the product here; add ToS/privacy beat, invite redemption, per-user
group creation instructions, remove all remaining env-var-per-user knobs.

**Phase 3 — visibility (~2 wks):** read-only web dashboard (today view /
plans / wins) with Telegram-widget auth; Google Calendar OAuth (free/busy
into briefings, due dates out).

**Phase 4 — optional & demand-driven:** one-way Notion mirror via public
OAuth + template duplication; editable web UI; billing.

Total to "strangers can use it, with a screen to look at": **~6–8 weeks** of
focused work, none of it blocked on Notion's review or API timelines.

---

## 8. Recommendation

**Option C.** Make chordial's own database the system of record and demote
Notion to an optional per-user mirror. The clinchers:

1. Bespoke-to-parity is *cheaper to build* than Notion-multi-tenant
   (2–3 weeks vs 3–5), and far cheaper to own — no user-editable schema, no
   fleet migrations over a rate-limited third-party API.
2. Requiring Notion filters out most of the addressable audience for an AI
   companion; chat-first onboarding is chordial's identity.
3. The v3 roadmap already demands a task-schema redesign (Plans/Goals/Wins).
   Building it natively means designing it once in alembic instead of twice —
   once in Notion now and again in whatever schema evolution comes next.
4. The `TaskStore` seam preserves the dainframe and all existing Notion code,
   so nothing is thrown away and the decision stays reversible.
