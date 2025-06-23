from datetime import datetime
from typing import Dict, Any, Optional
from src.core.temporal_context import TemporalContext

class ContextBuilder:
    @staticmethod
    def build_message_context(
        user_preferred_name: Optional[str] = None,
        timestamp: datetime = datetime.now(),
        message_type: str = "conversation",
        **extra_context
    ) -> Dict[str, Any]:
        """builds a rich context dictionary for ai interactions"""
        
        context = {
            "temporal": TemporalContext.get_detailed_context(timestamp),
            "temporal_string": TemporalContext.get_context_string(timestamp),
            "special_context": TemporalContext.get_special_context(timestamp),
        }
        
        # add user info
        if user_preferred_name:
            context["user_name"] = user_preferred_name

        if message_type:
            context["message_type"] = message_type
            
        # add any extra context passed in
        context.update(extra_context)
        
        return context