import asyncio
import discord
from discord.ext import commands, tasks
from datetime import datetime
import logging

from .base import BaseInterface, UndeliverableError
from config import Config
from src.utils.string_utils import chunk_message

logger = logging.getLogger(__name__)

class DiscordInterface(BaseInterface):
    """Discord bot implementation"""

    platform = "discord"

    def __init__(self, chat_service):
        super().__init__(chat_service)
        
        # Setup intents
        intents = discord.Intents.default()
        intents.messages = True
        intents.guilds = True
        intents.message_content = True  # Needed for reading message content
        
        # Create bot instance
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.scheduled_dm_task = None
        self._setup_events()

    
    def _setup_events(self):
        """Setup Discord event handlers"""
        
        @self.bot.event
        async def on_ready():
            logger.info(f'{self.bot.user} has connected to Discord!')
        
        @self.bot.event
        async def on_message(message):
            # Ignore messages from the bot itself
            if message.author == self.bot.user:
                return
            
            # Handle DMs
            if isinstance(message.channel, discord.DMChannel):
                await self.handle_incoming_message(message)
            
            # Process commands
            await self.bot.process_commands(message)
    
    async def start(self):
        """Start the Discord bot"""
        await self.bot.start(Config.DISCORD_TOKEN)
    
    async def stop(self):
        """Stop the Discord bot"""
        if self.scheduled_dm_task:
            self.scheduled_dm_task.cancel()
        await self.bot.close()
    
    async def send_message(self, platform_user_id: str, content: str, **kwargs) -> bool:
        """Send a message to a Discord user, splitting if needed.

        Raises UndeliverableError for permanent failures (unknown user, DMs
        forbidden) so the router can deactivate the link. Transient failures
        return False and leave the link active."""
        try:
            user = await self.bot.fetch_user(int(platform_user_id))
            if not user:
                # fetch_user returns None only for a genuinely unknown id
                raise UndeliverableError(f"discord user {platform_user_id} not found")

            # chunk the message if it's too long
            chunks = chunk_message(content)

            # send each chunk
            for i, chunk in enumerate(chunks):
                await user.send(chunk)
                # small delay between chunks to avoid rate limiting
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.5)

            logger.info(f"Sent DM to user {user.name} ({len(chunks)} chunk{'s' if len(chunks) > 1 else ''})")
            return True

        except discord.NotFound as e:
            # 404 unknown user - the id is dead, this link is undeliverable
            raise UndeliverableError(f"discord user {platform_user_id} not found") from e
        except discord.Forbidden as e:
            # 403 - they've blocked the bot or disabled DMs; won't succeed on retry
            raise UndeliverableError(
                f"discord user {platform_user_id} has DMs disabled/blocked"
            ) from e
        except ValueError as e:
            # non-integer platform_user_id - malformed link, never deliverable
            raise UndeliverableError(f"invalid discord user id '{platform_user_id}'") from e
        except Exception as e:
            # transient (network, rate limit, etc) - leave the link active
            logger.error(f"transient error sending discord message to {platform_user_id}: {e}")
            return False
    
    async def handle_incoming_message(self, message: discord.Message):
        """Handle incoming Discord messages"""
        # Convert Discord message to a format the chat service understands
        from src.models.unified_message import UnifiedMessage
        
        unified_msg = UnifiedMessage(
            content=message.content,
            platform_user_id=str(message.author.id),
            platform="discord",
            platform_message_id=str(message.id),
            metadata={
                "username": message.author.name,
                "timestamp": message.created_at
            }
        )
        
        # Process through chat service
        response = await self.chat_service.process_message(unified_msg)

        # Send response back, chunked - a >2000-char reply on this live path
        # used to hit discord's raw length limit and error out uncaught
        if response:
            chunks = chunk_message(response)
            for i, chunk in enumerate(chunks):
                await message.channel.send(chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.5)