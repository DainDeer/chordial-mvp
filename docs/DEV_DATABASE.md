# The test & dev database flow, from zero

*Written for someone with no prior knowledge of how chordial's databases are
set up. Companion to `scripts/dev_db.py` and `tests/conftest.py`.*

---

## 1. The mental model: three kinds of database

Chordial can talk to two database engines, and which one it uses is decided
by a single setting — the `DATABASE_URL` environment variable:

- **Postgres** is a database *server*: a background program that owns its
  data and answers questions over a socket. Production runs on this.
  A postgres URL looks like `postgresql+psycopg://user:password@localhost/chordial`.
- **SQLite** is a database *file*: the entire database — every table, every
  row — lives in one ordinary file (e.g. `chordial_dev.db`). No server, no
  password. A sqlite URL looks like `sqlite:///chordial_dev.db`.

Everything below follows from one fact: **a sqlite database is just a file,
so copying the file copies the database, and deleting the file deletes it.**

In day-to-day work you'll touch three databases:

| database | engine | what it's for |
|---|---|---|
| **prod** (`chordial` on postgres) | postgres | the real thing. real conversations, real memories. never point anything experimental at it |
| **dev** (`chordial_dev.db`) | sqlite | a sandbox you chat against. wiped and rebuilt freely |
| **test** (temp files / `chordial_test`) | sqlite or postgres | created and destroyed automatically by the test suite. you never touch these directly |

---

## 2. The dev database: `scripts/dev_db.py`

This is the sandbox tool. Every command is safe — nothing here can touch
prod, because the script hard-pins itself to `chordial_dev.db`.

### Fresh — "I want to test the brand-new-user experience"

```bash
poetry run python scripts/dev_db.py fresh
```

Deletes `chordial_dev.db` and rebuilds it empty, with the schema at the
latest version. The app started against this database has never met anyone —
you get the full introduction/onboarding flow from the very first message.

### Seed — "I want to jump straight into a lived-in state"

```bash
poetry run python scripts/dev_db.py seed --telegram-id 123456789
```

Does `fresh`, then fills the database with a plausible life: active helpers
(so there's no intro flow), three plans, goals, tasks (one due today, one
overdue, one in progress), an active cycle, a couple of wins, yesterday's
check-in, three notes, and upcoming occasions. The sample data is created
through the real `WorkspaceStore`, so seeding is itself a small integration
test.

`--telegram-id` is your own Telegram numeric id — including it links *your*
Telegram account to the seeded dev user, so you can open Telegram and start
chatting immediately. (Ask @userinfobot on Telegram if you don't know your id.)

### Running the app against the dev database

```bash
DATABASE_URL=sqlite:///chordial_dev.db poetry run python main.py
```

That prefix sets the database for *this one run* — your `.env` file is not
modified. (Environment variables you set directly always beat `.env`,
because `load_dotenv()` never overrides an existing variable.)

### Snapshot / restore — save states, like an emulator

```bash
poetry run python scripts/dev_db.py snapshot before-cycle-test
# ...chat, mutate, break things...
poetry run python scripts/dev_db.py restore before-cycle-test
```

`snapshot` copies the current dev database to `dev_states/<name>.db`;
`restore` copies it back. Both are instant. This is the payoff of sqlite
being a file: you can set up a tricky state once, snapshot it, and return to
it as many times as you want. `dev_db.py list` shows what you've saved.

---

## 3. The automated test databases (you never manage these)

Running `poetry run pytest` needs databases too, but they're invisible:

- **Default (sqlite lane):** `tests/conftest.py` creates a throwaway temp
  file before any test runs and points `DATABASE_URL` at it, so tests can
  never write to a real database. Many test modules additionally create
  their own private temp files. Everything is deleted by the OS eventually;
  you never see it.
- **Postgres lane:** the same suite can run against real postgres to catch
  engine differences (this is what guards the alembic migrations):

  ```bash
  # one-time: brew services run postgresql@16   (mac; on the server it's already running)
  #           createdb chordial_test
  TEST_DATABASE_URL=postgresql+psycopg://dain@localhost/chordial_test poetry run pytest
  ```

  The conftest wipes and rebuilds `chordial_test`'s schema every run, and it
  refuses any database whose name doesn't contain "test" — so a typo can't
  aim the test suite at prod. Run this lane before merging anything that
  touches models or migrations; sqlite is the fast everyday default.

---

## 4. Running dev and prod daemons side by side (on the server)

Fully supported. Two copies of the app are just two processes; they don't
know about each other. The rules:

1. **Different `DATABASE_URL`** — prod on postgres, dev on its sqlite file.
   This is the non-negotiable one.
2. **Different Telegram bots** — one bot token can only be polled by ONE
   process at a time (Telegram returns conflicts if two processes poll the
   same token, and messages get eaten). So the dev daemon needs its own
   BotFather bots: a dev `TELEGRAM_TOKEN`, and a dev `TELEGRAM_TOKEN_<HELPER>`
   for **every enabled helper** — each helper is its own bot. Tip: keep
   `ENABLED_HELPERS` small in dev so you only need a couple of dev bots.
3. **Different Discord app/token** (or just leave Discord disabled in dev
   with `ENABLE_DISCORD=false`).
4. **Different Telegram group** (`TELEGRAM_GROUP_CHAT_ID`) if you're testing
   group-chat mode — or unset it and test DMs only.
5. **No `NOTION_API_KEY` in dev** (until the native workspace fully replaces
   Notion): a dev daemon pointed at the real Notion would happily write to
   the real dainframe.

### How to give each daemon its own settings

Since env vars beat `.env`, the same checkout can serve both daemons — the
prod systemd unit uses the `.env` file, and a dev unit overrides what
differs:

```ini
# /etc/systemd/system/chordial-dev.service (sketch)
[Service]
WorkingDirectory=/home/dain/chordial-mvp
EnvironmentFile=/home/dain/chordial-mvp/.env.dev   # DATABASE_URL, dev bot tokens, ...
ExecStart=/home/dain/.local/bin/poetry run python main.py
```

where `.env.dev` contains *complete* values for everything that must differ
(`DATABASE_URL`, all telegram tokens, `ENABLED_HELPERS`, no notion key).
Alternatively, keep a second checkout (`~/chordial-dev/`) with its own
`.env` — more disk, zero shared-file surprises, and you can run a different
branch in dev than in prod. Both approaches are fine; the second is simpler
to reason about, and lets dev run Phase-B code while prod stays on main.

One shared-checkout caveat: both daemons write to the same `prompt_logs/`
directory. Harmless, but if the interleaving ever confuses you, that's a
point in favor of the second checkout.

---

## 5. Gotchas, collected

- **Never** hand-edit `DATABASE_URL` in `.env` to point at the dev database
  "temporarily" — that's how prod daemons end up on sandbox data. Use the
  one-run prefix (`DATABASE_URL=... poetry run python main.py`) instead.
- One bot token, one process. Every telegram polling conflict traces back to
  this.
- `chordial_dev.db` and `dev_states/` are gitignored (`*.db`) — snapshots
  are local artifacts, not repo content.
- The dev database schema updates itself: the app (and `dev_db.py fresh`)
  runs `alembic upgrade head` at startup, so after a `git pull` your dev db
  just works.
