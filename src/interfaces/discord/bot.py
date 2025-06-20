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
        
        # Store the scheduled task reference
        self.scheduled_dm_task = None
    
    def _setup_events(self):
        """Setup Discord event handlers"""
        
        @self.bot.event
        async def on_ready():
            logger.info(f'{self.bot.user} has connected to Discord!')
            # Start scheduled messages
            self.scheduled_dm_task = self.send_scheduled_dm.start()
        
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
    
    @tasks.loop(minutes=Config.DM_INTERVAL_MINUTES)
    async def send_scheduled_dm(self):
        """Send scheduled DMs to opted-in users"""
        await self.bot.wait_until_ready()
        
        # Get all users who should receive scheduled messages
        # For now, we'll use a simple list approach
        # Later this will query the database for users with scheduled messages enabled
        scheduled_users = await self._get_scheduled_message_users()
        
        for user_id in scheduled_users:
            try:
                # Generate message through chat service
                message = await self.chat_service.generate_scheduled_message(user_id, "discord")
                
                if message:
                    await self.send_message(user_id, message)
                    logger.info(f"Sent scheduled message to user {user_id}")
            except Exception as e:
                logger.error(f"Error sending scheduled message to {user_id}: {e}")
    
    async def _get_scheduled_message_users(self) -> list[str]:
        """Get list of users who should receive scheduled messages"""
        # Check if we have user manager available through chat service
        if hasattr(self.chat_service, 'user_manager'):
            return await self.chat_service.user_manager.get_users_with_scheduled_messages("discord")
        
        # Fallback to config for MVP
        if Config.DISCORD_TARGET_USER_ID:
            return [str(Config.DISCORD_TARGET_USER_ID)]
        
        return []