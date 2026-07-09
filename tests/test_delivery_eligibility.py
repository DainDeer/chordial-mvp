"""delivery-eligibility tests: who the scheduler is allowed to proactively
message, and deactivating a dead link.

uses an isolated temp-file sqlite db bound just for these tests (patches the
SessionLocal that get_db() reads) so it never touches the real chordial.db,
regardless of test import order.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, PlatformIdentity  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    """fresh isolated sqlite db, wired into get_db() for the duration of a test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # get_db() reads the module-global SessionLocal at call time
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    yield TestSession
    engine.dispose()


def _add_user(session, *, name="tester", is_active=True, is_test=False,
              identity_active=True, platform="discord", platform_user_id="123"):
    with session() as s:
        user = User(preferred_name=name, is_active=is_active, is_test=is_test)
        s.add(user)
        s.flush()
        uuid = user.uuid
        s.add(PlatformIdentity(
            user_uuid=uuid,
            platform=platform,
            platform_user_id=platform_user_id,
            is_active=identity_active,
        ))
        s.commit()
        return uuid


def test_normal_user_is_eligible(db):
    uuid = _add_user(db, platform_user_id="normal")
    assert run(UserManager().get_scheduled_users()) == [uuid]


def test_test_user_is_excluded(db):
    _add_user(db, is_test=True, platform_user_id="seed")
    assert run(UserManager().get_scheduled_users()) == []


def test_inactive_user_is_excluded(db):
    _add_user(db, is_active=False, platform_user_id="churned")
    assert run(UserManager().get_scheduled_users()) == []


def test_inactive_platform_link_is_excluded(db):
    _add_user(db, identity_active=False, platform_user_id="dead-link")
    assert run(UserManager().get_scheduled_users()) == []


def test_user_without_preferred_name_is_excluded(db):
    _add_user(db, name=None, platform_user_id="unonboarded")
    assert run(UserManager().get_scheduled_users()) == []


def test_dual_platform_user_is_one_schedule_slot(db):
    """a person on discord AND telegram is one person, not two check-ins."""
    uuid = _add_user(db, platform_user_id="d-1")
    with db() as s:
        s.add(PlatformIdentity(user_uuid=uuid, platform="telegram",
                               platform_user_id="t-1", is_active=True))
        s.commit()
    assert run(UserManager().get_scheduled_users()) == [uuid]


def test_deactivate_platform_identity_flips_the_flag(db):
    uuid = _add_user(db, platform_user_id="going-dead")
    users = UserManager()

    # eligible before
    assert run(users.get_scheduled_users()) == [uuid]

    run(users.deactivate_platform_identity("discord", "going-dead"))

    # excluded after - their only link is off
    assert run(users.get_scheduled_users()) == []


def test_deactivate_unknown_identity_is_a_safe_noop(db):
    # no rows exist - should not raise
    run(UserManager().deactivate_platform_identity("discord", "nobody"))


# --- delivery targeting -------------------------------------------------------

def _add_dual_platform_user(db):
    uuid = _add_user(db, platform_user_id="d-1")            # discord, older row
    with db() as s:
        s.add(PlatformIdentity(user_uuid=uuid, platform="telegram",
                               platform_user_id="t-1", is_active=True))
        s.commit()
    return uuid


def test_resolve_prefers_the_active_platform(db):
    uuid = _add_dual_platform_user(db)
    users = UserManager()
    assert run(users.resolve_delivery_identity(uuid, "discord")) == ("discord", "d-1")
    assert run(users.resolve_delivery_identity(uuid, "telegram")) == ("telegram", "t-1")


def test_resolve_falls_back_when_preferred_is_dead(db):
    """blocked on the platform they last used -> reach them on the other one
    rather than going silent."""
    uuid = _add_dual_platform_user(db)
    users = UserManager()
    run(users.deactivate_platform_identity("telegram", "t-1"))
    assert run(users.resolve_delivery_identity(uuid, "telegram")) == ("discord", "d-1")


def test_resolve_falls_back_when_no_preference(db):
    """user never messaged (no active platform) -> most recent active link."""
    uuid = _add_dual_platform_user(db)
    assert run(UserManager().resolve_delivery_identity(uuid, None)) == ("telegram", "t-1")


def test_resolve_respects_allowed_platforms(db):
    """never target a platform with no live interface."""
    uuid = _add_dual_platform_user(db)
    users = UserManager()
    assert run(users.resolve_delivery_identity(uuid, "telegram", ["discord"])) == ("discord", "d-1")


def test_resolve_none_when_nothing_deliverable(db):
    uuid = _add_user(db, identity_active=False, platform_user_id="dead")
    assert run(UserManager().resolve_delivery_identity(uuid, "discord")) is None
