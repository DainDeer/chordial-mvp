"""bootstrap a FRESH, EMPTY database for a fresh-start launch
(NATIVE_MIGRATION_PLAN section 3, phase C).

creates the schema from the models and stamps alembic at head - the section
2.3 pattern. an empty postgres database must NOT run `alembic upgrade head`
(the migration chain's history is sqlite-shaped and is not guaranteed to
replay on another dialect); this script is the supported way to bring a new
database to life. after it runs, the app's startup `upgrade head` is a
no-op until the next real revision, which applies normally.

    poetry run python scripts/init_fresh_db.py --url postgresql+psycopg://user:pass@localhost/chordial_prod

refuses to touch a database that already has tables - this tool only ever
births databases, never modifies them.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.database.models import Base  # noqa: E402


def repo_head_revision() -> str:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(
        AlembicConfig(str(ROOT / "alembic.ini"))).get_current_head()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True,
                        help="database URL (e.g. postgresql+psycopg://user:pass@host/db)")
    args = parser.parse_args()

    engine = create_engine(args.url)
    existing = inspect(engine).get_table_names()
    if existing:
        sys.exit(f"refusing: database is not empty (has {len(existing)} tables, "
                 f"e.g. {', '.join(sorted(existing)[:5])}). this tool only "
                 "births fresh databases - it never modifies existing ones.")

    head = repo_head_revision()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                     {"v": head})

    tables = sorted(inspect(engine).get_table_names())
    print(f"created {len(tables) - 1} tables + alembic_version, stamped at {head}")
    print("the database is ready - point DATABASE_URL at it and start the app.")


if __name__ == "__main__":
    main()
