from typing import List, Dict, Optional, Any
from datetime import datetime
import logging
import os
import json

from src.managers.memories_manager import MemoriesManager
from src.models.message import Message

logger = logging.getLogger(__name__)

class PromptService:
    """handles all prompt construction for chordial ai interactions"""
    
    def __init__(self, enable_prompt_logging: bool = True):
        # base personality for chordial
        self.base_personality = """you are chordial, a warm, emotionally attuned ai assistant and companion. 
you help users with productivity, personal goals, and offer encouragement in gentle, playful ways. 
you speak in lowercase, and use soft, expressive language—like a cozy friend checking in. 
you're never judgmental, and you respond naturally to both emotional tone and time of day. 
your style is casual, kind, and a little whimsical."""
        
        # prompt logging for debugging/tuning
        self.enable_prompt_logging = enable_prompt_logging
        self.prompt_log_dir = "prompt_logs"

        # memories manager for fetching user memories
        self.memories_manager = MemoriesManager()
        
        # create log directory if logging is enabled
        if self.enable_prompt_logging and not os.path.exists(self.prompt_log_dir):
            os.makedirs(self.prompt_log_dir)
            logger.info(f"created prompt log directory: {self.prompt_log_dir}")

    async def _create_base_system_prompt(
        self,
        user_name: Optional[str] = None,
        user_uuid: Optional[str] = None,
        message_type: str = "conversation",
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """create the base system prompt with user context"""
        
        prompt_parts = [self.base_personality]

        # add user memories if we have a user_uuid
        if user_uuid:
            try:
                memories = await self.memories_manager.get_memories_for_prompt(
                    user_uuid=user_uuid,
                    max_count=10,
                    include_core=True
                )
        
                if memories:
                    prompt_parts.append("\n--- important things to remember about this user ---")
                    for memory in memories:
                        if memory['core']:
                            prompt_parts.append(f"[ALWAYS REMEMBER] {memory['instruction']}")
                        else:
                            prompt_parts.append(f"[{memory['type']}] {memory['instruction']}")
                    prompt_parts.append("--- end of memories ---\n")
                    
            except Exception as e:
                logger.error(f"failed to fetch memories for prompt: {e}")
                # continue without memories if there's an error
        
        # add user-specific instructions
        if user_name:
            if message_type == "scheduled":
                prompt_parts.append(f"\nyou are writing a message to someone who goes by {user_name}")
                prompt_parts.append("this is a scheduled message, so generate a natural, contextual check-in message.")
            else:
                prompt_parts.append(f"\nyou are replying to a message from {user_name}")

        prompt_parts.append("\ninclude it if it feels natural. or especially if sending a scheduled check-in message.")
        # TODO: once the temporal strings are added to each message, add further instructions here
        # thinking of something like: try to use their name if it is a scheduled message or if the user is reaching out after a period of over 3 hours.
        
        # add conversation-specific reminders
        prompt_parts.append("\nignore the tone in the messages labeled SUMMARY, these are summarized messages just for context.")
        prompt_parts.append("generate a very lively and caring message")
        
        return "\n".join(prompt_parts)

    def _add_temporal_context_to_history(
        self,
        conversation_history: List[Message],
        user_name: Optional[str] = None
    ) -> List[Message]:
        """internal helper to add temporal context markers to conversation history"""
        
        if not conversation_history:
            return conversation_history
        
        from src.utils.temporal_context import TemporalContext
        from datetime import datetime
        
        updated_history = []
        now = datetime.now()
        
        for msg in conversation_history:
            # skip system messages
            if msg.role == "system":
                updated_history.append(msg)
                continue
                
            # format the content with temporal context
            formatted_content = TemporalContext.format_message_with_temporal_context(
                content=msg.content,
                role=msg.role,
                message_type=msg.message_type,
                timestamp=msg.timestamp,
                user_name=user_name,
                now=now
            )
            
            # create updated message with temporal context
            updated_msg = Message(
                role=msg.role,
                content=formatted_content,
                timestamp=msg.timestamp,
                message_type=msg.message_type,
                db_id=msg.db_id
            )
                
            updated_history.append(updated_msg)
        
        return updated_history

    async def build_conversation_prompt(
        self,
        conversation_history: List[Message],
        current_message: Optional[str] = None,
        user_name: Optional[str] = None,
        user_uuid: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, str]]:
        """build prompt for regular conversation responses"""
        
        messages = []
        
        # add system prompt
        system_prompt = await self._create_base_system_prompt(
            user_name=user_name,
            user_uuid=user_uuid,
            message_type="conversation",
            context=context
        )
        messages.append({"role": "system", "content": system_prompt})

        # add temporal context to history
        history_with_context = self._add_temporal_context_to_history(
            conversation_history,
            user_name
        )
        
        # convert messages to dicts and add to prompt
        for msg in history_with_context:
            messages.append(msg.to_dict())
        
        # add current message if provided
        if current_message:
            messages.append({
                "role": "user",
                "content": f"{user_name} (now): {current_message}"
            })
        
        # log the prompt
        self._log_prompt(user_name, "conversation", messages)
        
        logger.debug(f"built conversation prompt with {len(messages)} messages")
        return messages

    async def build_scheduled_message_prompt(
        self,
        conversation_history: List[Message],
        user_name: Optional[str] = None,
        user_uuid: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, str]]:
        """build prompt for scheduled check-in messages"""
        
        messages = []
        
        # create a more specific system prompt for scheduled messages
        base_prompt = await self._create_base_system_prompt(
            user_name=user_name,
            user_uuid=user_uuid,
            message_type="scheduled",
            context=context
        )
        
        # add scheduled-specific instructions
        scheduled_prompt = base_prompt + """

for this scheduled message:
- be naturally aware of the time without always mentioning it directly
- reference previous conversations if relevant
- ask open-ended questions that invite sharing
- be encouraging but not pushy
- use lowercase only
- keep it brief but warm"""
        
        messages.append({"role": "system", "content": scheduled_prompt})
        
        # add temporal context to history
        history_with_context = self._add_temporal_context_to_history(
            conversation_history,
            user_name
        )
        
        # convert messages to dicts and add to prompt
        for msg in history_with_context:
            messages.append(msg.to_dict())
        
        # log the prompt
        self._log_prompt(user_name, "scheduled", messages)
        
        logger.debug(f"built scheduled message prompt with {len(messages)} messages")
        return messages

    def build_custom_prompt(
        self,
        system_instructions: str,
        conversation_history: Optional[List[Message]] = None,
        user_message: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """build a custom prompt with specific instructions"""
        
        messages = []
        
        # combine base personality with custom instructions
        full_system_prompt = f"{self.base_personality}\n\n{system_instructions}"
        messages.append({"role": "system", "content": full_system_prompt})
        
        # add conversation history if provided
        if conversation_history:
            messages.extend([msg.to_dict() for msg in conversation_history])
        
        # add user message if provided
        if user_message:
            messages.append({"role": "user", "content": user_message})
        
        return messages
    
    def _log_prompt(
        self,
        user_name: Optional[str],
        prompt_type: str,
        messages: List[Dict[str, str]]
    ):
        """log prompts to file for debugging/tuning purposes"""
        
        if not self.enable_prompt_logging:
            return
        
        try:
            # use a safe filename based on user name or "unknown"
            safe_username = (user_name or "unknown_user").replace(" ", "_").replace("/", "_")
            filename = os.path.join(self.prompt_log_dir, f"prompts_{safe_username}.log")
            
            with open(filename, "a", encoding="utf-8") as f:
                # write separator
                f.write("\n" + "="*80 + "\n")
                
                # write metadata
                f.write(f"timestamp: {datetime.now().isoformat()}\n")
                f.write(f"prompt_type: {prompt_type}\n")
                f.write(f"user: {user_name or 'unknown'}\n")
                f.write(f"message_count: {len(messages)}\n")
                
                # calculate token estimate (rough: ~4 chars per token)
                total_chars = sum(len(msg.get("content", "")) for msg in messages)
                estimated_tokens = total_chars // 4
                f.write(f"estimated_tokens: ~{estimated_tokens}\n")
                
                f.write("-"*40 + "\n\n")
                
                # write the actual messages
                for i, msg in enumerate(messages):
                    f.write(f"[{i}] role: {msg.get('role', 'unknown')}\n")
                    content = msg.get('content', '')
                    # indent content for readability
                    indented_content = '\n'.join(f"    {line}" for line in content.split('\n'))
                    f.write(f"{indented_content}\n\n")
                
                f.write("="*80 + "\n\n")
                
        except Exception as e:
            logger.error(f"failed to log prompt: {e}")
            # don't let logging errors break the main flow
    
    def get_prompt_stats(self, user_name: Optional[str] = None) -> Dict[str, Any]:
        """get statistics about logged prompts for analysis"""
        
        if not self.enable_prompt_logging:
            return {"error": "prompt logging is disabled"}
        
        try:
            if user_name:
                safe_username = user_name.replace(" ", "_").replace("/", "_")
                files = [f"prompts_{safe_username}.log"]
            else:
                # get all log files
                files = [f for f in os.listdir(self.prompt_log_dir) if f.startswith("prompts_") and f.endswith(".log")]
            
            stats = {
                "total_prompts": 0,
                "conversation_prompts": 0,
                "scheduled_prompts": 0,
                "users": set(),
                "total_estimated_tokens": 0
            }
            
            for filename in files:
                filepath = os.path.join(self.prompt_log_dir, filename)
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                        
                    # count prompt types
                    stats["conversation_prompts"] += content.count("prompt_type: conversation")
                    stats["scheduled_prompts"] += content.count("prompt_type: scheduled")
                    stats["total_prompts"] = stats["conversation_prompts"] + stats["scheduled_prompts"]
                    
                    # extract user names
                    import re
                    user_matches = re.findall(r"user: (.+)", content)
                    stats["users"].update(u for u in user_matches if u != "unknown")
                    
                    # sum token estimates
                    token_matches = re.findall(r"estimated_tokens: ~(\d+)", content)
                    stats["total_estimated_tokens"] += sum(int(t) for t in token_matches)
            
            stats["users"] = list(stats["users"])  # convert set to list for json serialization
            return stats
            
        except Exception as e:
            logger.error(f"failed to get prompt stats: {e}")
            return {"error": str(e)}