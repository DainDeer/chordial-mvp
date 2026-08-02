"""nightly postgres backup (NATIVE_MIGRATION_PLAN §2.6): pg_dump
custom-format dumps into backups/, keeping the newest 14.

connection parsing is the whole reason this is python: passwords with
url-special characters (: @) break libpq's url parser ("invalid integer
value ... for connection option port") while sqlalchemy's make_url - the
parser the APP connects with - handles them. so we parse DATABASE_URL the
same way the app does and hand pg_dump the components via PG* env vars,
where no quoting exists to get wrong.

run through the venv (sqlalchemy + the repo .env come along for free):
    poetry run python scripts/pg_backup.py            # DATABASE_URL from .env
    poetry run python scripts/pg_backup.py <url>      # explicit override

restore rehearsal (the §2.5 preflight gate):
    createdb restore_test && pg_restore -d restore_test backups/chordial-<ts>.dump
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url  # noqa: E402

from config import Config  # noqa: E402  (loads the repo .env)

KEEP = 14


def main() -> int:
    url = make_url(sys.argv[1] if len(sys.argv) > 1 else Config.DATABASE_URL)
    if not url.drivername.startswith("postgres"):
        print(f"not a postgres url ({url.drivername}) - nothing to back up")
        return 1

    env = dict(os.environ)
    for key, value in (
        ("PGHOST", url.host),
        ("PGPORT", url.port),
        ("PGUSER", url.username),
        ("PGPASSWORD", url.password),
    ):
        if value is not None:
            env[key] = str(value)

    name = url.database or "chordial"
    backups = Path(__file__).resolve().parents[1] / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dump = backups / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.dump"

    subprocess.run(
        ["pg_dump", "-Fc", "-d", name, "-f", str(dump)], env=env, check=True
    )
    print(f"wrote {dump}")

    # rotate: delete all but the newest KEEP dumps for this db
    dumps = sorted(backups.glob(f"{name}-*.dump"), reverse=True)
    for old in dumps[KEEP:]:
        old.unlink()
        print(f"rotated out {old.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
