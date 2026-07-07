# Database migrations (Alembic)

Alembic is the single source of truth for the database schema. The SQLAlchemy
models in `src/database/models.py` are the source; Alembic revisions in
`alembic/versions/` are how that schema is applied to any database.

## How it runs

`init_db()` (in `src/database/database.py`, called once at startup in `main.py`)
runs `alembic upgrade head` programmatically. It's idempotent — a no-op when the
database is already current — so **a fresh install or a `git pull` on the server
needs no manual migration step**. `create_all()` is no longer used to build the
schema; Alembic owns it.

Connection URL and model metadata are wired up in `alembic/env.py`, which pulls
`Config.DATABASE_URL` so the CLI and the app always target the same database.
SQLite batch mode (`render_as_batch`) is on, so future `ALTER`/`DROP COLUMN`
migrations work despite SQLite's limitations.

## Adding a schema change

1. Edit the models in `src/database/models.py`.
2. Autogenerate a revision:
   ```
   alembic revision --autogenerate -m "short description"
   ```
3. **Read the generated file** in `alembic/versions/` — autogenerate is good but
   not perfect (data migrations, tricky type changes, and renames need hand
   edits).
4. Apply it locally: `alembic upgrade head` (or just start the app).
5. Commit the models change and the revision together. On the next deploy, the
   server applies it automatically at startup.

Useful commands: `alembic current` (what revision is this db at), `alembic
history`, `alembic downgrade -1` (undo the last revision).

## One-time transition for an existing database

A database that already has tables but no `alembic_version` row must be told it's
already at the baseline, or `upgrade head` will try to re-create existing tables.
This is only needed once, per pre-existing database.

- **A server with disposable data** (e.g. the VPS during testing): simplest is to
  delete the database file and start the app — Alembic builds it fresh.
  ```
  rm chordial.db && python main.py     # init_db() builds the schema
  ```
- **A database with data to keep** (e.g. local dev): make sure it's schema-current
  first, then stamp it at the baseline without running anything:
  ```
  alembic stamp head
  ```

After that, every environment is on Alembic and upgrades are automatic.
