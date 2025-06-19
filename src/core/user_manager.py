from typing import Optional, Dict, Any
import logging
from sqlalchemy.orm import Session

from src.database.models import User, PlatformIdentity
from src.database.database import get_db

logger = logging.getLogger(__name__)

class UserManager:
    """manages user data across platforms"""
    
    async def get_or_create_user(self, platform: str, platform_user_id: str, platform_username: Optional[str] = None) -> User:
        """get existing user or create new one"""
        with get_db() as db:
            # check if platform identity exists
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            if identity:
                # user exists, return them
                return identity.user
            
            # create new user
            new_user = User()
            db.add(new_user)
            
            # create platform identity
            new_identity = PlatformIdentity(
                user_id=new_user.id,
                platform=platform,
                platform_user_id=platform_user_id,
                platform_username=platform_username
            )
            db.add(new_identity)
            
            db.commit()
            logger.info(f"created new user {new_user.id} for {platform}:{platform_user_id}")
            
            return new_user
    
    async def is_new_user(self, platform: str, platform_user_id: str) -> bool:
        """check if this is a new user"""
        with get_db() as db:
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            # they're new if no identity exists OR if they haven't set a name yet
            if not identity:
                return True
            
            user = identity.user
            return user.preferred_name is None
    
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
    
    async def get_user_by_platform(self, platform: str, platform_user_id: str) -> Optional[User]:
        """get user by their platform identity"""
        with get_db() as db:
            identity = db.query(PlatformIdentity).filter(
                PlatformIdentity.platform == platform,
                PlatformIdentity.platform_user_id == platform_user_id
            ).first()
            
            return identity.user if identity else None