"""dev database tooling: disposable sqlite states for local testing.

the dev database is a plain sqlite file (chordial_dev.db), which makes state
management trivial - fresh means delete the file, a saved state is a file
copy. postgres fidelity is the pg test lane's job (TEST_DATABASE_URL); this
tool optimizes for flow.

    poetry run python scripts/dev_db.py fresh
        empty database at alembic head - the new-user / introduction flow.

    poetry run python scripts/dev_db.py seed [--telegram-id 12345]
        fresh + a lived-in sample state: active helpers (no intro flow),
        plans/goals/tasks/cycle/wins/check-ins/notes/occasions seeded through
        the real WorkspaceStore. --telegram-id links YOUR telegram account to
        the dev user so you can chat immediately.

    poetry run python scripts/dev_db.py link-code
        mint a one-time device-link code for the desktop app (app/) - the
        no-telegram path. on a fresh db this also creates a bare dev user,
        so the app's first message gets the real introduction flow.

    poetry run python scripts/dev_db.py snapshot NAME
    poetry run python scripts/dev_db.py restore NAME
    poetry run python scripts/dev_db.py list
        save/load named states (sqlite backup api -> dev_states/NAME.db).

then run the app against it:

    DATABASE_URL=sqlite:///chordial_dev.db poetry run python main.py
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "chordial_dev.db"
STATES_DIR = ROOT / "dev_states"

# everything the app stack imports reads DATABASE_URL at import time -
# pin it to the dev file before any src import
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
sys.path.insert(0, str(ROOT))


def _remove_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def cmd_fresh(_args) -> None:
    _remove_db()
    from src.database.database import init_db
    init_db()
    print(f"fresh dev db at alembic head: {DB_PATH.name}")


def cmd_seed(args) -> None:
    cmd_fresh(args)
    from src.database.database import get_db
    from src.database.models import User, PlatformIdentity, HelperState
    from src.services.workspace import get_store

    today = date.today()
    with get_db() as db:
        db.add(User(uuid="dev-user", preferred_name="megan", timezone="America/Chicago"))
        for helper in ("vel", "pip", "skip", "remy", "mabel", "juniper", "edwin"):
            db.add(HelperState(user_uuid="dev-user", helper_id=helper, status="active"))
        if args.telegram_id:
            db.add(PlatformIdentity(user_uuid="dev-user", platform="telegram",
                                    platform_user_id=str(args.telegram_id)))

    s = get_store()
    u = "dev-user"

    album = s.create_plan(u, "finish the album", "juniper", status="active",
                          cadence="weekly", why="because the songs deserve to exist")
    book = s.create_plan(u, "write the novel", "juniper", status="active", cadence="loose")
    s.create_plan(u, "spring cleaning", "pip", status="complete")

    mix = s.create_goal(u, album["id"], "mix track one",
                        target=(today + timedelta(days=14)).isoformat(),
                        done_means="a bounce i can play in the car without wincing")
    s.create_goal(u, book["id"], "finish chapter three")

    s.create_task(u, "bounce stems", goal_id=mix["id"], scheduled=today.isoformat(),
                  window="afternoon", priority="high", pom_estimate=2)
    s.create_task(u, "eq the vocal bus", goal_id=mix["id"],
                  scheduled=(today - timedelta(days=2)).isoformat())  # overdue
    t = s.create_task(u, "outline the storm scene", plan_id=book["id"],
                      scheduled=today.isoformat(), window="evening")
    s.update_task(u, t["id"], status="in_progress")
    done = s.create_task(u, "back up session files", plan_id=album["id"])
    s.update_task(u, done["id"], status="done")

    cyc = s.create_cycle(u, "cycle 12", status="active",
                         start_date=(today - timedelta(days=6)).isoformat(),
                         end_date=(today + timedelta(days=7)).isoformat(),
                         focus="music first, words second, rest on sundays",
                         theme="finish what's half-done",
                         capacity_blocks=24)

    # the cycle spine (phase 5): commitments with a frozen baseline, so the
    # cycle view has real shared state to show on first boot
    from src.services.cycles import CycleStore
    cs = CycleStore()
    cs.create_commitment(u, cyc["id"], "mix track one", priority="high",
                         blocks_planned=6, plan_id=album["id"],
                         next_action="bounce stems (1 block)")
    cs.create_commitment(u, cyc["id"], "chapter three drafted",
                         priority="medium", blocks_planned=4,
                         plan_id=book["id"], task_id=t["id"],
                         next_action="outline the storm scene")
    cs.freeze_baseline(u, cyc["id"])

    s.log_win(u, "backed up every session", (today - timedelta(days=1)).isoformat(),
              "juniper", plan_id=album["id"], weight="solid",
              evidence="finally! the external drive is alive")
    s.log_win(u, "wrote 400 words before breakfast",
              (today - timedelta(days=2)).isoformat(), "juniper",
              plan_id=book["id"], weight="spark")

    s.log_checkin(u, (today - timedelta(days=1)).isoformat(), "evening", "vel",
                  energy="good", notes="steady day, mixed for an hour",
                  plan_ids=[album["id"]])

    s.jot(u, "bridge idea: modulate up a third and strip to just piano",
          plan_id=album["id"], tags=["music"], helper="juniper")
    s.jot(u, "the storm should mirror the argument in chapter one",
          plan_id=book["id"], tags=["writing"], helper="juniper")
    s.jot(u, "video idea: studio tour but every cut is on the beat", tags=["video"])

    s.create_occasion(u, "moms birthday", date(today.year, 9, 3).isoformat(),
                      recurrence="yearly")
    s.create_occasion(u, "dentist", (today + timedelta(days=2)).isoformat(),
                      time="14:30")

    linked = f", telegram id {args.telegram_id} linked" if args.telegram_id else ""
    print(f"seeded sample state for 'dev-user'{linked}")
    print(f"run:  DATABASE_URL=sqlite:///{DB_PATH.name} poetry run python main.py")


def cmd_link_code(_args) -> None:
    """mint a one-time device-link code for the dev user, so the desktop app
    can link without telegram in the loop. creates the dev user bare if the
    db is fresh - no name, no helper states - which is exactly the app-first
    experience: vel runs the front-door introduction on the first message."""
    if not DB_PATH.exists():
        sys.exit(f"no {DB_PATH.name} yet - run: dev_db.py fresh (or seed)")
    from src.database.database import get_db
    from src.database.models import User
    from src.web.device_auth import mint_device_link_code

    with get_db() as db:
        if db.query(User).filter(User.uuid == "dev-user").first() is None:
            db.add(User(uuid="dev-user", timezone="America/Chicago"))
            print("created bare 'dev-user' (fresh db - the app gets the "
                  "introduction flow)")
    code = mint_device_link_code("dev-user")
    print(f"device link code for 'dev-user':  {code}")
    print("paste it into the app's link screen (codes are one-time and expire)")


def cmd_snapshot(args) -> None:
    if not DB_PATH.exists():
        sys.exit(f"no {DB_PATH.name} to snapshot")
    STATES_DIR.mkdir(exist_ok=True)
    dest = STATES_DIR / f"{args.name}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    src.backup(dst)   # WAL-safe online copy
    dst.close(); src.close()
    print(f"snapshot saved: dev_states/{dest.name}")


def cmd_restore(args) -> None:
    source = STATES_DIR / f"{args.name}.db"
    if not source.exists():
        sys.exit(f"no snapshot named {args.name!r} - try: dev_db.py list")
    _remove_db()
    src = sqlite3.connect(source)
    dst = sqlite3.connect(DB_PATH)
    src.backup(dst)
    dst.close(); src.close()
    print(f"restored {DB_PATH.name} from dev_states/{source.name}")


def cmd_list(_args) -> None:
    snaps = sorted(STATES_DIR.glob("*.db")) if STATES_DIR.exists() else []
    if not snaps:
        print("no snapshots yet - dev_db.py snapshot NAME creates one")
    for p in snaps:
        print(f"  {p.stem}  ({p.stat().st_size // 1024} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fresh", help="empty db at alembic head (new-user flow)")
    seed = sub.add_parser("seed", help="fresh + lived-in sample state")
    seed.add_argument("--telegram-id", help="link your telegram account to the dev user")
    snap = sub.add_parser("snapshot", help="save current dev db as a named state")
    snap.add_argument("name")
    rest = sub.add_parser("restore", help="load a named state")
    rest.add_argument("name")
    sub.add_parser("list", help="list saved states")
    sub.add_parser("link-code",
                   help="mint a device-link code for the desktop app")
    args = parser.parse_args()
    {"fresh": cmd_fresh, "seed": cmd_seed, "snapshot": cmd_snapshot,
     "restore": cmd_restore, "list": cmd_list,
     "link-code": cmd_link_code}[args.command](args)


if __name__ == "__main__":
    main()
