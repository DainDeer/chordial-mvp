from typing import Dict, Any, Optional
from src.core.temporal_context import TemporalContext

class ContextBuilder:
    @staticmethod
    def build_message_context(
        user_preferred_name: Optional[str] = None,
        temporal_context: Optional[TemporalContext] = None,
        message_type: Optional[str] = None,
        **extra_context
    ) -> Dict[str, Any]:
        """builds a rich context dictionary for ai interactions"""
        
        # get temporal stuff
        if temporal_context is None:
            temporal_context = TemporalContext()
        
        context = {
            "temporal": temporal_context.get_detailed_context(),
            "temporal_string": temporal_context.get_context_string(),
            "special_context": temporal_context.get_special_context(),
        }
        
        # add user info
        if user_preferred_name:
            context["user_name"] = user_preferred_name

        if message_type:
            context["message_type"] = message_type
            
        # add any extra context passed in
        context.update(extra_context)
        
        return context