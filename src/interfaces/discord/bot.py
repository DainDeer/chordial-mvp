import discord
from discord.ext import commands, tasks
from datetime import datetime
import logging

from ..base import BaseInterface
from config import Config

logger = logging.getLogger(__name__)

class DiscordInterface(BaseInterface):
    """discord bot implementation"""
    
    def __init__(self, chat_service):
        super().__init__(chat_service)
        
        # setup intents
        intents = discord.Intents.default()
        intents.messages = True
        intents.guilds = True
        intents.message_content = True  # needed for reading message content
        
        # create bot instance
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self._setup_events()
        
        # store the scheduled task reference
        self.scheduled_dm_task = None
    
    def _setup_events(self):
        """setup discord event handlers"""
        
        @self.bot.event
        async def on_ready():
            logger.info(f'{self.bot.user} has connected to discord!')
            # start scheduled messages
            self.scheduled_dm_task = self.send_scheduled_dm.start()
        
        @self.bot.event
        async def on_message(message):
            # ignore messages from the bot itself
            if message.author == self.bot.user:
                return
            
            # handle dms
            if isinstance(message.channel, discord.DMChannel):
                await self.handle_incoming_message(message)
            
            # process commands
            await self.bot.process_commands(message)
    
    async def start(self):
        """start the discord bot"""
        await self.bot.start(Config.DISCORD_TOKEN)
    
    async def stop(self):
        """stop the discord bot"""
        if self.scheduled_dm_task:
            self.scheduled_dm_task.cancel()
        await self.bot.close()
    
    async def send_message(self, user_id: str, content: str, **kwargs) -> bool:
        """send a message to a discord user"""
        try:
            user = await self.bot.fetch_user(int(user_id))
            if user:
                await user.send(content)
                logger.info(f"sent dm to user {user.name}")
                return True
        except discord.Forbidden:
            logger.error(f"could not send dm to user {user_id}. they might have dms disabled.")
        except Exception as e:
            logger.error(f"error sending message: {e}")
        return False
    
    async def handle_incoming_message(self, message: discord.Message):
        """handle incoming discord messages"""
        # convert discord message to a format the chat service understands
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
        
        # process through chat service
        response = await self.chat_service.process_message(unified_msg)
        
        # send response back
        if response:
            await message.channel.send(response)
    
    @tasks.loop(minutes=Config.DM_INTERVAL_MINUTES)
    async def send_scheduled_dm(self):
        """send scheduled dms"""
        await self.bot.wait_until_ready()
        
        # for now, using the hardcoded user id
        # later this will come from a database of users who opted in
        user_id = str(Config.DISCORD_TARGET_USER_ID)
        
        # generate message through chat service
        # for now, we'll use a simple message
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # in the future, this will call the ai to generate a contextual message
        message = await self.chat_service.generate_scheduled_message(user_id)
        
        # fallback to simple message if service isn't ready yet
        if not message:
            message = f"hello! this is your scheduled message from chordial. the current time is: {current_time} ✨"
        
        await self.send_message(user_id, message)