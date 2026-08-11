"""device auth: the desktop app's identity (docs/ROOMS_DESIGN.md section 10).

same identity-provider mechanic as web login - chordial (in chat, or a
logged-in web session later) mints a one-time LinkCode with
purpose='device_link'; the app redeems it once and receives a bearer token
`<device_uuid>.<secret>` exactly once. only sha256(secret) is stored, so a
database read never yields a usable credential.

devices are the multi-tenant seam: every /api/v1 request resolves its bearer
token to a live device row and acts as that device's user. revocation is a
row stamp (revoked_at), individually per device - no shared secrets, no
rotate-the-world.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional

from src.database.database import get_db
from src.database.models import Device, LinkCode
from src.services.platform_link_service import PlatformLinkService
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

_links = PlatformLinkService()

# how stale last_seen_at may get before an authenticated request refreshes
# it - writing on every request would turn each GET into a DB write
_LAST_SEEN_REFRESH_SECONDS = 300


class DeviceIdentity:
    """what a verified bearer token names: which device, whose reality."""

    __slots__ = ("id", "device_uuid", "user_uuid", "name")

    def __init__(self, id: int, device_uuid: str, user_uuid: str, name: str):
        self.id = id
        self.device_uuid = device_uuid
        self.user_uuid = user_uuid
        self.name = name


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint_device_link_code(user_uuid: str) -> str:
    """a one-time device-link code for this user (chat tool / web UI mint)."""
    return _links.create_code(user_uuid, purpose="device_link")


def link_device(code: str, name: str = "device") -> Optional[tuple[str, str]]:
    """redeem a device_link code; returns (device_uuid, bearer_token) or None.

    consumption is one conditional update (same race-safe mechanic as web
    login): two concurrent redemptions race for used_at and exactly one wins
    a device.
    """
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    now = utc_now()
    with get_db() as db:
        claimed = db.query(LinkCode).filter(
            LinkCode.code == normalized,
            LinkCode.purpose == "device_link",
            LinkCode.used_at.is_(None),
            LinkCode.expires_at >= now,
        ).update({"used_at": now}, synchronize_session=False)
        db.commit()
        if claimed != 1:
            return None
        user_uuid = db.query(LinkCode.user_uuid).filter(
            LinkCode.code == normalized).scalar()

        secret = secrets.token_urlsafe(32)
        device = Device(user_uuid=user_uuid, name=(name or "device")[:80],
                        token_hash=_hash(secret), last_seen_at=now)
        db.add(device)
        db.commit()
        logger.info("device %s linked for user %s",
                    device.device_uuid, user_uuid)
        return device.device_uuid, f"{device.device_uuid}.{secret}"


def verify_device_token(token: Optional[str]) -> Optional[DeviceIdentity]:
    """the device a valid bearer token names, else None. revoked devices
    fail closed; last_seen_at refreshes at most every few minutes."""
    if not token or "." not in token:
        return None
    device_uuid, _, secret = token.partition(".")
    if not device_uuid or not secret:
        return None
    with get_db() as db:
        row = db.query(Device).filter(
            Device.device_uuid == device_uuid,
            Device.revoked_at.is_(None),
        ).first()
        if row is None:
            return None
        if not hmac.compare_digest(row.token_hash, _hash(secret)):
            return None
        now = utc_now()
        if (row.last_seen_at is None or
                (now - row.last_seen_at).total_seconds()
                > _LAST_SEEN_REFRESH_SECONDS):
            row.last_seen_at = now
            db.commit()
        return DeviceIdentity(row.id, row.device_uuid, row.user_uuid, row.name)


def list_devices(user_uuid: str) -> list[dict]:
    """this user's devices, revoked ones included (the UI shows history)."""
    with get_db() as db:
        rows = db.query(Device).filter(
            Device.user_uuid == user_uuid).order_by(Device.created_at).all()
        return [{
            "device_id": r.device_uuid,
            "name": r.name,
            "created_at": r.created_at.isoformat(),
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            "revoked": r.revoked_at is not None,
        } for r in rows]


def revoke_device(user_uuid: str, device_uuid: str) -> bool:
    """revoke one of the user's own devices. scoped: a device can only ever
    revoke devices belonging to the same user. idempotent."""
    with get_db() as db:
        claimed = db.query(Device).filter(
            Device.device_uuid == device_uuid,
            Device.user_uuid == user_uuid,
            Device.revoked_at.is_(None),
        ).update({"revoked_at": utc_now()}, synchronize_session=False)
        db.commit()
        if claimed:
            logger.info("device %s revoked for user %s", device_uuid, user_uuid)
        return bool(claimed)
