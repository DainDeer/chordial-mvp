import asyncio
import logging
from config import Config
from src.services.chat_service import ChatService
from src.services.agent_service import AgentService
from src.services.scheduler_service import SchedulerService
from src.services.usage_recorder import UsageRecorder
from src.services.tools import build_default_registry
from src.managers.conversation_manager import ConversationManager
from src.managers.user_manager import UserManager
from src.database.database import init_db


def _build_provider(provider_name: str):
    """construct the configured ai provider, or None if misconfigured."""
    if provider_name == "anthropic":
        from src.providers.ai.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if provider_name == "openai":
        from src.providers.ai.openai_provider import OpenAIProvider
        return OpenAIProvider()
    logger.error(f"unknown AI_PROVIDER '{provider_name}' (expected 'anthropic' or 'openai')")
    return None

# setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    """main entry point for chordial"""
    logger.info("starting chordial...")
    
    # initialize database
    init_db()
    
    # initialize core services
    conversation_manager = ConversationManager()
    user_manager = UserManager()

    # initialize ai provider + agent loop
    provider_name = Config.AI_PROVIDER
    provider = _build_provider(provider_name)
    registry = build_default_registry()
    agent_service = None
    if provider is not None:
        if await provider.is_available():
            logger.info(f"{provider_name} provider initialized (model={provider.model})")
            agent_service = AgentService(
                provider=provider,
                registry=registry,
                provider_name=provider_name,
                usage_recorder=UsageRecorder(),
                max_iterations=Config.MAX_TOOL_ITERATIONS,
            )
        else:
            logger.warning(f"{provider_name} provider configured but not available")

    # create chat service (falls back to echo if no agent service is available)
    chat_service = ChatService(
        agent_service=agent_service,
        conversation_manager=conversation_manager,
        user_manager=user_manager,
        tool_registry=registry if agent_service else None,
    )
    
    # create scheduler service
    scheduler_service = SchedulerService(
        chat_service=chat_service,
        user_manager=user_manager
    )
    
    # initialize interfaces
    interfaces = []
    
    if Config.ENABLE_DISCORD:
        from src.providers.platforms.discord_bot import DiscordInterface
        discord_interface = DiscordInterface(chat_service)
        interfaces.append(discord_interface)
        logger.info("discord interface enabled")
    
    # start all interfaces
    tasks = []
    for interface in interfaces:
        task = asyncio.create_task(interface.start())
        tasks.append(task)
    
    # create callback for sending messages through interfaces
    async def send_message_callback(platform: str, platform_user_id: str, message: str):
        """callback for scheduler to send messages through appropriate interface"""
        for interface in interfaces:
            if platform == "discord":
                # check if this interface has send_message method and use it
                if hasattr(interface, 'send_message'):
                    await interface.send_message(platform_user_id, message)
                    break
            # add other platforms here as we build them
    
    # start the scheduler service
    scheduler_task = asyncio.create_task(
        scheduler_service.run_scheduling_loop(
            platforms=['discord'],  # add more platforms as we support them
            message_callback=send_message_callback
        )
    )
    tasks.append(scheduler_task)
    logger.info("scheduler service started")
    
    try:
        # keep running until interrupted
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("shutting down chordial...")
        # stop scheduler
        scheduler_service.stop()
        # stop all interfaces
        for interface in interfaces:
            await interface.stop()

if __name__ == "__main__":
    asyncio.run(main())