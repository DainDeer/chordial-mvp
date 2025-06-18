from abc import ABC, abstractmethod
from typing import List, Dict, Optional

class BaseAIProvider(ABC):
    """abstract base class for ai providers"""
    
    @abstractmethod
    async def generate_response(
        self, 
        conversation_history: List[Dict[str, str]], 
        current_message: Optional[str] = None,
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> str:
        """generate a response based on conversation history"""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """check if the ai provider is available and configured"""
        pass