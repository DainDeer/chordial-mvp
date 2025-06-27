from typing import Optional
import logging

from src.core.user_manager import UserManager
from src.core.memories_manager import MemoriesManager, MemoryType, MemorySource

logger = logging.getLogger(__name__)

class OnboardingService:
    """handles new user onboarding flow"""
    
    def __init__(self, user_manager: UserManager):
        self.user_manager = user_manager
        self.memories_manager = MemoriesManager()
        self.onboarding_states = {}  # track where users are in onboarding
    
    def get_welcome_message(self) -> str:
        """get the initial welcome message"""
        return """hey there! welcome to chordial! 🎵 
        
i'm your new ai companion, here to help with productivity, reminders, and just being a friendly presence.

first things first - what would you like me to call you? just type your preferred name!"""
    
    async def handle_onboarding_response(self, user_uuid: str, platform: str, platform_user_id: str, response: str) -> tuple[Optional[str], str]:
        """handle responses during onboarding"""
        state_key = f"{platform}:{platform_user_id}"
        current_state = self.onboarding_states.get(state_key, "name")
        
        if current_state == "name":
            # they just gave us their name
            preferred_name = response.strip()
            
            # update user preferences
            await self.user_manager.update_user_preferences(user_uuid, {
                'preferred_name': preferred_name
            })
            
            # move to memory state
            self.onboarding_states[state_key] = "memory"
            
            return preferred_name, f"""nice to meet you, {preferred_name}! 💕
            
before we get started, i'd love to learn something special about you!

what's something you want me to always remember about you? it could be anything - how you like to be treated, something about who you are, or anything else that's important to you! 🌟"""
        
        elif current_state == "memory":
            # they just told us what to remember
            memory_content = response.strip()
            
            # create the ai instruction from their response
            ai_instruction = f"Remember this about the user: {memory_content}"
            
            # save as core memory
            try:
                await self.memories_manager.create_memory(
                    user_uuid=user_uuid,
                    ai_instruction=ai_instruction,
                    memory_type=MemoryType.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    keywords=["identity", "core", "onboarding"],
                    core=True,
                    memory_metadata={
                        "onboarding": True,
                        "original_response": memory_content
                    }
                )
                logger.info(f"created core memory for user {user_uuid}: {memory_content[:50]}...")
            except Exception as e:
                logger.error(f"failed to create core memory: {e}")
                # don't break onboarding if memory fails
            
            # onboarding complete!
            del self.onboarding_states[state_key]
            
            # get the user's name for the response
            _, user_name = await self.user_manager.get_or_create_user(platform, platform_user_id)
            
            return user_name, f"""got it! i'll always remember that about you 💖 *makes a special note*
            
i'm here to help you stay productive and check in on you throughout the day. i'll send you gentle reminders and be here whenever you want to talk.

feel free to message me anytime - whether you need help with something, want to chat, or just need a friendly check-in!

ready to get started? just say hi or ask me anything! ✨"""
        
        # shouldn't get here but just in case
        return None, "hmm, something went wrong with onboarding. let's start fresh - what would you like me to call you?"
    
    def is_user_onboarding(self, platform: str, platform_user_id: str) -> bool:
        """check if user is currently in onboarding flow"""
        state_key = f"{platform}:{platform_user_id}"
        return state_key in self.onboarding_states
    
    def start_onboarding(self, platform: str, platform_user_id: str):
        """start onboarding for a user"""
        state_key = f"{platform}:{platform_user_id}"
        self.onboarding_states[state_key] = "name"