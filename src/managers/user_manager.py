from typing import Optional, Dict, Any, List
import logging
from sqlalchemy.orm import Session

from src.database.models import User, PlatformIdentity
from src.database.database import get_db

logger = logging.getLogger(__name__)

class UserManager:
    """manages user data across platforms"""
    
    async def get_or_create_user(self, platform: str, platform_user_id: str, platform_username: Optional[str] = None) -> tuple[str,str]:
        """get existing user or create new one, returns (user_uuid,user_name)"""
        with get_db() as db:
            # check if platform identity exists
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            if identity:
                # make sure we load the user relationship
                if identity.user_uuid:
                    user = db.query(User).filter(User.uuid == identity.user_uuid).first()
                    if user:
                        logger.info(f"found existing user {user.uuid} with name '{user.preferred_name}'")
                        return user.uuid, user.preferred_name
                    else:
                        logger.error(f"identity has user_id {identity.user_uuid} but user not found!")
                        # fall through to create new user
                else:
                    logger.error(f"identity exists but has no user_uuid!")
            
            # create new user
            new_user = User()
            db.add(new_user)
            db.flush()  # flush to get the id before creating identity
            
            # create platform identity with proper user relationship
            new_identity = PlatformIdentity(
                user_uuid=new_user.uuid,
                user=new_user,  # set the relationship directly
                platform=platform,
                platform_user_id=platform_user_id,
                platform_username=platform_username
            )
            db.add(new_identity)
            
            db.commit()
            user_uuid = new_user.uuid  # grab the id before session closes
            logger.info(f"created new user {user_uuid} for {platform}:{platform_user_id}")
            
            return user_uuid, None
    
    async def is_new_user(self, platform: str, platform_user_id: str) -> bool:
        """check if this is a new user (no identity exists yet)"""
        with get_db() as db:
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            # they're new ONLY if no identity exists at all
            is_new = identity is None
            logger.info(f"user {platform}:{platform_user_id} is {'new' if is_new else 'existing'}")
            return is_new
    
    async def update_user_preferences(self, user_uuid: str, preferences: Dict[str, Any]):
        """update user preferences"""
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            
            if not user:
                logger.error(f"user {user_uuid} not found")
                return
            
            # update allowed fields
            if 'preferred_name' in preferences:
                user.preferred_name = preferences['preferred_name']
            
            if 'timezone' in preferences:
                user.timezone = preferences['timezone']
            
            if 'schedule_preferences' in preferences:
                user.schedule_preferences = preferences['schedule_preferences']
            
            if 'bot_personality' in preferences:
                user.bot_personality = preferences['bot_personality']
            
            db.commit()
            logger.info(f"updated preferences for user {user_uuid}")
    
    async def needs_onboarding(self, user_uuid: str) -> bool:
        """check if user needs to complete onboarding (hasn't set preferred name)"""
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            if user:
                return user.preferred_name is None
            return True

    async def get_user_timezone(self, user_uuid: str) -> str:
        """get a user's timezone, defaulting to UTC if unset or user not found"""
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            if user and user.timezone:
                return user.timezone
            return "UTC"

    async def get_user_profile(self, user_uuid: str) -> tuple[Optional[str], str]:
        """(preferred_name, timezone) in one query - what a briefing needs."""
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            if user is None:
                return None, "UTC"
            return user.preferred_name, user.timezone or "UTC"

    async def get_scheduled_users(self) -> List[str]:
        """distinct user_uuids eligible for proactive sends: the human is
        active, not a test/seed account, onboarded, and reachable on at least
        one still-deliverable platform link. one entry per USER (a person on
        discord AND telegram is one person, not two schedule slots)."""
        with get_db() as db:
            rows = db.query(PlatformIdentity.user_uuid).join(User).filter(
                PlatformIdentity.is_active == True,   # ≥1 link hasn't hard-failed
                User.is_active == True,               # human is active
                User.is_test == False,                # not a synthetic/seed row
                User.preferred_name != None           # completed onboarding
            ).distinct().all()
            return [user_uuid for (user_uuid,) in rows]

    async def resolve_delivery_identity(
        self,
        user_uuid: str,
        preferred_platform: Optional[str],
        allowed_platforms: Optional[List[str]] = None,
    ) -> Optional[tuple[str, str]]:
        """where to actually deliver a proactive message: the preferred (i.e.
        most recently used) platform's active link if it exists, else the most
        recently created other active link - going silent on a user who is
        reachable elsewhere is worse than check-in on their other platform.
        `allowed_platforms` restricts to platforms with a live interface.
        returns (platform, platform_user_id) or None."""
        with get_db() as db:
            query = db.query(PlatformIdentity).filter(
                PlatformIdentity.user_uuid == user_uuid,
                PlatformIdentity.is_active == True,
            )
            if allowed_platforms is not None:
                query = query.filter(PlatformIdentity.platform.in_(allowed_platforms))
            identities = query.order_by(PlatformIdentity.id.desc()).all()

            if not identities:
                return None
            for identity in identities:
                if identity.platform == preferred_platform:
                    return identity.platform, identity.platform_user_id
            newest = identities[0]
            return newest.platform, newest.platform_user_id

    async def get_identity(self, user_uuid: str, platform: str) -> Optional[tuple[str, bool]]:
        """(platform_user_id, is_active) for this user's link on a platform,
        or None if they've never been linked there."""
        with get_db() as db:
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.user_uuid == user_uuid,
                PlatformIdentity.platform == platform,
            ).first()
            if identity is None:
                return None
            return identity.platform_user_id, bool(identity.is_active)

    async def deactivate_platform_identity(self, platform: str, platform_user_id: str) -> None:
        """mark a single platform link as undeliverable. called when an outbound
        send hard-fails (e.g. discord 404 unknown-user / 403 forbidden) so the
        scheduler stops paying to generate messages for a dead channel. the user
        stays active and reachable on any other platforms they're linked on."""
        with get_db() as db:
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            if identity is None:
                logger.warning(
                    f"cannot deactivate unknown identity {platform}:{platform_user_id}"
                )
                return
            if not identity.is_active:
                return  # already off, nothing to do
            identity.is_active = False
            db.commit()
            logger.info(
                f"deactivated undeliverable identity {platform}:{platform_user_id} "
                f"(user {identity.user_uuid})"
            )