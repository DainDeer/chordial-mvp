import discord
from discord.ext import commands, tasks
from datetime import datetime
import logging

from ..base import BaseInterface
from config import Config

logger = logging.getLogger(__name__)

class DiscordInterface(BaseInterface):
    """Discord bot implementation"""
    
    def __init__(self, chat_service):
        super().__init__(chat_service)
        
        # Setup intents
        intents = discord.Intents.default()
        intents.messages = True
        intents.guilds = True
        intents.message_content = True  # Needed for reading message content
        
        # Create bot instance
        self.bot = commands.Bot(command_prefix="!", intents=intents)
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
    
    async def send_message(self, user_id: str, content: str, **kwargs) -> bool:
        """Send a message to a Discord user"""
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                await user.send(content)
                logger.info(f"Sent DM to user {user.name}")
                return True
        except discord.Forbidden:
            logger.error(f"Could not send DM to user {user_id}. They might have DMs disabled.")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
        return False
    
    async def handle_incoming_message(self, message: discord.Message):
        """Handle incoming Discord messages"""
        # Convert Discord message to a format the chat service understands
        from src.adapters.message_adapter import UnifiedMessage
        
        unified_msg = UnifiedMessage(
            content=message.content,
            user_id=str(message.author.id),
            platform="discord",
            platform_message_id=str(message.id),
            metadata={
                "username": message.author.name,
                "timestamp": message.created_at
            }
        )
        
        # Process through chat service
        response = await self.chat_service.process_message(unified_msg)
        
        # Send response back
        if response:
            await message.channel.send(response)