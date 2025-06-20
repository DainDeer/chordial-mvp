from openai import AsyncOpenAI
from openai.resources.models import AsyncModels
from typing import List, Dict, Optional
import logging
import json

from .base import BaseAIProvider
from src.core.temporal_context import TemporalContext
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
        temporal_context: Optional[TemporalContext] = None,
        is_scheduled: bool = False,
        **kwargs
    ) -> str:
        """generate a response using openai's api"""
        try:
            messages = []

            # get temporal context if not provided
            if temporal_context is None:
                temporal_context = self.get_temporal_context()
            
            # add system prompt
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            else:
                # default system prompt with temporal awareness
                context_info = temporal_context.get_context_string()
                special_context = temporal_context.get_special_context()

                default_prompt = f"""you are chordial, a warm, emotionally attuned ai assistant and companion. 
            you help users with productivity, personal goals, and offer encouragement in gentle, playful ways. 
            you speak in lowercase, and use soft, expressive language—like a cozy friend checking in. 
            you're never judgmental, and you respond naturally to both emotional tone and time of day. 
            your style is casual, kind, and a little whimsical. use the current time to gently guide your tone and questions.
                
                current context: {context_info}
                {special_context if special_context else ""}
                
                use this temporal awareness naturally in your responses when relevant, but don't always mention the time."""
                messages.append({"role": "system", "content": default_prompt})
            
            # add conversation history
            messages.extend(conversation_history)

            # make api call
            logger.info("sending to openai api:")
            logger.info(f"messages: {json.dumps(messages, indent=2)}")
            
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
            await AsyncModels.retrieve(self.model)
            return True
        except:
            return False