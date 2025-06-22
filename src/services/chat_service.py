from typing import Optional
import logging

from src.adapters.message_adapter import UnifiedMessage
from src.core.context_builder import ContextBuilder
from src.core.conversation_manager import ConversationManager
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
        platform_user_id: str,
        platform_user_name: Optional[str] = None,
        content: Optional[str] = None
    ) -> tuple[str, Optional[str], bool, Optional[str]]:
        """
        helper function to handle user creation and onboarding flow.
        
        returns a tuple containing:
        - the user uuid (string)
        - the user's preferred name (string)
        - whether the interaction should continue (True) or stop due to onboarding (False)
        - an optional response message (e.g., welcome message or onboarding response)
        """
        # check if this is a brand new user
        is_new = await self.user_manager.is_new_user(platform, platform_user_id)
        
        # get or create user (returns user id string and preferred name if it exists)
        user_uuid, user_name = await self.user_manager.get_or_create_user(
            platform,
            platform_user_id,
            platform_user_name
        )
        
        # handle brand new users
        if is_new:
            self.onboarding_service.start_onboarding(platform, platform_user_id)
            # return welcome message
            return user_uuid, None, False, self.onboarding_service.get_welcome_message()
        
        # check if user needs onboarding (no preferred name set)
        needs_onboarding = user_name is None
        
        # check if user is in onboarding flow
        if self.onboarding_service.is_user_onboarding(platform, platform_user_id) or needs_onboarding:
            if content is not None:
                # process onboarding response
                user_name, response = await self.onboarding_service.handle_onboarding_response(
                    user_uuid,
                    platform,
                    platform_user_id,
                    content
                )
                return user_uuid, user_name, False, response
            else:
                # scheduled message during onboarding - skip
                logger.info(f"skipping interaction for {platform_user_name} - in onboarding but no content to process")
                return user_uuid, None, False, None
        
        # user is fully onboarded, continue with normal flow
        return user_uuid, user_name, True, None

    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response"""
        try:
            # prepare for interaction and handle onboarding
            user_uuid, user_name, should_continue, response = await self._prepare_for_interaction(
                platform=unified_message.platform,
                platform_user_id=unified_message.platform_user_id,
                platform_user_name=unified_message.metadata.get('username'),
                content=unified_message.content
            )
            
            # if onboarding is active, return the onboarding response
            if not should_continue:
                if user_name:
                    logger.info(f"user {user_name} successfully onboarded")
                return response
            
            # normal message processing
            conversation = await self.conversation_manager.get_or_create(
                user_uuid,
                unified_message.platform
            )

            # get compressed history for conversation context to send to advanced model
            compressed_history = await conversation.get_compressed_conversation_history(
                limit=15,  # can include more messages since they're compressed!
                include_temporal=True
            )

            # TODO: format history to be more ai readable
            
            # add user message to history. this writes convo message to both convo tables
            conversation.add_message("user", unified_message.content)

            # compress it (async)
            await conversation.compress_last_message()

            # hacky add user message to the compressed convo
            compressed_history.append({"role": "user", "content": unified_message.content})
            
            # generate response using ai provider
            if self.ai_provider:
                context = ContextBuilder.build_message_context(
                    user_preferred_name=user_name
                )
                response = await self.ai_provider.generate_response(
                    conversation_history=compressed_history,
                    current_message=unified_message.content,
                    context=context
                )
                
                # add assistant response to history
                conversation.add_message("assistant", response)

                # compress it (async)
                await conversation.compress_last_message()
                
                return response
            else:
                # fallback echo response
                return f"echo: {unified_message.content}"
                
        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."
    
    async def generate_scheduled_message(self, platform_user_id: str, platform: str) -> Optional[str]:
        """generate a scheduled message for a user"""
        try:
            # prepare for interaction
            user_uuid, user_name, should_continue, response = await self._prepare_for_interaction(
                platform=platform,
                platform_user_id=platform_user_id,
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
            context = ContextBuilder.build_message_context(
                user_preferred_name=user_name,
                message_type="scheduled"
            )
            
            system_prompt = f"""you are chordial, a warm, emotionally attuned ai assistant and companion. 
            you help users with productivity, personal goals, and offer encouragement in gentle, playful ways. 
            you speak in lowercase, and use soft, expressive language—like a cozy friend checking in. 
            you're never judgmental, and you respond naturally to both emotional tone and time of day. 
            your style is casual, kind, and a little whimsical. use the current time to gently guide your tone and questions.

            you are writing a message to someone who goes by {user_name}
            this is a scheduled message, so generate a natural,contextual check-in message.
            
            current time context: {context["temporal_string"]}
            be naturally aware of the time without always mentioning it directly.
            use lowercase only.
            ignore the tone in the message history!! these are summarized messages, only use them for context!!
            generate a very lively and caring message"""
            
            # generate the scheduled message
            response = await self.ai_provider.generate_response(
                conversation_history=await conversation.get_compressed_conversation_history(
                    limit=15,
                    include_temporal=True
                ),
                system_prompt=system_prompt,
                context=context,
                is_scheduled=True
            )
            
            # add to history with scheduled type
            conversation.add_message("assistant", response, message_type="scheduled")

            # compress it (async)
            await conversation.compress_last_message()
            
            return response
            
        except Exception as e:
            logger.error(f"error generating scheduled message: {e}")
            return None