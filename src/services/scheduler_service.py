from datetime import datetime, timedelta
from typing import Dict, Optional, List
import asyncio
import logging

from src.core.user_manager import UserManager
from src.services.chat_service import ChatService
from src.database.database import get_db
from src.database.models import ConversationHistory
from config import Config

logger = logging.getLogger(__name__)

class ScheduledMessageContext:
    """tracks scheduling context for each user"""
    def __init__(self, user_id: str, platform: str):
        self.user_id = user_id
        self.platform = platform
        self.last_scheduled_at: Optional[datetime] = None
        self.last_message_was_scheduled: bool = False
        self.next_scheduled_time: Optional[datetime] = None

class SchedulerService:
    """handles intelligent scheduling of messages across all platforms"""
    
    def __init__(self, chat_service: ChatService, user_manager: UserManager):
        self.chat_service = chat_service
        self.user_manager = user_manager
        self.user_contexts: Dict[str, ScheduledMessageContext] = {}
        self.default_interval_minutes = Config.DM_INTERVAL_MINUTES
        self.delay_after_ignored_hours = 24  # delay 24h if message was ignored
        self.quiet_hours_start = 21  # 9 PM
        self.quiet_hours_end = 8   # 8 AM
        self._running = False
    
    def _get_context_key(self, user_id: str, platform: str) -> str:
        """generate unique key for user context"""
        return f"{platform}:{user_id}"
    
    def _get_or_create_context(self, user_id: str, platform: str) -> ScheduledMessageContext:
        """get or create scheduling context for a user"""
        key = self._get_context_key(user_id, platform)
        if key not in self.user_contexts:
            self.user_contexts[key] = ScheduledMessageContext(user_id, platform)
        return self.user_contexts[key]
    
    async def _check_last_message(self, user_id: str, platform: str) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
        """check who sent the last message, when, and what type"""
        with get_db() as db:
            last_message = db.query(ConversationHistory).filter(
                ConversationHistory.user_id == user_id,
                ConversationHistory.platform == platform
            ).order_by(ConversationHistory.created_at.desc()).first()
            
            if last_message:
                message_type = last_message.context.get('message_type', 'conversation') if last_message.context else 'conversation'
                return last_message.role, last_message.created_at, message_type
            return None, None, None
    
    def _is_quiet_hours(self) -> bool:
        """check if it's currently quiet hours (after 9pm or before 8am)"""
        current_hour = datetime.now().hour
        return current_hour >= self.quiet_hours_start or current_hour < self.quiet_hours_end
    
    async def should_send_scheduled_message(self, user_id: str, platform: str) -> bool:
        """determine if we should send a scheduled message to this user now"""
        context = self._get_or_create_context(user_id, platform)
        now = datetime.now()
        
        # check if user has completed onboarding
        needs_onboarding = await self.user_manager.needs_onboarding(user_id)
        if needs_onboarding:
            logger.debug(f"user {user_id} needs onboarding, skipping scheduled message")
            return False
        
        # check last message in conversation
        last_role, last_message_time, last_message_type = await self._check_last_message(user_id, platform)
        
        # if no messages yet, send one
        if not last_role:
            logger.info(f"no messages found for {user_id}, sending first scheduled message")
            return True
        
        # calculate time since last message
        time_since_last = now - last_message_time if last_message_time else timedelta(hours=999)
        
        # if last message was a scheduled message (ignored by user)
        if last_role == "assistant" and last_message_type == "scheduled":
            # check if it's been 24 hours
            if time_since_last < timedelta(hours=self.delay_after_ignored_hours):
                logger.debug(f"last message was scheduled and sent {time_since_last} ago, waiting 24h")
                return False
            else:
                logger.info(f"24h passed since ignored scheduled message for {user_id}")
                return True
        
        # last message was from user or was a conversation response
        else:
            # check if we're in quiet hours
            if self._is_quiet_hours():
                logger.debug(f"in quiet hours, not sending scheduled message to {user_id}")
                return False
            
            # check if enough time has passed for regular interval
            if time_since_last >= timedelta(minutes=self.default_interval_minutes):
                logger.info(f"regular interval passed for active user {user_id}")
                return True
            else:
                logger.debug(f"only {time_since_last} since last user message, waiting")
                return False
    
    async def send_scheduled_message(self, user_id: str, platform: str, platform_user_id: str) -> Optional[str]:
        """send a scheduled message if appropriate"""
        if not await self.should_send_scheduled_message(user_id, platform):
            return None
        
        # generate the message
        message = await self.chat_service.generate_scheduled_message(user_id, platform)
        
        if message:
            context = self._get_or_create_context(user_id, platform)
            context.last_scheduled_at = datetime.now()
            logger.info(f"generated scheduled message for {user_id}: {message[:50]}...")
        
        return message
    
    async def get_users_for_scheduling(self, platform: str) -> List[tuple[str, str]]:
        """get list of (user_id, platform_user_id) tuples for users who might need scheduled messages"""
        platform_user_ids = await self.user_manager.get_users_with_scheduled_messages(platform)
        
        # we need to map platform user ids back to our internal user ids
        # this is a bit inefficient but works for now
        user_mappings = []
        with get_db() as db:
            from src.database.models import PlatformIdentity
            for platform_user_id in platform_user_ids:
                identity = db.query(PlatformIdentity).filter(
                    PlatformIdentity.platform == platform,
                    PlatformIdentity.platform_user_id == platform_user_id
                ).first()
                
                if identity and identity.user_id:
                    user_mappings.append((identity.user_id, platform_user_id))
        
        return user_mappings
    
    async def run_scheduling_loop(self, platforms: List[str], message_callback):
        """main scheduling loop that checks all users across platforms"""
        self._running = True
        check_interval = 60  # check every minute
        
        while self._running:
            try:
                for platform in platforms:
                    user_mappings = await self.get_users_for_scheduling(platform)
                    
                    for user_id, platform_user_id in user_mappings:
                        message = await self.send_scheduled_message(user_id, platform, platform_user_id)
                        if message:
                            # use the callback to actually send the message
                            await message_callback(platform, platform_user_id, message)
                
            except Exception as e:
                logger.error(f"error in scheduling loop: {e}")
            
            await asyncio.sleep(check_interval)
    
    def stop(self):
        """stop the scheduling loop"""
        self._running = False