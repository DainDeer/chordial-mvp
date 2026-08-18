import asyncio
import logging
from config import Config
from src.services.chat_service import ChatService
from dainframe.loop.agent_loop import AgentLoop
from src.services.pulse_wiring import build_pulse
from src.services.usage_recorder import UsageRecorder
from src.services.message_router import MessageRouter
from src.services.tools import build_default_registry
from src.managers.user_manager import UserManager
from src.database.database import init_db


def _build_interfaces(chat_service, link_service, user_manager):
    """construct every enabled platform interface. add a branch here per platform;
    nothing else in main() needs to change - the router and scheduler discover
    platforms from whatever this returns.

    telegram in v3 is MULTI-BOT: one interface per enabled helper that has a
    token (Config.telegram_helper_tokens()), all sharing one UpdateDeduper (N
    bots in a group each receive every human message; the deduper keeps the
    first) and one handle->helper map (for resolving @mentions). a single-bot
    deployment - only the chair's TELEGRAM_TOKEN set - yields exactly one
    telegram interface, i.e. v2 behavior.

    the real @username (mention parsing, deep links) is config
    (TELEGRAM_USERNAME_<HELPER>), never the persona card's `telegram_handle`
    placeholder - BotFather names are globally unique, so the card's guess
    ('tempo_bot') is rarely the name you actually got to register."""
    interfaces = []
    if Config.ENABLE_DISCORD:
        from src.providers.platforms.discord_bot import DiscordInterface

        interfaces.append(DiscordInterface(chat_service))
        logger.info("discord interface enabled")
    if Config.ENABLE_TELEGRAM:
        from src.providers.platforms.telegram_bot import (
            TelegramInterface,
            UpdateDeduper,
        )

        tokens = Config.telegram_helper_tokens()
        if not tokens:
            raise RuntimeError(
                "ENABLE_TELEGRAM is true but no helper has a telegram token "
                "(set TELEGRAM_TOKEN for the chair and/or TELEGRAM_TOKEN_<HELPER>)"
            )
        missing_username = [h for h in tokens if not Config.telegram_username_for(h)]
        if missing_username:
            raise RuntimeError(
                "ENABLE_TELEGRAM is true but these helpers have a token and no "
                f"configured @username: {', '.join(missing_username)} (set "
                "TELEGRAM_BOT_USERNAME for the chair and/or "
                "TELEGRAM_USERNAME_<HELPER> for the rest - this must be the "
                "REAL BotFather username, not the persona card's placeholder)"
            )

        usernames = Config.telegram_helper_usernames()
        handle_to_helper = {
            username.lower(): helper_id for helper_id, username in usernames.items()
        }
        deduper = UpdateDeduper()
        for helper_id, token in tokens.items():
            interfaces.append(
                TelegramInterface(
                    helper_id=helper_id,
                    token=token,
                    telegram_handle=usernames[helper_id],
                    chat_service=chat_service,
                    link_service=link_service,
                    user_manager=user_manager,
                    deduper=deduper,
                    group_chat_id=Config.TELEGRAM_GROUP_CHAT_ID,
                    handle_to_helper=handle_to_helper,
                )
            )
        logger.info("telegram interfaces enabled for helpers: %s", ", ".join(tokens))
    return interfaces


def _build_resolver(provider_name: str):
    """the §4.8 routing seam: a director hinting a model on a ScriptLine
    resolves through this table. chordial's builders carry its config - api
    keys, and thinking-by-model (anthropic utility-tier models reject
    adaptive thinking) - and every resolved instance shares ONE concurrency
    ceiling, so hinting a new model never mints a new 'global' semaphore.
    the chat provider itself is the table's default route (resolved once at
    startup), so hint-less runs use the exact same instance."""
    from dainframe.providers import ProviderTable
    from dainframe.providers.limits import ConcurrencyLimiter

    def build_anthropic(model, limiter):
        from dainframe.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model,
            api_key=Config.ANTHROPIC_API_KEY,
            thinking=model != Config.ANTHROPIC_UTILITY_MODEL,
            limiter=limiter,
        )

    def build_openai(model, limiter):
        from dainframe.providers.openai import OpenAIProvider

        return OpenAIProvider(
            model=model, api_key=Config.OPENAI_API_KEY, limiter=limiter
        )

    return ProviderTable(
        default_provider=provider_name,
        default_models={
            "anthropic": Config.CHAT_MODEL,
            "openai": Config.OPENAI_MODEL,
        },
        limiter=ConcurrencyLimiter(Config.MAX_CONCURRENT_AI_CALLS),
        builders={"anthropic": build_anthropic, "openai": build_openai},
    )


def _build_provider(provider_name: str, model: str = None, thinking: bool = True):
    """construct the configured ai provider, or None if misconfigured. pass
    `model` to override the default (e.g. the cheaper utility model), and
    `thinking=False` for models that don't support adaptive thinking (haiku).

    dainframe providers take all config via constructor - model, key, and the
    concurrency ceiling. each provider gets its own private limiter here,
    byte-for-byte the pre-extraction behavior (one semaphore per provider
    instance); a shared ceiling across providers is one injected limiter away
    when it's ever wanted."""
    from dainframe.providers.limits import ConcurrencyLimiter

    limiter = ConcurrencyLimiter(Config.MAX_CONCURRENT_AI_CALLS)
    if provider_name == "anthropic":
        from dainframe.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model or Config.CHAT_MODEL,
            api_key=Config.ANTHROPIC_API_KEY,
            thinking=thinking,
            limiter=limiter,
        )
    if provider_name == "openai":
        from dainframe.providers.openai import OpenAIProvider

        return OpenAIProvider(
            model=model or Config.OPENAI_MODEL,
            api_key=Config.OPENAI_API_KEY,
            limiter=limiter,
        )
    logger.error(
        f"unknown AI_PROVIDER '{provider_name}' (expected 'anthropic' or 'openai')"
    )
    return None


# setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def _run_services(interfaces, pulse, router, tether=None, scorer=None):
    """Supervise long-running services as one failure domain.

    A platform task returning normally is still unexpected while the process
    is running. Stop and cancel its siblings so a dead interface can never be
    hidden by the pulse's infinite loop.
    """
    named_tasks = {
        asyncio.create_task(
            interface.start()
        ): f"{getattr(interface, 'platform', type(interface).__name__)} interface"
        for interface in interfaces
    }
    if pulse is not None:
        named_tasks[asyncio.create_task(pulse.run())] = "pulse"
        logger.info("the pulse started")
    if tether is not None:
        named_tasks[asyncio.create_task(tether.run())] = "rewind tether"
    if scorer is not None:
        named_tasks[asyncio.create_task(scorer.run())] = "cycle scorer"

    try:
        done, _ = await asyncio.wait(named_tasks, return_when=asyncio.FIRST_COMPLETED)
        task = next(iter(done))
        service_name = named_tasks[task]
        exception = task.exception()
        if exception is not None:
            raise RuntimeError(f"{service_name} stopped unexpectedly") from exception
        raise RuntimeError(f"{service_name} stopped unexpectedly")
    finally:
        if pulse is not None:
            pulse.stop()
        for interface in interfaces:
            try:
                await interface.stop()
            except Exception:
                logger.exception(
                    "failed to stop %s interface",
                    getattr(interface, "platform", type(interface).__name__),
                )
        for task in named_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*named_tasks, return_exceptions=True)


async def _close_provider(provider):
    """Close an SDK's async HTTP client when it exposes a close hook."""
    if provider is None:
        return
    close = getattr(getattr(provider, "client", None), "close", None)
    if close is not None:
        result = close()
        if asyncio.iscoroutine(result):
            await result


async def main():
    """main entry point for chordial"""
    logger.info("starting chordial...")

    # initialize database
    init_db()

    # initialize core services
    user_manager = UserManager()

    # initialize ai provider + agent loop. the chat provider is the resolver
    # table's default route, so hint-less runs and hinted runs share the same
    # instances and the same concurrency ceiling.
    provider_name = Config.AI_PROVIDER
    resolver = None
    provider = None
    if provider_name in ("anthropic", "openai"):
        from dainframe.core import ExecutionHints

        resolver = _build_resolver(provider_name)
        provider = resolver.resolve(ExecutionHints(), agent="startup").provider
    else:
        logger.error(
            f"unknown AI_PROVIDER '{provider_name}' (expected 'anthropic' or 'openai')"
        )
    utility_provider = None
    registry = build_default_registry()
    agent_service = None
    if provider is not None:
        if await provider.is_available():
            logger.info(
                f"{provider_name} provider initialized (model={provider.model})"
            )
            agent_service = AgentLoop(
                provider=provider,
                registry=registry,
                provider_name=provider_name,
                usage_sink=UsageRecorder(),
                max_iterations=Config.MAX_TOOL_ITERATIONS,
                resolver=resolver,
            )
        else:
            logger.warning(f"{provider_name} provider configured but not available")

    # proactive workspace awareness: a live agenda digest the chat path injects
    # as ambient context. queries postgres directly - nothing cached, nothing
    # to keep fresh.
    agenda_service = None
    if Config.agenda_enabled():
        from src.services.workspace.agenda import WorkspaceAgenda

        agenda_service = WorkspaceAgenda()
        logger.info("workspace agenda enabled (live queries)")

    # the outbound router is constructed early (before the orchestrator, which
    # borrows router.deliver for out-of-band sends like the platform-switch
    # notice); interfaces register onto it further down, and deliver resolves
    # them at call time, so the ordering is safe.
    router = MessageRouter(user_manager)

    # assemble the cast and the engine. the dainframe engine decides who talks
    # and records what happened (through chordial's director/context/hooks -
    # see src/services/orchestration.py); each agent owns how it thinks.
    # today's cast: the companion helpers (chat personas, tool loop on the
    # persona model) and the curator (silent memory hygiene, utility model).
    orchestrator = None
    curator_agent = None
    if agent_service is not None:
        from src.agents import HelperAgent, CuratorAgent
        from src.personas import load_personas, validate_enabled
        from src.services.orchestration import build_orchestrator

        # each enabled helper is a HelperAgent driven by its persona card.
        # validate_enabled crashes at startup on an unknown id or a missing
        # chair (same fail-loudly style as the telegram config checks) - a
        # mistyped helper or an unroutable deployment should be loud, never a
        # silently-absent persona.
        validate_enabled(Config.ENABLED_HELPERS)
        cards = load_personas()
        agents = {}
        for helper_id in Config.ENABLED_HELPERS:
            agents[helper_id] = HelperAgent(cards[helper_id], agent_service, registry)

        # one utility provider (haiku; thinking=False - it doesn't support
        # adaptive thinking) shared by the background utility jobs below
        utility_provider = _build_provider(
            provider_name,
            model=Config.utility_model_for(provider_name),
            thinking=False,
        )

        if utility_provider is not None:
            from src.services.memory_curator import MemoryCuratorService

            curator_agent = CuratorAgent(
                MemoryCuratorService(
                    provider=utility_provider,
                    provider_name=provider_name,
                    usage_recorder=UsageRecorder(),
                )
            )
            agents["curator"] = curator_agent
            logger.info(f"memory curator initialized (model={utility_provider.model})")

        # completion reconciler: marks tasks done that the user mentioned
        # finishing in passing. needs the agenda (for the open-task list) and
        # the utility model.
        reconciler = None
        if (
            agenda_service is not None
            and utility_provider is not None
            and Config.RECONCILER_ENABLED
        ):
            from src.services.completion_reconciler import CompletionReconcilerService

            reconciler = CompletionReconcilerService(
                provider=utility_provider,
                provider_name=provider_name,
                agenda_service=agenda_service,
                tool_registry=registry,
                usage_recorder=UsageRecorder(),
            )
            logger.info("completion reconciler initialized")

        # the speaking decider (phase 3b): the routing spine's gray-zone
        # call - a shared-room message with no mention and several plausible
        # lanes gets ONE tiny utility-model pick. no utility model, no
        # decider: the director's rules alone route everything to the chair.
        decider = None
        if utility_provider is not None:
            from src.services.speaking_policy import SpeakingDecider

            decider = SpeakingDecider(
                provider=utility_provider,
                provider_name=provider_name,
                usage_recorder=UsageRecorder(),
            )
            logger.info("speaking decider initialized (model=%s)",
                        utility_provider.model)

        from src.managers.helper_state_manager import HelperStateManager

        orchestrator = build_orchestrator(
            agents=agents,
            user_manager=user_manager,
            agenda_service=agenda_service,
            reconciler=reconciler,
            # speaker-aware delivery: the engine makes a specific helper speak
            # (a group line via that helper's bot, or the switch notice as
            # the chair). the router resolves (platform, speaker) -> interface.
            router=router,
            helper_state_manager=HelperStateManager(),
            decider=decider,
        )
        logger.info(f"dainframe engine initialized (agents: {', '.join(agents)})")

    # create chat service (falls back to echo if no orchestrator is available)
    chat_service = ChatService(
        orchestrator=orchestrator,
        user_manager=user_manager,
    )


    # the link-code service: chat-first account linking across platforms
    # (minted by the link_platform tool, redeemed by the telegram interface)
    from src.services.platform_link_service import PlatformLinkService

    link_service = PlatformLinkService(user_manager)

    # build interfaces and register them with the outbound router. the router
    # owns platform->interface routing and link-deactivation on hard failures,
    # so the scheduler never has to know which interface backs a platform.
    interfaces = _build_interfaces(chat_service, link_service, user_manager)
    for interface in interfaces:
        router.register(interface)

    # the web focus view + the app-facing /api/v1 surface. the WebService is
    # supervised like any interface but NOT router-registered (it serves
    # pages and the api); the AppInterface is the inverse - router-registered
    # so the orchestrator can deliver to connected app surfaces, but not
    # supervised (it has no loop to run; the web server owns the sockets).
    if Config.ENABLE_WEB:
        from src.providers.platforms.app import AppInterface
        from src.web.server import WebService

        app_interface = AppInterface(chat_service)
        router.register(app_interface)
        interfaces.append(WebService(chat_service=chat_service,
                                     app_interface=app_interface))
        logger.info(
            "web focus view + app api enabled (http://%s:%s)",
            Config.WEB_HOST, Config.WEB_PORT
        )

    # the ambient loop: the dainframe pulse drives scheduled check-ins and
    # curation through the same engine as every chat message. built after
    # the interfaces register, so delivery targeting is restricted to
    # platforms that actually have a live interface. no engine, no pulse.
    # the rewind tether (docs/REWIND_DESIGN.md section 8): a fixed-string
    # ping through the router when an offer sits unresolved and away - no
    # engine, no tokens. built only when a ringable platform is live.
    tether = None
    if any(p in ("telegram", "discord") for p in router.platforms()):
        from src.services.rewind_tether import RewindTether

        tether = RewindTether(router, user_manager)
        logger.info("rewind tether watcher enabled")

    # edwin's cycle scorer (ROOMS_DESIGN.md section 8, phase 6): a slow
    # sweep that files one assessment per ended cycle. the scores are
    # arithmetic, so the watcher runs even without a utility model - the
    # model only upgrades the prose (summary + evidence-checked findings).
    from src.services.cycle_scorer import CycleScorer

    scorer = CycleScorer(
        provider=utility_provider,
        provider_name=provider_name,
        usage_recorder=UsageRecorder(),
    )
    logger.info("cycle scorer enabled (%s prose)",
                "model" if utility_provider is not None else "deterministic")

    pulse = None
    if orchestrator is not None:
        pulse = build_pulse(
            orchestrator=orchestrator,
            user_manager=user_manager,
            curator=curator_agent,
            # 'app' is deliberately excluded from PROACTIVE targeting: the
            # desktop app is often closed, and a failed send there means a
            # missed check-in with no fallback at send time. presence-aware
            # routing (deer bubble vs tether) lands in phase 7; until then
            # ambient outreach sticks to the always-on messaging platforms.
            platforms=[p for p in router.platforms() if p != "app"],
        )

    try:
        await _run_services(interfaces, pulse, router, tether, scorer)
    finally:
        logger.info("shutting down chordial...")
        await _close_provider(utility_provider)
        await _close_provider(provider)


if __name__ == "__main__":
    asyncio.run(main())
