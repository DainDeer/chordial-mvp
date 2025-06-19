from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from src.core.temporal_context import TemporalContext

class BaseAIProvider(ABC):
    """abstract base class for ai providers"""
    
    def get_temporal_context(self) -> TemporalContext:
        """get current temporal context"""
        return TemporalContext()
    
    @abstractmethod
    async def generate_response(
        self, 
        conversation_history: List[Dict[str, str]], 
        current_message: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temporal_context: Optional[TemporalContext] = None,
        **kwargs
    ) -> str:
        """generate a response based on conversation history"""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """check if the ai provider is available and configured"""
        pass