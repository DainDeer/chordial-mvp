from typing import Optional, Dict, Any
import logging
from sqlalchemy.orm import Session

from src.database.models import User, PlatformIdentity
from src.database.database import get_db

logger = logging.getLogger(__name__)

class UserManager:
    """manages user data across platforms"""
    
    async def get_or_create_user(self, platform: str, platform_user_id: str, platform_username: Optional[str] = None) -> str:
        """get existing user or create new one, returns user id"""
        with get_db() as db:
            # check if platform identity exists
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            if identity:
                # make sure we load the user relationship
                if identity.user_id:
                    user = db.query(User).filter(User.id == identity.user_id).first()
                    if user:
                        logger.info(f"found existing user {user.id} with name '{user.preferred_name}'")
                        return user.id
                    else:
                        logger.error(f"identity has user_id {identity.user_id} but user not found!")
                        # fall through to create new user
                else:
                    logger.error(f"identity exists but has no user_id!")
            
            # create new user
            new_user = User()
            db.add(new_user)
            db.flush()  # flush to get the id before creating identity
            
            # create platform identity with proper user relationship
            new_identity = PlatformIdentity(
                user_id=new_user.id,
                user=new_user,  # set the relationship directly
                platform=platform,
                platform_user_id=platform_user_id,
                platform_username=platform_username
            )
            db.add(new_identity)
            
            db.commit()
            user_id = new_user.id  # grab the id before session closes
            logger.info(f"created new user {user_id} for {platform}:{platform_user_id}")
            
            return user_id
    
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
    
    async def update_user_preferences(self, user_id: str, preferences: Dict[str, Any]):
        """update user preferences"""
        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            
            if not user:
                logger.error(f"user {user_id} not found")
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
            logger.info(f"updated preferences for user {user_id}")
    
    async def needs_onboarding(self, user_id: str) -> bool:
        """check if user needs to complete onboarding (hasn't set preferred name)"""
        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return user.preferred_name is None
            return True
    
    async def get_users_with_scheduled_messages(self, platform: str) -> list[str]:
        """get all platform user ids who have scheduled messages enabled"""
        with get_db() as db:
            # query for active users on this platform
            identities = db.query(PlatformIdentity).join(User).filter(
                PlatformIdentity.platform == platform,
                User.is_active == True,
                User.preferred_name != None  # only users who completed onboarding
            ).all()
            
            return [identity.platform_user_id for identity in identities]