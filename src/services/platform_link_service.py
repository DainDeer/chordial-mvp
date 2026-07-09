"""the platform link service: one-time codes that bind a new platform account
to an existing user.

the flow is chat-first: the user asks chordial for a code on their current
platform (the link_platform tool), then hands it to the bot on the new
platform - by tapping the telegram deep link (t.me/<bot>?start=<code>) or
just pasting the code. redeeming it creates/reactivates the PlatformIdentity
so both platforms are one user: same memories, same conversation.

codes are short-lived (15 min default), single-use, and drawn from an
unambiguous alphabet (no I/L/O/0/1 lookalikes) so they survive being read off
one screen and typed into another. unknown strangers on the new platform never
get past the code check - without a valid code there is no user creation, no
onboarding, no api spend.
"""
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Optional

from sqlalchemy.exc import IntegrityError

from src.database.database import get_db
from src.database.models import LinkCode
from src.managers.user_manager import UserManager
from src.utils.timezone_utils import utc_now
from config import Config

logger = logging.getLogger(__name__)

# 29 symbols, no I/L/O (and no 0/1 by construction): 29^8 ≈ 5e11 - unguessable
# at chat scale, comfortably human-typable
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


class LinkResult(Enum):
    LINKED = "linked"        # fresh bind
    RELINKED = "relinked"    # same user, link reactivated (e.g. after a block)
    INVALID = "invalid"      # unknown or already-used code
    EXPIRED = "expired"      # known code, past its ttl
    CONFLICT = "conflict"    # account already bound to a different user


@dataclass
class LinkOutcome:
    result: LinkResult
    user_uuid: Optional[str] = None


class PlatformLinkService:
    def __init__(self, user_manager: Optional[UserManager] = None,
                 ttl_minutes: Optional[int] = None):
        self.user_manager = user_manager or UserManager()
        self.ttl = timedelta(minutes=ttl_minutes if ttl_minutes is not None
                             else Config.LINK_CODE_TTL_MINUTES)

    def create_code(self, user_uuid: str) -> str:
        """mint a fresh single-use code for this user."""
        for _ in range(5):  # unique collision is ~impossible; retry anyway
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
            try:
                with get_db() as db:
                    db.add(LinkCode(
                        code=code,
                        user_uuid=user_uuid,
                        created_at=utc_now(),
                        expires_at=utc_now() + self.ttl,
                    ))
                    db.commit()
                logger.info(f"minted link code for user {user_uuid} (expires in {self.ttl})")
                return code
            except IntegrityError:
                logger.warning("link code collision (!), retrying")
        raise RuntimeError("could not mint a unique link code")

    async def redeem(
        self,
        code: str,
        platform: str,
        platform_user_id: str,
        platform_username: Optional[str] = None,
    ) -> LinkOutcome:
        """validate a code and bind the redeeming platform account to its
        user. single-use: stamped used_at on any successful bind."""
        normalized = (code or "").strip().upper()
        if not normalized:
            return LinkOutcome(LinkResult.INVALID)

        with get_db() as db:
            row = db.query(LinkCode).filter(LinkCode.code == normalized).first()
            if row is None or row.used_at is not None:
                return LinkOutcome(LinkResult.INVALID)
            if row.expires_at < utc_now():
                return LinkOutcome(LinkResult.EXPIRED)
            user_uuid = row.user_uuid

        result = await self.user_manager.link_platform_identity(
            user_uuid, platform, platform_user_id, platform_username,
        )
        if result == "conflict":
            return LinkOutcome(LinkResult.CONFLICT)

        with get_db() as db:
            row = db.query(LinkCode).filter(LinkCode.code == normalized).first()
            if row is not None:
                row.used_at = utc_now()
                db.commit()

        outcome = LinkResult.LINKED if result == "linked" else LinkResult.RELINKED
        logger.info(f"link code redeemed: {platform}:{platform_user_id} -> user {user_uuid} ({outcome.value})")
        return LinkOutcome(outcome, user_uuid=user_uuid)


def deep_link(code: str) -> Optional[str]:
    """the tappable telegram link that both opens the bot AND delivers the
    code via /start payload - satisfying telegram's must-message-first rule
    and the redemption in one tap."""
    if not Config.TELEGRAM_BOT_USERNAME:
        return None
    return f"https://t.me/{Config.TELEGRAM_BOT_USERNAME}?start={code}"
