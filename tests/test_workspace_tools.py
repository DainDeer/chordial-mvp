"""workspace tools: the native replacement for the notion tool surface.

contract assertions first (the 9 legacy names survive with compatible
required fields and flags), then behavior through the real registry +
store: display-vocab round-trips, resolution-ladder candidates, link
inheritance, graceful constraint errors, persona allowlists under both
backends.
"""
import asyncio
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import Base, User
from src.providers.ai.types import ToolCall
from src.services.tools import build_default_registry

U1 = "user-one"

# the byte contract: every legacy notion tool name, with the required
# fields its schema declared. phase D may drop the *_project aliases, but
# these nine survive the backend swap untouched.
LEGACY_CONTRACTS = {
    "list_tasks": [],
    "create_task": ["title"],
    "update_task": ["task"],
    "list_projects": [],
    "create_project": ["title"],
    "update_project": ["project"],
    "list_cycles": [],
    "create_cycle": ["title"],
    "update_cycle": ["cycle"],
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def registry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(Config, "WORKSPACE_BACKEND", "native")
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain", timezone="UTC"))
        s.commit()
    yield build_default_registry()
    engine.dispose()


def call(registry, name, **tool_input):
    result = run(registry.execute(
        ToolCall(id="t", name=name, input=tool_input), U1))
    return result


# --- contract assertions -----------------------------------------------------


def test_nine_legacy_tool_names_survive_with_compatible_contracts(registry):
    defs = {d.name: d for d in registry.definitions()}
    for name, required in LEGACY_CONTRACTS.items():
        assert name in defs, f"legacy tool {name} missing under native backend"
        assert defs[name].input_schema.get("required", []) == required


def test_plan_named_aliases_are_registered(registry):
    names = {d.name for d in registry.definitions()}
    assert {"list_plans", "create_plan", "update_plan"} <= names


def test_list_tools_dont_record_events_and_mutations_do(registry):
    for name in ("list_tasks", "list_projects", "list_cycles", "list_goals",
                 "list_wins", "list_checkins", "list_notes", "list_occasions"):
        assert registry.should_record(name) is False, name
    for name in ("create_task", "update_task", "create_project", "jot",
                 "log_win", "log_checkin", "log_occasion", "update_note"):
        assert registry.should_record(name) is True, name


def test_jot_is_terminal_like_save_memory(registry):
    assert registry.is_terminal("jot") is True
    assert registry.is_terminal("create_task") is False


# --- display vocab round-trips -----------------------------------------------


def test_task_status_display_round_trip(registry):
    call(registry, "create_task", title="bounce stems", status="To do")
    listing = call(registry, "list_tasks").content
    assert "[To do]" in listing


def test_legacy_project_statuses_still_accepted(registry):
    call(registry, "create_project", title="old style", status="Not started")
    call(registry, "create_project", title="rolling", status="recurring")
    listing = call(registry, "list_plans", include_closed=True).content
    assert "[Proposed]" in listing and "[Active]" in listing


# --- resolution + links ------------------------------------------------------


def test_ambiguity_returns_candidates_never_a_guess(registry):
    call(registry, "create_project", title="album artwork")
    call(registry, "create_project", title="album release")
    result = call(registry, "update_plan", plan="album", status="Active")
    assert "multiple plans match" in result.content
    assert "p1" in result.content and "p2" in result.content
    listing = call(registry, "list_plans").content
    assert "[Active]" not in listing   # nothing was written


def test_goal_link_implies_its_plan(registry):
    call(registry, "create_project", title="finish the album", helper="aria")
    call(registry, "create_goal", plan="finish the album", title="mix track one")
    call(registry, "create_task", title="bounce stems", goal="mix track one")
    listing = call(registry, "list_tasks").content
    assert "plan=finish the album" in listing and "goal=mix track one" in listing


def test_win_inherits_plan_from_task(registry):
    call(registry, "create_project", title="finish the album", helper="aria")
    call(registry, "create_task", title="bounce stems", project="finish the album")
    call(registry, "log_win", title="bounced the stems", task="bounce stems")
    wins = call(registry, "list_wins").content
    assert "plan=finish the album" in wins


def test_public_id_round_trip_the_reconciler_contract(registry):
    """the reconciler echoes payload ids (t42) into update_task verbatim -
    that exact shape must resolve and write."""
    call(registry, "create_task", title="walk outside")
    result = call(registry, "update_task", task="t1", status="Done")
    assert not result.is_error and "updated task (id=t1)" in result.content
    assert "walk outside" in call(registry, "list_tasks", status="Done").content


def test_duplicate_morning_checkin_is_a_graceful_tool_error(registry):
    today = date.today().isoformat()
    ok = call(registry, "log_checkin", kind="morning", date=today)
    assert not ok.is_error
    dupe = call(registry, "log_checkin", kind="morning", date=today)
    assert dupe.is_error and "already exists" in dupe.content


def test_note_promotion_via_tool(registry):
    call(registry, "create_task", title="write the bridge")
    call(registry, "jot", body="bridge idea: modulate up a third")
    result = call(registry, "update_note", note="n1", promoted_task="write the bridge")
    assert not result.is_error
    open_notes = call(registry, "list_notes").content
    assert open_notes == "no notes matched."   # promoted leaves the open set
    archived = call(registry, "list_notes", include_closed=True).content
    assert "[Promoted]" in archived


# --- allowlists under both backends ------------------------------------------


def test_mochi_allowlist_resolves_under_native(registry):
    from src.personas import load_personas
    view = registry.view(load_personas()["mochi"].tools)
    names = {d.name for d in view.definitions()}
    assert {"jot", "log_occasion", "list_wins", "list_checkins"} <= names
    assert not names & set(LEGACY_CONTRACTS), "mochi must never see task tools"


def test_mochi_allowlist_resolves_under_notion_backend_too(monkeypatch):
    """the v3 extras register under BOTH backends, so persona cards stay
    valid even before the native cutover (and without a notion key)."""
    monkeypatch.setattr(Config, "WORKSPACE_BACKEND", "notion")
    monkeypatch.setattr(Config, "NOTION_API_KEY", None)
    reg = build_default_registry()
    names = {d.name for d in reg.definitions()}
    assert {"jot", "log_occasion", "list_wins", "list_checkins"} <= names
    assert "list_tasks" not in names   # no key -> no notion task tools
    from src.personas import load_personas
    reg.view(load_personas()["mochi"].tools)   # must not raise
