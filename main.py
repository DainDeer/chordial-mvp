import asyncio
import logging
from config import Config
from src.services.chat_service import ChatService
from src.services.agent_service import AgentService
from src.services.scheduler_service import SchedulerService
from src.services.usage_recorder import UsageRecorder
from src.services.message_router import MessageRouter
from src.services.tools import build_default_registry
from src.managers.user_manager import UserManager
from src.database.database import init_db


def _build_interfaces(chat_service):
    """construct every enabled platform interface. add a branch here per platform;
    nothing else in main() needs to change - the router and scheduler discover
    platforms from whatever this returns."""
    interfaces = []
    if Config.ENABLE_DISCORD:
        from src.providers.platforms.discord_bot import DiscordInterface
        interfaces.append(DiscordInterface(chat_service))
        logger.info("discord interface enabled")
    return interfaces


def _build_provider(provider_name: str, model: str = None, thinking: bool = True):
    """construct the configured ai provider, or None if misconfigured. pass
    `model` to override the default (e.g. the cheaper utility model), and
    `thinking=False` for models that don't support adaptive thinking (haiku)."""
    if provider_name == "anthropic":
        from src.providers.ai.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, thinking=thinking)
    if provider_name == "openai":
        from src.providers.ai.openai_provider import OpenAIProvider
        return OpenAIProvider(model=model) if model else OpenAIProvider()
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

    # proactive notion awareness: a cached agenda snapshot the scheduler keeps
    # fresh in the background and the chat path injects as ambient context. only
    # wired up when notion is configured and the feature flag is on.
    agenda_service = None
    if Config.agenda_enabled():
        from src.services.notion.snapshot_service import AgendaSnapshotService
        agenda_service = AgendaSnapshotService()
        logger.info("agenda snapshot service enabled")

    # assemble the cast and the orchestrator. the orchestrator decides who
    # talks and records what happened; each agent owns how it thinks. today's
    # cast: the companion (chordial's chat persona, tool loop on the persona
    # model) and the curator (silent memory hygiene on the utility model).
    orchestrator = None
    if agent_service is not None:
        from src.agents import CompanionAgent, CuratorAgent
        from src.services.orchestrator import Orchestrator

        agents = {"chordial": CompanionAgent(agent_service, registry)}

        # one utility provider (haiku; thinking=False - it doesn't support
        # adaptive thinking) shared by the background utility jobs below
        utility_provider = _build_provider(
            provider_name, model=Config.UTILITY_MODEL, thinking=False,
        )

        if utility_provider is not None:
            from src.services.memory_curator import MemoryCuratorService
            agents["curator"] = CuratorAgent(MemoryCuratorService(
                provider=utility_provider,
                provider_name=provider_name,
                usage_recorder=UsageRecorder(),
            ))
            logger.info(f"memory curator initialized (model={utility_provider.model})")

        # completion reconciler: marks tasks done that the user mentioned
        # finishing in passing. needs the agenda (for the open-task list) and
        # the utility model.
        reconciler = None
        if agenda_service is not None and utility_provider is not None and Config.RECONCILER_ENABLED:
            from src.services.completion_reconciler import CompletionReconcilerService
            reconciler = CompletionReconcilerService(
                provider=utility_provider,
                provider_name=provider_name,
                agenda_service=agenda_service,
                tool_registry=registry,
                usage_recorder=UsageRecorder(),
            )
            logger.info("completion reconciler initialized")

        orchestrator = Orchestrator(
            agents=agents,
            user_manager=user_manager,
            agenda_service=agenda_service,
            tool_registry=registry,
            reconciler=reconciler,
        )
        logger.info(f"orchestrator initialized (agents: {', '.join(agents)})")

    # create chat service (falls back to echo if no orchestrator is available)
    chat_service = ChatService(
        orchestrator=orchestrator,
        user_manager=user_manager,
    )

    # create scheduler service
    scheduler_service = SchedulerService(
        orchestrator=orchestrator,
        user_manager=user_manager,
        agenda_service=agenda_service,
    )

    # build interfaces and register them with the outbound router. the router
    # owns platform->interface routing and link-deactivation on hard failures,
    # so the scheduler never has to know which interface backs a platform.
    interfaces = _build_interfaces(chat_service)
    router = MessageRouter(user_manager)
    for interface in interfaces:
        router.register(interface)

    # start all interfaces
    tasks = [asyncio.create_task(interface.start()) for interface in interfaces]

    # start the scheduler service - it drives its loop off whatever platforms
    # actually have a live interface, and delivers through the router.
    scheduler_task = asyncio.create_task(
        scheduler_service.run_scheduling_loop(
            platforms=router.platforms(),
            message_callback=router.deliver,
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