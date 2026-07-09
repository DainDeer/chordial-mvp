"""link-code flow tests: minting, redemption, and the identity-binding rules.

the security properties that matter:
- codes are single-use and expire
- a hallucinated/unknown/reused code binds nothing
- a platform account already bound to a DIFFERENT user is never captured
  (conflict -> hard refuse)
- re-linking the SAME user reactivates a dead link (the block-then-return path)
"""
import asyncio
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, User, PlatformIdentity, LinkCode  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.platform_link_service import (  # noqa: E402
    PlatformLinkService, LinkResult, deep_link, _CODE_ALPHABET,
)
from src.utils.timezone_utils import utc_now  # noqa: E402
from config import Config  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="dain"))
        s.add(User(uuid="u2", preferred_name="other"))
        s.commit()
    yield TestSession
    engine.dispose()


def _svc():
    return PlatformLinkService(UserManager(), ttl_minutes=15)


def test_create_and_redeem_links_a_new_platform(db):
    svc = _svc()
    code = svc.create_code("u1")

    assert len(code) == 8
    assert all(c in _CODE_ALPHABET for c in code)

    outcome = run(svc.redeem(code, "telegram", "tg-99", "dain_tg"))
    assert outcome.result == LinkResult.LINKED
    assert outcome.user_uuid == "u1"

    with db() as s:
        identity = s.query(PlatformIdentity).filter_by(
            platform="telegram", platform_user_id="tg-99").one()
        assert identity.user_uuid == "u1"
        assert identity.is_active is True
        assert identity.platform_username == "dain_tg"


def test_redeem_is_case_and_whitespace_tolerant(db):
    svc = _svc()
    code = svc.create_code("u1")
    outcome = run(svc.redeem(f"  {code.lower()} ", "telegram", "tg-99"))
    assert outcome.result == LinkResult.LINKED


def test_code_is_single_use(db):
    svc = _svc()
    code = svc.create_code("u1")
    assert run(svc.redeem(code, "telegram", "tg-99")).result == LinkResult.LINKED
    # second redemption (any platform id) is refused
    assert run(svc.redeem(code, "telegram", "tg-100")).result == LinkResult.INVALID


def test_expired_code_is_refused(db):
    svc = _svc()
    code = svc.create_code("u1")
    with db() as s:
        row = s.query(LinkCode).filter_by(code=code).one()
        row.expires_at = utc_now() - timedelta(minutes=1)
        s.commit()
    assert run(svc.redeem(code, "telegram", "tg-99")).result == LinkResult.EXPIRED


def test_unknown_code_is_invalid(db):
    assert run(_svc().redeem("NOPE1234", "telegram", "tg-99")).result == LinkResult.INVALID
    assert run(_svc().redeem("", "telegram", "tg-99")).result == LinkResult.INVALID


def test_conflict_never_captures_another_users_account(db):
    """u2's code cannot steal a telegram account already bound to u1 - and the
    code survives (not burned by the failed attempt)."""
    svc = _svc()
    first = svc.create_code("u1")
    run(svc.redeem(first, "telegram", "tg-99"))

    second = svc.create_code("u2")
    outcome = run(svc.redeem(second, "telegram", "tg-99"))
    assert outcome.result == LinkResult.CONFLICT

    with db() as s:
        identity = s.query(PlatformIdentity).filter_by(
            platform="telegram", platform_user_id="tg-99").one()
        assert identity.user_uuid == "u1"  # unchanged
        code_row = s.query(LinkCode).filter_by(code=second).one()
        assert code_row.used_at is None    # conflict didn't burn the code


def test_relink_reactivates_a_dead_link(db):
    """blocked the bot, came back: a fresh code for the same user flips the
    deactivated identity back on."""
    svc = _svc()
    users = UserManager()
    run(svc.redeem(svc.create_code("u1"), "telegram", "tg-99"))
    run(users.deactivate_platform_identity("telegram", "tg-99"))

    outcome = run(svc.redeem(svc.create_code("u1"), "telegram", "tg-99", "new_name"))
    assert outcome.result == LinkResult.RELINKED

    with db() as s:
        identity = s.query(PlatformIdentity).filter_by(
            platform="telegram", platform_user_id="tg-99").one()
        assert identity.is_active is True
        assert identity.platform_username == "new_name"


def test_codes_are_unique_rows(db):
    svc = _svc()
    codes = {svc.create_code("u1") for _ in range(20)}
    assert len(codes) == 20  # all distinct


# --- the tool + deep link -------------------------------------------------------

def test_deep_link_shape(monkeypatch):
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", "chordial_bot")
    assert deep_link("ABCD2345") == "https://t.me/chordial_bot?start=ABCD2345"
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", None)
    assert deep_link("ABCD2345") is None


def test_link_platform_tool_output_contains_code_and_link(db, monkeypatch):
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", "chordial_bot")
    from src.services.tools import link_tools

    out = run(link_tools._link_platform({}, "u1"))
    # the code in the output is a real, redeemable row
    with db() as s:
        code = s.query(LinkCode).filter_by(user_uuid="u1").one().code
    assert code in out
    assert f"https://t.me/chordial_bot?start={code}" in out
    assert "expires" in out


def test_registry_gating(monkeypatch):
    from src.services.tools import build_default_registry

    monkeypatch.setattr(Config, "ENABLE_TELEGRAM", False)
    names = [d.name for d in build_default_registry().definitions()]
    assert "link_platform" not in names

    monkeypatch.setattr(Config, "ENABLE_TELEGRAM", True)
    monkeypatch.setattr(Config, "TELEGRAM_BOT_USERNAME", "chordial_bot")
    names = [d.name for d in build_default_registry().definitions()]
    assert "link_platform" in names