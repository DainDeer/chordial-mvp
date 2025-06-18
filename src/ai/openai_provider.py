from openai import AsyncOpenAI
from typing import List, Dict, Optional
import logging

from .base import BaseAIProvider
from config import Config

logger = logging.getLogger(__name__)

class OpenAIProvider(BaseAIProvider):
    """openai/chatgpt provider implementation"""
    
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or Config.OPENAI_API_KEY
        self.model = model or Config.OPENAI_MODEL
        
        self.client = AsyncOpenAI(
            api_key=self.api_key
        )

    async def generate_response(
        self, 
        conversation_history: List[Dict[str, str]], 
        current_message: Optional[str] = None,
        system_prompt: Optional[str] = None,
        is_scheduled: bool = False,
        **kwargs
    ) -> str:
        """generate a response using openai's api"""
        try:
            messages = []
            
            # add system prompt
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            else:
                # default system prompt
                default_prompt = """you are chordial, a helpful and friendly ai assistant. 
                you only use lowercase letters.
                you help users with productivity, provide reminders, and act as a supportive companion.
                keep your responses concise but warm and engaging."""
                messages.append({"role": "system", "content": default_prompt})
            
            # add conversation history
            messages.extend(conversation_history)
            
            # add current message if not a scheduled message
            if current_message and not is_scheduled:
                messages.append({"role": "user", "content": current_message})
            
            # make api call
            response = await self.client.responses.create(
                model=self.model,
                input=messages,
                temperature=0.7
            )
            
            return response.output[0].content[0].text.strip()
            
        except Exception as e:
            logger.error(f"error generating openai response: {e}")
            return "i'm having trouble connecting to my ai service right now. please try again later."
    
    async def is_available(self) -> bool:
        """check if openai is configured and available"""
        if not self.api_key:
            return False
        
        try:
            # try a simple api call to check availability
            await openai.Model.aretrieve(self.model)
            return True
        except:
            return False