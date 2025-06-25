from abc import ABC, abstractmethod
from typing import Any, List, Dict, Optional

class BaseAIProvider(ABC):
    """abstract base class for ai providers"""
    
    @abstractmethod
    async def generate_response(
        self, 
        messages: List[Dict[str, str]],  # simplified: just the message list
        **kwargs  # for any provider-specific options
    ) -> str:
        """generate a response based on the provided messages"""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """check if the ai provider is available and configured"""
        pass