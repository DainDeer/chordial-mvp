from openai import AsyncOpenAI
from openai.resources.models import AsyncModels
from typing import Any, List, Dict, Optional
import logging
import json

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
        messages: List[Dict[str, str]],  # simplified: just takes the messages directly
        **kwargs
    ) -> str:
        """generate a response using openai's api"""
        try:
            # log what we're sending
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