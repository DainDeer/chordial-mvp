from typing import Optional, Tuple
import logging

from src.adapters.message_adapter import UnifiedMessage
from src.core.conversation_manager import ConversationManager
from src.core.temporal_context import TemporalContext
from src.core.user_manager import UserManager
from src.services.onboarding_service import OnboardingService

logger = logging.getLogger(__name__)

class ChatService:
    """main service for handling chat interactions across all platforms"""
    
    def __init__(self, ai_provider=None, conversation_manager=None, user_manager=None):
        self.ai_provider = ai_provider
        self.conversation_manager = conversation_manager or ConversationManager()
        self.user_manager = user_manager or UserManager()
        self.onboarding_service = OnboardingService(self.user_manager)
    
    async def _prepare_for_interaction(
        self, 
        platform: str, 
        user_id: str, 
        content: Optional[str] = None,
        username: Optional[str] = None
    ) -> Tuple[str, bool, Optional[str]]:
        """
        helper function to handle user creation and onboarding flow.
        
        returns a tuple containing:
        - the user id (string)
        - whether the interaction should continue (True) or stop due to onboarding (False)
        - an optional response message (e.g., welcome message or onboarding response)
        """
        # check if this is a brand new user
        is_new = await self.user_manager.is_new_user(platform, user_id)
        
        # get or create user (returns user id string)
        user_uuid = await self.user_manager.get_or_create_user(
            platform,
            user_id,
            username
        )
        
        # handle brand new users
        if is_new:
            self.onboarding_service.start_onboarding(platform, user_id)
            # return welcome message
            return user_uuid, False, self.onboarding_service.get_welcome_message()
        
        # check if user needs onboarding (no preferred name set)
        needs_onboarding = await self.user_manager.needs_onboarding(user_uuid)
        
        # check if user is in onboarding flow
        if self.onboarding_service.is_user_onboarding(platform, user_id) or needs_onboarding:
            if content is not None:
                # process onboarding response
                response = await self.onboarding_service.handle_onboarding_response(
                    user_uuid,
                    platform,
                    user_id,
                    content
                )
                return user_uuid, False, response
            else:
                # scheduled message during onboarding - skip
                logger.info(f"skipping interaction for {user_id} - in onboarding but no content to process")
                return user_uuid, False, None
        
        # user is fully onboarded, continue with normal flow
        return user_uuid, True, None

    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response"""
        try:
            # prepare for interaction and handle onboarding
            user_uuid, should_continue, response = await self._prepare_for_interaction(
                platform=unified_message.platform,
                user_id=unified_message.user_id,
                content=unified_message.content,
                username=unified_message.metadata.get('username')
            )
            
            # if onboarding is active, return the onboarding response
            if not should_continue:
                return response
            
            # normal message processing
            conversation = await self.conversation_manager.get_or_create(
                user_uuid,
                unified_message.platform
            )
            
            # add user message to history
            conversation.add_message("user", unified_message.content)
            
            # generate response using ai provider
            if self.ai_provider:
                temporal_context = TemporalContext()
                response = await self.ai_provider.generate_response(
                    conversation.get_history(),
                    unified_message.content,
                    temporal_context=temporal_context
                )
                
                # add assistant response to history
                conversation.add_message("assistant", response)
                
                return response
            else:
                # fallback echo response
                return f"echo: {unified_message.content}"
                
        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."
    
    async def generate_scheduled_message(self, user_id: str, platform: str) -> Optional[str]:
        """generate a scheduled message for a user"""
        try:
            # prepare for interaction
            user_uuid, should_continue, response = await self._prepare_for_interaction(
                platform=platform,
                user_id=user_id,
                content=None  # no content for scheduled messages
            )
            
            # if user is new, return the welcome message
            # if user is in onboarding, return None (skip)
            if not should_continue:
                return response
            
            # normal scheduled message generation
            if not self.ai_provider:
                return None
            
            # get conversation history
            conversation = await self.conversation_manager.get_or_create(
                user_uuid,
                platform
            )
            
            # create a prompt for generating a check-in message
            temporal_context = TemporalContext()
            context_details = temporal_context.get_detailed_context()
            
            system_prompt = f"""you are chordial, a warm, emotionally attuned ai assistant and companion. 
            you help users with productivity, personal goals, and offer encouragement in gentle, playful ways. 
            you speak in lowercase, and use soft, expressive language—like a cozy friend checking in. 
            you're never judgmental, and you respond naturally to both emotional tone and time of day. 
            your style is casual, kind, and a little whimsical. use the current time to gently guide your tone and questions.
            
            current time context: it's {context_details['time_of_day']} on a {context_details['day_type']}.
            be naturally aware of the time without always mentioning it directly.
            use lowercase only."""
            
            # generate the scheduled message
            response = await self.ai_provider.generate_response(
                conversation.get_history(),
                system_prompt=system_prompt,
                temporal_context=temporal_context,
                is_scheduled=True
            )
            
            # don't add scheduled messages to history unless user responds
            # this prevents cluttering the conversation with one-sided check-ins
            
            return response
            
        except Exception as e:
            logger.error(f"error generating scheduled message: {e}")
            return None