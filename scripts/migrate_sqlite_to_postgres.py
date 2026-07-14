"""one-shot data copy: sqlite -> postgres (NATIVE_MIGRATION_PLAN §2.4).

copies every model table row-for-row, preserving primary keys, then resets
postgres sequences and verifies the result. both URLs are explicit arguments
- never inferred from DATABASE_URL - so a misconfigured env can't invert the
copy direction. the whole copy runs in a single target transaction: it either
lands complete or not at all.

usage:
    poetry run python scripts/migrate_sqlite_to_postgres.py \
        --source sqlite:///chordial.db \
        --target postgresql+psycopg://dain@localhost/chordial

    --force   truncate a non-empty target first (for retrying a failed run)

idempotence: a clean run refuses a non-empty target; --force restarts from
zero rather than resuming, which is the safe shape for an atomic copy.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text, func

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.database.models import Base  # noqa: E402

BATCH_SIZE = 1000


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, help="sqlite URL (sqlite:///path.db)")
    p.add_argument("--target", required=True, help="postgres URL (postgresql+psycopg://...)")
    p.add_argument("--force", action="store_true",
                   help="truncate non-empty target tables before copying")
    args = p.parse_args()
    # direction guard: this script only ever moves sqlite -> postgres
    if not args.source.startswith("sqlite"):
        p.error(f"--source must be a sqlite URL, got: {args.source}")
    if not args.target.startswith("postgresql"):
        p.error(f"--target must be a postgresql URL, got: {args.target}")
    return args


def alembic_version(conn):
    if not inspect(conn).has_table("alembic_version"):
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def ensure_target_schema(source_engine, target_engine):
    """create tables from the models if missing (plan §2.3: create_all, not
    chain replay) and stamp alembic_version to match the source, so future
    `upgrade head` runs continue from the same point on both databases."""
    with source_engine.connect() as conn:
        source_version = alembic_version(conn)
    if source_version is None:
        sys.exit("source has no alembic_version - refusing to copy from an unstamped db")

    Base.metadata.create_all(target_engine)  # checkfirst by default
    with target_engine.begin() as conn:
        target_version = alembic_version(conn)
        if target_version is None:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                         {"v": source_version})
            print(f"stamped target at {source_version}")
        elif target_version != source_version:
            sys.exit(f"alembic version mismatch: source={source_version} "
                     f"target={target_version} - bring both to the same revision first")


def check_target_empty(target_engine, force):
    with target_engine.begin() as conn:
        nonempty = [t.name for t in Base.metadata.sorted_tables
                    if conn.execute(select(func.count()).select_from(t)).scalar()]
        if not nonempty:
            return
        if not force:
            sys.exit(f"refusing: target tables not empty: {', '.join(nonempty)} "
                     "(use --force to truncate and restart the copy)")
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        print(f"--force: truncated {len(Base.metadata.sorted_tables)} tables")


def copy_all(source_engine, target_conn):
    """walk tables in FK-safe order, stream rows, insert preserving PKs."""
    for table in Base.metadata.sorted_tables:
        copied = 0
        with source_engine.connect() as src:
            result = src.execute(select(table))
            while batch := result.fetchmany(BATCH_SIZE):
                target_conn.execute(table.insert(), [dict(row._mapping) for row in batch])
                copied += len(batch)
        print(f"  {table.name}: {copied} rows")


def reset_sequences(target_conn):
    """explicit-PK inserts don't advance serial sequences; without this the
    first post-cutover insert hits a duplicate key error."""
    for table in Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or pk[0].type.python_type is not int:
            continue  # string PKs (users.uuid) have no sequence
        col = pk[0].name
        seq = target_conn.execute(
            text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col}
        ).scalar()
        if seq is None:
            print(f"  warning: no sequence found for {table.name}.{col}, skipped")
            continue
        max_id = target_conn.execute(
            select(func.max(table.c[col]))).scalar()
        if max_id is None:
            # empty table: next value should be 1
            target_conn.execute(text("SELECT setval(:s, 1, false)"), {"s": seq})
        else:
            target_conn.execute(text("SELECT setval(:s, :m)"), {"s": seq, "m": max_id})


def verify(source_engine, target_engine):
    """per-table row counts, plus content spot-checks on the two tables that
    matter most. returns False on any mismatch."""
    ok = True
    with source_engine.connect() as src, target_engine.connect() as tgt:
        print(f"\n{'table':<26}{'source':>8}{'target':>8}")
        for table in Base.metadata.sorted_tables:
            s = src.execute(select(func.count()).select_from(table)).scalar()
            t = tgt.execute(select(func.count()).select_from(table)).scalar()
            flag = "" if s == t else "  << MISMATCH"
            ok &= s == t
            print(f"{table.name:<26}{s:>8}{t:>8}{flag}")

        events = Base.metadata.tables["conversation_events"]
        latest = select(events.c.user_uuid, func.max(events.c.id)).group_by(events.c.user_uuid)
        memories = Base.metadata.tables["memories"]
        per_user = select(memories.c.user_uuid, func.count()).group_by(memories.c.user_uuid)
        for label, query in [("latest event id per user", latest),
                             ("memory count per user", per_user)]:
            s = dict(src.execute(query).all())
            t = dict(tgt.execute(query).all())
            match = "ok" if s == t else "MISMATCH"
            ok &= s == t
            print(f"spot-check {label}: {match}")
    return ok


def main():
    args = parse_args()
    source_engine = create_engine(args.source)
    target_engine = create_engine(args.target)

    ensure_target_schema(source_engine, target_engine)
    check_target_empty(target_engine, args.force)

    print("copying:")
    with target_engine.begin() as conn:  # one transaction: all-or-nothing
        copy_all(source_engine, conn)
        reset_sequences(conn)

    if not verify(source_engine, target_engine):
        sys.exit("VERIFICATION FAILED - target transaction was committed but "
                 "does not match source; fix and rerun with --force")
    print("\ncopy complete and verified")


if __name__ == "__main__":
    main()
