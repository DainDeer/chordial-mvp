import asyncio
import logging
from config import Config
from src.services.chat_service import ChatService
from src.core.conversation_manager import ConversationManager

# setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    """main entry point for chordial"""
    logger.info("starting chordial...")
    
    # initialize core services
    conversation_manager = ConversationManager()
    
    # initialize ai provider
    ai_provider = None
    if Config.OPENAI_API_KEY:
        from src.ai.openai_provider import OpenAIProvider
        ai_provider = OpenAIProvider()
        if await ai_provider.is_available():
            logger.info("openai provider initialized successfully")
        else:
            logger.warning("openai provider configured but not available")
    
    # create chat service
    chat_service = ChatService(
        ai_provider=ai_provider,
        conversation_manager=conversation_manager
    )
    
    # initialize interfaces
    interfaces = []
    
    if Config.ENABLE_DISCORD:
        from src.interfaces.discord.bot import DiscordInterface
        discord_interface = DiscordInterface(chat_service)
        interfaces.append(discord_interface)
        logger.info("discord interface enabled")
    
    # start all interfaces
    tasks = []
    for interface in interfaces:
        task = asyncio.create_task(interface.start())
        tasks.append(task)
    
    try:
        # keep running until interrupted
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("shutting down chordial...")
        # stop all interfaces
        for interface in interfaces:
            await interface.stop()

if __name__ == "__main__":
    asyncio.run(main())