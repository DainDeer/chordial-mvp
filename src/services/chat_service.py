from typing import Optional
import logging

from src.adapters.message_adapter import UnifiedMessage
from src.core.conversation_manager import ConversationManager

logger = logging.getLogger(__name__)

class ChatService:
    """main service for handling chat interactions across all platforms"""
    
    def __init__(self, ai_provider=None, conversation_manager=None):
        self.ai_provider = ai_provider
        self.conversation_manager = conversation_manager or ConversationManager()
    
    async def process_message(self, unified_message: UnifiedMessage) -> Optional[str]:
        """process an incoming message and generate a response"""
        try:
            # get or create conversation for this user
            conversation = await self.conversation_manager.get_or_create(
                unified_message.user_id,
                unified_message.platform
            )
            
            # add message to conversation history
            conversation.add_message("user", unified_message.content)
            
            # generate response using ai provider
            if self.ai_provider:
                response = await self.ai_provider.generate_response(
                    conversation.get_history(),
                    unified_message.content
                )
                
                # add ai response to conversation history
                conversation.add_message("assistant", response)
                
                return response
            else:
                # fallback when no ai provider is configured yet
                return f"echo: {unified_message.content}"
                
        except Exception as e:
            logger.error(f"error processing message: {e}")
            return "sorry, i encountered an error processing your message."
    
    async def generate_scheduled_message(self, user_id: str, platform: str = "discord") -> Optional[str]:
        """generate a scheduled message for a user"""
        try:
            if not self.ai_provider:
                return None
            
            # get user's conversation history
            conversation = await self.conversation_manager.get_or_create(user_id, platform)
            
            # create a prompt for generating a check-in message
            system_prompt = """you are chordial, a friendly ai assistant that checks in on users periodically.
            you only use lowercase letters.
            based on the conversation history, generate a natural, contextual check-in message.
            keep it brief and friendly. you might ask about their progress on something they mentioned,
            share a relevant tip, or just say hello in a personalized way."""
            
            # generate the scheduled message
            response = await self.ai_provider.generate_response(
                conversation.get_history(),
                system_prompt=system_prompt,
                is_scheduled=True
            )
            
            return response
            
        except Exception as e:
            logger.error(f"error generating scheduled message: {e}")
            return None