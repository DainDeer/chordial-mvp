"""the web login flow (src/web/auth.py + the public-mode server paths).

chordial-as-identity-provider: a web_login LinkCode minted in chat redeems
into a signed session cookie; sessions gate every page and api route when
WEB_PUBLIC_URL is set; purposes never cross (a web code can't bind a
platform, a platform code can't log into the web).
"""
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import Base, LinkCode, User
from src.services.platform_link_service import LinkResult, PlatformLinkService
from src.utils.timezone_utils import utc_now
from src.web import auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain", timezone="UTC"))
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


@pytest.fixture()
def public(monkeypatch):
    """the one-switch public deployment: url set, secret set. http here so
    the test client's cookie jar accepts the session cookie (it drops Secure
    cookies over plain http, correctly) - the Secure flag itself is asserted
    in test_https_deployments_set_secure_cookies."""
    monkeypatch.setattr(Config, "WEB_PUBLIC_URL", "http://focus.test")
    monkeypatch.setattr(Config, "WEB_SESSION_SECRET", "test-secret")


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn):
    service = WebService(user_resolver=lambda: U1)
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


# --- sessions -----------------------------------------------------------------


def test_session_round_trip(public):
    token = auth.mint_session(U1)
    assert auth.verify_session(token) == U1


def test_session_expiry_is_utc_epoch_math(public):
    # minted in the past, expired now - naive-utc arithmetic on both sides
    old = utc_now() - timedelta(days=Config.WEB_SESSION_DAYS, minutes=1)
    token = auth.mint_session(U1, now=old)
    assert auth.verify_session(token) is None


def test_session_tamper_and_garbage(public):
    token = auth.mint_session(U1)
    assert auth.verify_session(token.replace(U1, U2)) is None  # re-signed? no
    assert auth.verify_session(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert auth.verify_session("nonsense") is None
    assert auth.verify_session("") is None
    assert auth.verify_session(None) is None


def test_session_requires_a_secret(monkeypatch):
    monkeypatch.setattr(Config, "WEB_SESSION_SECRET", None)
    with pytest.raises(RuntimeError):
        auth.mint_session(U1)
    assert auth.verify_session("anything.1.abc") is None


# --- login codes --------------------------------------------------------------


def test_login_code_round_trip_and_single_use(env, public):
    code = auth.mint_login_code(U1)
    assert auth.redeem_login_code(code.lower()) == U1  # forgiving case
    assert auth.redeem_login_code(code) is None        # single-use


def test_login_code_expiry(env, public):
    code = auth.mint_login_code(U1)
    with db_mod.SessionLocal() as s:
        row = s.query(LinkCode).filter(LinkCode.code == code).first()
        row.expires_at = utc_now() - timedelta(minutes=1)
        s.commit()
    assert auth.redeem_login_code(code) is None


def test_purposes_never_cross(env, public):
    web_code = auth.mint_login_code(U1)
    platform_code = PlatformLinkService().create_code(U1)  # purpose default
    # a platform code is not a web login
    assert auth.redeem_login_code(platform_code) is None
    # a web code pasted to the bot reads as unknown, never a bind
    outcome = _run(PlatformLinkService().redeem(
        web_code, "telegram", "tg-123", "dain"))
    assert outcome.result == LinkResult.INVALID
    # and neither redemption consumed the other's code
    assert auth.redeem_login_code(web_code) == U1


def test_login_url_carries_the_code(public):
    assert auth.login_url("ABCD2345") == "http://focus.test/login?code=ABCD2345"


def test_https_deployments_set_secure_cookies(env, monkeypatch):
    monkeypatch.setattr(Config, "WEB_PUBLIC_URL", "https://focus.test")
    monkeypatch.setattr(Config, "WEB_SESSION_SECRET", "test-secret")
    code = auth.mint_login_code(U1)

    async def flow(client):
        r = await client.post("/api/login/redeem", json={"code": code})
        assert r.status == 200
        raw = r.headers["Set-Cookie"]
        assert "Secure" in raw and "HttpOnly" in raw and "SameSite=Lax" in raw
        return True

    assert _run(_with_client(flow))


# --- the public-mode server ---------------------------------------------------


def test_public_mode_gates_page_and_api(env, public):
    async def flow(client):
        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"
        r = await client.get("/api/today")
        assert r.status == 401
        return True

    assert _run(_with_client(flow))


def test_login_redeem_sets_a_working_session(env, public):
    code = auth.mint_login_code(U2)

    async def flow(client):
        r = await client.post("/api/login/redeem", json={"code": code})
        assert r.status == 200
        # the cookie now rides automatically; the page opens and the api
        # acts as the SESSION's user, not the localhost resolver's
        r = await client.get("/", allow_redirects=False)
        assert r.status == 200
        r = await client.get("/api/today")
        assert r.status == 200
        payload = await r.json()
        assert payload["user"]["name"] == "guest"
        return True

    assert _run(_with_client(flow))


def test_bad_code_is_a_401_not_a_session(env, public):
    async def flow(client):
        r = await client.post("/api/login/redeem", json={"code": "WRONG123"})
        assert r.status == 401
        r = await client.get("/api/today")
        assert r.status == 401
        return True

    assert _run(_with_client(flow))


def test_logout_clears_the_session(env, public):
    code = auth.mint_login_code(U1)

    async def flow(client):
        await client.post("/api/login/redeem", json={"code": code})
        r = await client.post("/api/logout")
        assert r.status == 200
        r = await client.get("/api/today")
        assert r.status == 401
        return True

    assert _run(_with_client(flow))


def test_startup_refuses_public_mode_without_a_secret(monkeypatch):
    monkeypatch.setattr(Config, "WEB_PUBLIC_URL", "https://focus.test")
    monkeypatch.setattr(Config, "WEB_SESSION_SECRET", None)
    with pytest.raises(RuntimeError, match="WEB_SESSION_SECRET"):
        WebService(user_resolver=lambda: U1).build_app()


def test_localhost_mode_needs_no_session(env, monkeypatch):
    monkeypatch.setattr(Config, "WEB_PUBLIC_URL", None)

    async def flow(client):
        r = await client.get("/api/today")
        assert r.status == 200
        # and the login surface politely doesn't exist
        r = await client.post("/api/login/redeem", json={"code": "ANY"})
        assert r.status == 404
        return True

    assert _run(_with_client(flow))


# --- rate limiting ------------------------------------------------------------


def test_rate_limiter_window():
    limiter = auth.RateLimiter(attempts=3, window_seconds=300)
    assert all(limiter.allow("ip-1") for _ in range(3))
    assert not limiter.allow("ip-1")
    assert limiter.allow("ip-2")  # other keys unaffected


def test_redeem_is_rate_limited(env, public):
    async def flow(client):
        service_limiter = None
        for _ in range(12):
            r = await client.post("/api/login/redeem", json={"code": "NOPE1234"})
        assert r.status == 429
        return True

    assert _run(_with_client(flow))


def test_concurrent_redemption_mints_exactly_one_session(env, public):
    """the used_at claim is one conditional update: two racers, one winner."""
    code = auth.mint_login_code(U1)

    async def race():
        return await asyncio.gather(
            asyncio.to_thread(auth.redeem_login_code, code),
            asyncio.to_thread(auth.redeem_login_code, code),
        )

    results = _run(race())
    assert sorted(results, key=lambda r: r or "") == [None, U1]


def test_cross_origin_writes_are_refused(env, public):
    code = auth.mint_login_code(U1)

    async def flow(client):
        # a sibling subdomain with riding cookies gets a 403, even logged in
        await client.post("/api/login/redeem", json={"code": code})
        r = await client.post("/api/focus/pause",
                              headers={"Origin": "https://evil.focus.test"})
        assert r.status == 403
        # the page's own origin sails through
        r = await client.post("/api/focus/pause",
                              headers={"Origin": "http://focus.test"})
        assert r.status == 200
        # non-browser clients send no Origin; csrf doesn't apply to them
        r = await client.post("/api/focus/pause")
        assert r.status == 200
        return True

    assert _run(_with_client(flow))


# --- the tool -----------------------------------------------------------------


def _tool_context(scope="dm"):
    from dainframe.tools.context import ToolContext
    return ToolContext(stream_id=U1, activation_id="act-1", actor="chordial",
                       metadata={"scope": scope})


def test_web_login_tool_mints_code_and_link(env, public):
    from src.services.tools.link_tools import WEB_LOGIN

    result = _run(WEB_LOGIN.handler({}, _tool_context()))
    assert "http://focus.test/login?code=" in result
    code = result.split("login code: ")[1].split(" ")[0]
    assert auth.redeem_login_code(code) == U1


def test_credential_tools_mint_in_any_scope(env, public):
    """7a: the dm-only guard retired with the multi-human crew group. every
    scope is now a private channel whose only reader is the code's owner,
    so bearer codes mint from the app's council room (the tether's whole
    link story) exactly as they do in a dm."""
    from src.services.tools.link_tools import LINK_PLATFORM, WEB_LOGIN

    assert "link code:" in _run(
        LINK_PLATFORM.handler({}, _tool_context(scope="group")))
    assert "login code:" in _run(
        WEB_LOGIN.handler({}, _tool_context(scope="group")))
    # and the dm path is unchanged
    assert "link code:" in _run(LINK_PLATFORM.handler({}, _tool_context()))
    assert "login code:" in _run(WEB_LOGIN.handler({}, _tool_context()))
