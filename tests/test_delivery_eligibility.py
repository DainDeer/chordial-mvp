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
    _add_user(db, platform_user_id="normal")
    result = run(UserManager().get_users_with_scheduled_messages("discord"))
    assert [pid for _, pid in result] == ["normal"]


def test_test_user_is_excluded(db):
    _add_user(db, is_test=True, platform_user_id="seed")
    result = run(UserManager().get_users_with_scheduled_messages("discord"))
    assert result == []


def test_inactive_user_is_excluded(db):
    _add_user(db, is_active=False, platform_user_id="churned")
    result = run(UserManager().get_users_with_scheduled_messages("discord"))
    assert result == []


def test_inactive_platform_link_is_excluded(db):
    _add_user(db, identity_active=False, platform_user_id="dead-link")
    result = run(UserManager().get_users_with_scheduled_messages("discord"))
    assert result == []


def test_user_without_preferred_name_is_excluded(db):
    _add_user(db, name=None, platform_user_id="unonboarded")
    result = run(UserManager().get_users_with_scheduled_messages("discord"))
    assert result == []


def test_deactivate_platform_identity_flips_the_flag(db):
    _add_user(db, platform_user_id="going-dead")
    users = UserManager()

    # eligible before
    assert [pid for _, pid in run(users.get_users_with_scheduled_messages("discord"))] == ["going-dead"]

    run(users.deactivate_platform_identity("discord", "going-dead"))

    # excluded after - the link is off, without touching other platforms
    assert run(users.get_users_with_scheduled_messages("discord")) == []


def test_deactivate_unknown_identity_is_a_safe_noop(db):
    # no rows exist - should not raise
    run(UserManager().deactivate_platform_identity("discord", "nobody"))
