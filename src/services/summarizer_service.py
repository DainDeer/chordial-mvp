import logging
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from sqlalchemy import and_

from src.database.database import get_db
from src.database.models import ConversationHistory, ConversationSummary, User
from src.providers.ai.openai_provider import OpenAIProvider
from config import Config

logger = logging.getLogger(__name__)

class SummarizerService:
    """handles intelligent summarization of conversation history"""
    
    def __init__(self, summary_model: str = "gpt-3.5-turbo"):
        # use a cheaper model for summaries
        self.summarizer = OpenAIProvider(model=summary_model)
        self.min_messages_to_summarize = 20  # don't summarize until we have at least this many
        self.max_messages_per_summary = 50   # summarize in chunks of this size
        
    async def get_last_summary_position(self, user_uuid: str, platform: str) -> Optional[int]:
        """get the last message id that was summarized for this user"""
        with get_db() as db:
            last_summary = db.query(ConversationSummary).filter(
                ConversationSummary.user_uuid == user_uuid,
                ConversationSummary.platform == platform
            ).order_by(ConversationSummary.last_message_id.desc()).first()
            
            return last_summary.last_message_id if last_summary else None
    
    async def get_unsummarized_messages(
        self, 
        user_uuid: str, 
        platform: str
    ) -> Tuple[List[ConversationHistory], int, int]:
        """get messages that haven't been summarized yet"""
        with get_db() as db:
            last_summarized_id = await self.get_last_summary_position(user_uuid, platform)
            
            query = db.query(ConversationHistory).filter(
                ConversationHistory.user_uuid == user_uuid,
                ConversationHistory.platform == platform
            )
            
            if last_summarized_id:
                query = query.filter(ConversationHistory.id > last_summarized_id)
            
            messages = query.order_by(ConversationHistory.created_at.asc()).all()
            
            if not messages:
                return [], 0, 0
            
            first_id = messages[0].id
            last_id = messages[-1].id
            
            return messages, first_id, last_id
    
    async def should_summarize(self, user_uuid: str, platform: str) -> bool:
        """check if we should create a new summary for this user"""
        messages, _, _ = await self.get_unsummarized_messages(user_uuid, platform)
        return len(messages) >= self.min_messages_to_summarize
    
    async def create_summary_prompt(self, messages: List[ConversationHistory]) -> str:
        """create the prompt for the summarizer"""
        conversation_text = []
        
        for msg in messages:
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            role = "User" if msg.role == "user" else "Chordial"
            conversation_text.append(f"[{timestamp}] {role}: {msg.content}")
        
        prompt = f"""Please create a concise summary of this conversation between a user and Chordial (an AI assistant).
Focus on:
1. Key topics discussed
2. Any goals or tasks mentioned
3. The user's emotional state or mood
4. Important information about the user
5. Any plans or commitments made

Keep the summary brief but informative (2-3 paragraphs max).

Conversation:
{chr(10).join(conversation_text)}

Summary:"""
        
        return prompt
    
    async def extract_key_topics(self, summary: str, messages: List[ConversationHistory]) -> List[str]:
        """extract key topics from the conversation (can be enhanced later)"""
        # for now, just extract based on keywords
        topics = []
        
        # combine all messages
        full_text = " ".join([m.content.lower() for m in messages])
        
        # look for goals
        if "goal" in full_text or "want to" in full_text or "plan to" in full_text:
            topics.append("goals_discussed")
        
        # look for emotions
        emotion_words = ["happy", "sad", "excited", "worried", "anxious", "stressed"]
        for emotion in emotion_words:
            if emotion in full_text:
                topics.append(f"mood:{emotion}")
                break
        
        # look for specific topics (can be expanded)
        topic_keywords = {
            "work": ["work", "job", "career", "office"],
            "health": ["health", "exercise", "sleep", "tired"],
            "learning": ["learn", "study", "practice", "improve"],
            "social": ["friend", "family", "relationship", "people"]
        }
        
        for topic, keywords in topic_keywords.items():
            if any(keyword in full_text for keyword in keywords):
                topics.append(f"topic:{topic}")
        
        return topics
    
    async def summarize_conversation_chunk(
        self, 
        user_uuid: str, 
        platform: str, 
        messages: List[ConversationHistory]
    ) -> Optional[str]:
        """summarize a chunk of conversation"""
        if not messages:
            return None
        
        try:
            # create the summary prompt
            prompt = await self.create_summary_prompt(messages)
            
            # generate summary using cheaper model
            summary = await self.summarizer.generate_response(
                conversation_history=[{"role": "user", "content": prompt}],
                system_prompt="You are a helpful assistant that creates concise, informative summaries."
            )
            
            # extract key topics
            key_topics = await self.extract_key_topics(summary, messages)
            
            # save to database
            with get_db() as db:
                summary_record = ConversationSummary(
                    user_uuid=user_uuid,
                    platform=platform,
                    first_message_id=messages[0].id,
                    last_message_id=messages[-1].id,
                    message_count=len(messages),
                    summary=summary,
                    key_topics=key_topics,
                    model_used=self.summarizer.model
                )
                db.add(summary_record)
                db.commit()
                
                logger.info(f"Created summary for user {user_uuid}: {len(messages)} messages summarized")
            
            return summary
            
        except Exception as e:
            logger.error(f"Error creating summary: {e}")
            return None
    
    async def process_user_summaries(self, user_uuid: str, platform: str) -> int:
        """process all pending summaries for a user, returns number of summaries created"""
        summaries_created = 0
        
        while await self.should_summarize(user_uuid, platform):
            messages, first_id, last_id = await self.get_unsummarized_messages(user_uuid, platform)
            
            # take only up to max_messages_per_summary
            chunk = messages[:self.max_messages_per_summary]
            
            summary = await self.summarize_conversation_chunk(user_uuid, platform, chunk)
            if summary:
                summaries_created += 1
            else:
                break  # stop if summarization fails
        
        return summaries_created
    
    async def get_recent_summaries(
        self, 
        user_uuid: str, 
        platform: str, 
        limit: int = 3
    ) -> List[ConversationSummary]:
        """get the most recent summaries for context"""
        with get_db() as db:
            summaries = db.query(ConversationSummary).filter(
                ConversationSummary.user_uuid == user_uuid,
                ConversationSummary.platform == platform
            ).order_by(ConversationSummary.created_at.desc()).limit(limit).all()
            
            # return in chronological order
            return list(reversed(summaries))
    
    async def get_context_for_conversation(
        self, 
        user_uuid: str, 
        platform: str,
        include_summaries: int = 2
    ) -> str:
        """get a context string including summaries for the AI"""
        summaries = await self.get_recent_summaries(user_uuid, platform, include_summaries)
        
        if not summaries:
            return ""
        
        context_parts = ["Previous conversation context:"]
        
        for i, summary in enumerate(summaries):
            timeframe = f"From {summary.created_at.strftime('%b %d')}"
            context_parts.append(f"\n{timeframe}: {summary.summary}")
            
            if summary.key_topics:
                topics_str = ", ".join(summary.key_topics)
                context_parts.append(f"Key topics: {topics_str}")
        
        return "\n".join(context_parts)