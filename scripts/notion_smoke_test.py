"""live smoke test for the notion integration.

runs a few read-only tool calls against the real dainframe using your
NOTION_API_KEY from .env. read-only by default; pass --write to also create a
throwaway task, read it back, and mark it Done (it stays in the db - delete it
in notion if you don't want it).

    python scripts/notion_smoke_test.py
    python scripts/notion_smoke_test.py --write
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from src.services.tools import notion_tools as NT  # noqa: E402


async def read_only():
    print("== list_cycles ==")
    print(await NT._list_cycles({}, "smoke"))
    print("\n== list_projects (limit 5) ==")
    print(await NT._list_projects({"limit": 5}, "smoke"))
    print("\n== list_tasks (In progress, limit 8) ==")
    print(await NT._list_tasks({"status": "In progress", "limit": 8}, "smoke"))


async def write_cycle():
    title = "smoke-test task (safe to delete)"
    print("\n== create_task ==")
    print(await NT._create_task(
        {"title": title, "priority": "low", "status": "To do"}, "smoke"
    ))
    print("\n== list_tasks (To do) — should include it ==")
    print(await NT._list_tasks({"status": "To do", "limit": 15}, "smoke"))
    print("\n== update_task -> Done ==")
    print(await NT._update_task({"task": title, "status": "Done"}, "smoke"))


async def main(do_write: bool):
    if not Config.notion_enabled():
        print("NOTION_API_KEY is not set. add it to .env and retry.")
        return
    print(f"notion key detected, api version {Config.NOTION_API_VERSION}\n")
    await read_only()
    if do_write:
        await write_cycle()
    print("\nsmoke test complete.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="also exercise create/update")
    args = ap.parse_args()
    asyncio.run(main(args.write))
