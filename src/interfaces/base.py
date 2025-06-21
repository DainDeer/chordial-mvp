from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

class BaseInterface(ABC):
    """abstract base class for all chat interfaces (discord, telegram, web, etc)"""
    
    def __init__(self, chat_service):
        self.chat_service = chat_service
    
    @abstractmethod
    async def start(self):
        """start the interface"""
        pass
    
    @abstractmethod
    async def stop(self):
        """stop the interface"""
        pass
    
    @abstractmethod
    async def send_message(self, platform_user_id: str, content: str, **kwargs) -> bool:
        """send a message to a user"""
        pass
    
    @abstractmethod
    async def handle_incoming_message(self, message: Any):
        """handle an incoming message from the platform"""
        pass