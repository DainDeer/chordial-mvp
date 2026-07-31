"""web auth: chordial is the identity provider.

there are no passwords. the user asks chordial (in chat) for a login code;
the web_login tool mints a one-time LinkCode with purpose='web_login' and
the bot DMs the code plus a tap-to-login link. redeeming the code here
mints a signed session cookie bound to the code's user_uuid. the trusted
channel to the user that every chat platform already provides IS the
authentication factor - same mechanic, and same table, as platform linking.

sessions are stateless HMAC-signed tokens (user_uuid.expires_ts.signature):
no session table, restarts don't log anyone out, and revocation is rotating
WEB_SESSION_SECRET. that trade is deliberate for a companion app's threat
model; per-session revocation is a table when it's ever needed.

redemption is rate-limited per client ip (the code space is ~5e11 with a
15-minute ttl, so brute force is hopeless even before the limiter - the
limiter just makes it loud and cheap).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from src.database.database import get_db
from src.database.models import LinkCode
from src.services.platform_link_service import PlatformLinkService
from src.utils.timezone_utils import utc_now
from config import Config

logger = logging.getLogger(__name__)

SESSION_COOKIE = "chordial_session"

# module-level service, same pattern as link_tools - the ttl knob is shared
# with platform linking on purpose (one mental model: "codes last 15 min")
_links = PlatformLinkService()


def mint_login_code(user_uuid: str) -> str:
    """a one-time web login code for this user (the web_login tool's mint)."""
    return _links.create_code(user_uuid, purpose="web_login")


def login_url(code: str) -> Optional[str]:
    """the tap-to-login link the bot includes in its DM. GET /login?code=...
    only RENDERS the page - the page's script POSTs the redemption - so a
    platform's link-preview prefetch (telegram fetches urls it displays!)
    can never burn the single-use code."""
    if not Config.WEB_PUBLIC_URL:
        return None
    return f"{Config.WEB_PUBLIC_URL}/login?code={code}"


def redeem_login_code(code: str) -> Optional[str]:
    """validate + consume a web_login code; returns its user_uuid, or None.
    single-use: stamped used_at on success."""
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    with get_db() as db:
        row = db.query(LinkCode).filter(LinkCode.code == normalized).first()
        if (row is None or row.used_at is not None
                or row.purpose != "web_login"):
            return None
        if row.expires_at < utc_now():
            return None
        row.used_at = utc_now()
        db.commit()
        logger.info(f"web login code redeemed for user {row.user_uuid}")
        return row.user_uuid


# --- sessions -----------------------------------------------------------------


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# chordial timestamps are NAIVE utc (utc_now()); datetime.timestamp() and
# fromtimestamp() would reinterpret them in the host's local timezone, so the
# epoch math stays explicit instead
_EPOCH = datetime(1970, 1, 1)


def _to_epoch(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds())


def _from_epoch(seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def mint_session(user_uuid: str, *, now: Optional[datetime] = None) -> str:
    """a signed bearer token: user_uuid.expires_ts.sig"""
    secret = Config.WEB_SESSION_SECRET
    if not secret:
        raise RuntimeError("WEB_SESSION_SECRET is not set")
    expires = (now or utc_now()) + timedelta(days=Config.WEB_SESSION_DAYS)
    payload = f"{user_uuid}.{_to_epoch(expires)}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_session(token: Optional[str],
                   *, now: Optional[datetime] = None) -> Optional[str]:
    """the user_uuid a valid unexpired token names, else None. constant-time
    signature check; any malformed shape is just None, never an exception."""
    secret = Config.WEB_SESSION_SECRET
    if not secret or not token:
        return None
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return None
    payload, sig = parts
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        return None
    user_uuid, _, expires_raw = payload.rpartition(".")
    try:
        expires = _from_epoch(int(expires_raw))
    except (ValueError, OverflowError):
        return None
    if (now or utc_now()) >= expires:
        return None
    return user_uuid or None


# --- redemption rate limit ----------------------------------------------------


class RateLimiter:
    """sliding-window attempts per key (client ip). in-memory on purpose:
    a restart forgiving a few attempts is fine; the code space is the real
    defense. bounded so a spray of spoofed ips can't grow memory."""

    def __init__(self, attempts: int = 10, window_seconds: int = 300,
                 max_keys: int = 1024):
        self.attempts = attempts
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if len(hits) >= self.attempts:
            self._hits[key] = hits
            return False
        hits.append(now)
        if key not in self._hits and len(self._hits) >= self.max_keys:
            # drop the stalest key wholesale - crude, bounded, good enough
            oldest = min(self._hits, key=lambda k: self._hits[k][-1])
            del self._hits[oldest]
        self._hits[key] = hits
        return True
