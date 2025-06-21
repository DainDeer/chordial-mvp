from typing import Optional
import logging

from src.core.user_manager import UserManager

logger = logging.getLogger(__name__)

class OnboardingService:
    """handles new user onboarding flow"""
    
    def __init__(self, user_manager: UserManager):
        self.user_manager = user_manager
        self.onboarding_states = {}  # track where users are in onboarding
    
    def get_welcome_message(self) -> str:
        """get the initial welcome message"""
        return """hey there! welcome to chordial! 🎵 
        
i'm your new ai companion, here to help with productivity, reminders, and just being a friendly presence.

first things first - what would you like me to call you? just type your preferred name!"""
    
    async def handle_onboarding_response(self, user_uuid: str, platform: str, platform_user_id: str, response: str) -> str:
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
            
            # move to next state (could expand this later)
            del self.onboarding_states[state_key]  # remove from onboarding!
            
            return f"""nice to meet you, {preferred_name}! 💕
            
i'll remember that and use it when we chat. 

i'm here to help you stay productive and check in on you throughout the day. i'll send you gentle reminders and be here whenever you want to talk.

feel free to message me anytime - whether you need help with something, want to chat, or just need a friendly check-in!

ready to get started? just say hi or ask me anything! ✨"""
        
        # shouldn't get here but just in case
        return None
    
    def is_user_onboarding(self, platform: str, platform_user_id: str) -> bool:
        """check if user is currently in onboarding flow"""
        state_key = f"{platform}:{platform_user_id}"
        return state_key in self.onboarding_states
    
    def start_onboarding(self, platform: str, platform_user_id: str):
        """start onboarding for a user"""
        state_key = f"{platform}:{platform_user_id}"
        self.onboarding_states[state_key] = "name"