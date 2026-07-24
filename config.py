import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class Config:
    # discord
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # ai provider selection: "anthropic" (default) or "openai"
    AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()

    # anthropic
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # chat model: the persona-facing model the user talks to. opus 4.6
    # ($5/$25 per MTok vs sonnet-5's $3/$15) - the companion IS the product,
    # so the conversational model gets the capability budget. note
    # claude-opus-4-8 is the same price as 4.6 if we ever want the newer one.
    CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-6")
    # utility models: cheap models for background jobs (summaries,
    # classification, etc). UTILITY_MODEL remains a backwards-compatible
    # override for deployments that predate provider-specific settings.
    _LEGACY_UTILITY_MODEL = os.getenv("UTILITY_MODEL")
    ANTHROPIC_UTILITY_MODEL = os.getenv(
        "ANTHROPIC_UTILITY_MODEL", _LEGACY_UTILITY_MODEL or "claude-haiku-4-5"
    )
    # effort for chat turns: low | medium | high (anthropic only; maps to output_config.effort)
    CHAT_EFFORT = os.getenv("CHAT_EFFORT", "low")
    # max output tokens for a chat/scheduled turn. CRITICAL: with adaptive
    # thinking on, this cap covers thinking + reply TOGETHER - at 2048 an
    # instruction-heavy turn (e.g. an introduction) could burn the whole budget
    # on thinking and emit ZERO reply text (stop_reason=max_tokens, empty
    # response -> the user gets the error fallback). it's a ceiling you only pay
    # for when tokens are actually generated, so keep it generous; a normal
    # short reply still costs a few hundred tokens.
    CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "4096"))

    # openai (kept as an alternate provider)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
    OPENAI_UTILITY_MODEL = os.getenv(
        "OPENAI_UTILITY_MODEL", _LEGACY_UTILITY_MODEL or "gpt-4o-mini"
    )
    # Public legacy attribute retained for callers outside this repository.
    UTILITY_MODEL = _LEGACY_UTILITY_MODEL or ANTHROPIC_UTILITY_MODEL
    COMPRESSOR_MODEL = os.getenv("COMPRESSOR_MODEL", "gpt-4o-mini")

    @classmethod
    def utility_model_for(cls, provider_name: str) -> str:
        """Return a utility model that belongs to the selected provider."""
        if provider_name == "anthropic":
            return cls.ANTHROPIC_UTILITY_MODEL
        if provider_name == "openai":
            return cls.OPENAI_UTILITY_MODEL
        raise ValueError(f"unknown AI provider: {provider_name}")

    # workspace backend: which system of record the task tools + agenda run
    # against. "notion" = the legacy dainframe integration; "native" = the
    # in-db workspace (docs/NATIVE_WORKSPACE_DESIGN.md). a transition flag
    # with a scheduled death (phase D deletes the notion branch), not a
    # permanent abstraction.
    WORKSPACE_BACKEND = os.getenv("WORKSPACE_BACKEND", "notion").lower()
    # cap rows returned by any single workspace list_* call (keeps prompts
    # lean); inherits the notion default so behavior doesn't shift at cutover
    WORKSPACE_MAX_PAGE_SIZE = int(
        os.getenv("WORKSPACE_MAX_PAGE_SIZE", os.getenv("NOTION_MAX_PAGE_SIZE", "25"))
    )

    @classmethod
    def workspace_native(cls) -> bool:
        return cls.WORKSPACE_BACKEND == "native"

    # notion (the dainframe workspace)
    # an internal integration token: https://www.notion.so/my-integrations
    # when unset, notion tools are simply not registered (chordial still runs).
    NOTION_API_KEY = os.getenv("NOTION_API_KEY")
    # rest api version pin. 2022-06-28 is stable and works with database ids.
    NOTION_API_VERSION = os.getenv("NOTION_API_VERSION", "2022-06-28")
    # database ids default to the dainframe's tasks/projects/cycles; override
    # via env if the integration points at a different workspace.
    NOTION_TASKS_DB_ID = os.getenv(
        "NOTION_TASKS_DB_ID", "9d5b5399-f284-481b-8d2a-e4797c6db18a"
    )
    NOTION_PROJECTS_DB_ID = os.getenv(
        "NOTION_PROJECTS_DB_ID", "0af777e5-3988-4a65-b9a0-1672524d9952"
    )
    NOTION_CYCLES_DB_ID = os.getenv(
        "NOTION_CYCLES_DB_ID", "c21c7869-4672-4bf1-8cd1-d5af73282572"
    )
    # cap rows returned by any single list_* call (keeps prompts lean)
    NOTION_MAX_PAGE_SIZE = int(os.getenv("NOTION_MAX_PAGE_SIZE", "25"))

    @classmethod
    def notion_enabled(cls) -> bool:
        return bool(cls.NOTION_API_KEY)

    # proactive notion awareness (see services/notion/snapshot_service.py). the
    # scheduler keeps a cached "agenda snapshot" fresh in the background and the
    # chat path injects it as ambient context. all of this is effective only
    # when notion_enabled() - AGENDA_ENABLED is the extra on/off switch.
    AGENDA_ENABLED = os.getenv("AGENDA_ENABLED", "true").lower() == "true"
    # how long a snapshot is considered fresh before a background refresh
    AGENDA_TTL_MINUTES = int(os.getenv("AGENDA_TTL_MINUTES", "30"))

    @classmethod
    def agenda_enabled(cls) -> bool:
        # the native agenda needs no api key - just the flag; the notion
        # agenda additionally needs the integration configured
        if cls.workspace_native():
            return cls.AGENDA_ENABLED
        return cls.notion_enabled() and cls.AGENDA_ENABLED

    # completion reconciler: after the companion replies, a cheap utility-model
    # pass that marks tasks done which the user mentioned finishing in passing.
    # only effective when the agenda is available (it needs the open-task list).
    RECONCILER_ENABLED = os.getenv("RECONCILER_ENABLED", "true").lower() == "true"

    # telegram (second platform). bot token from @BotFather; username (no '@')
    # is used for link-code deep links (https://t.me/<username>?start=<code>).
    # in v3 each helper runs as its OWN telegram bot: the token for helper X is
    # TELEGRAM_TOKEN_<X> (e.g. TELEGRAM_TOKEN_TEMPO), with the bare TELEGRAM_TOKEN
    # serving as chordial's (back-compat with the single-bot v2 deployment).
    #
    # the username works the same way (TELEGRAM_USERNAME_<X>, chordial falls
    # back to the bare TELEGRAM_BOT_USERNAME) and is DELIBERATELY config, not
    # the persona card's `telegram_handle` field: BotFather usernames must be
    # globally unique across all of telegram, so a card's placeholder
    # ('tempo_bot') is almost never the real, available name you register -
    # you WILL end up with something like 'chordial_mvp_v3_tempo_bot'. every
    # place that needs the real handle (mention parsing, meet-the-guides deep
    # links) reads it from here, never from the card.
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
    ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
    # trust-on-first-message for telegram DMs (the discord contract): an
    # unknown sender gets a user row + the introduction flow instead of the
    # stranger wall. default OFF because telegram bot usernames are publicly
    # discoverable - any stranger who finds the bot would cost api spend.
    # dev instances turn this on to test onboarding without discord; a public
    # deployment should prefer invite codes (MULTI_USER_SPEC phase 2) over
    # leaving this open. DM-only: the group room stays known-users-only.
    TELEGRAM_OPEN_ONBOARDING = (
        os.getenv("TELEGRAM_OPEN_ONBOARDING", "false").lower() == "true"
    )
    # the shared group chat all the helper bots and the user sit in. captured
    # once (env, or a /setup_group command that writes it back) - the delivery
    # target for group-scope proactive/scripted messages. None = no group yet
    # (helpers only reachable via 1:1 dms until it's set).
    TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID")
    # how long a platform link code stays redeemable
    LINK_CODE_TTL_MINUTES = int(os.getenv("LINK_CODE_TTL_MINUTES", "15"))

    @classmethod
    def telegram_token_for(cls, helper_id: str) -> Optional[str]:
        """the bot token a helper polls/sends on. chordial falls back to the
        bare TELEGRAM_TOKEN so a single-bot v2 deployment keeps working
        untouched; every other helper needs its own TELEGRAM_TOKEN_<HELPER>."""
        specific = os.getenv(f"TELEGRAM_TOKEN_{helper_id.upper()}")
        if specific:
            return specific
        if helper_id == "chordial":
            return cls.TELEGRAM_TOKEN
        return None

    @classmethod
    def telegram_helper_tokens(cls) -> "dict[str, str]":
        """{helper_id: token} for every ENABLED helper that has a token set.
        drives how many telegram interfaces main() spins up (one per bot)."""
        out: dict[str, str] = {}
        for helper_id in cls.ENABLED_HELPERS:
            token = cls.telegram_token_for(helper_id)
            if token:
                out[helper_id] = token
        return out

    @classmethod
    def telegram_username_for(cls, helper_id: str) -> Optional[str]:
        """the bot's REAL @username (no '@'), used for mention parsing and
        deep links. chordial falls back to TELEGRAM_BOT_USERNAME (back-compat
        with the single-bot v2 deployment); every other helper needs its own
        TELEGRAM_USERNAME_<HELPER> - there's no safe default, because a
        persona card's `telegram_handle` is just a placeholder, not a
        registered bot name."""
        specific = os.getenv(f"TELEGRAM_USERNAME_{helper_id.upper()}")
        if specific:
            return specific.lstrip("@")
        if helper_id == "chordial":
            return cls.TELEGRAM_BOT_USERNAME
        return None

    @classmethod
    def telegram_helper_usernames(cls) -> "dict[str, str]":
        """{helper_id: username} for every ENABLED helper that has BOTH a
        token and a configured username - main() requires both before an
        interface is built, so this is the authoritative set once startup
        validation has passed."""
        out: dict[str, str] = {}
        for helper_id in cls.telegram_helper_tokens():
            username = cls.telegram_username_for(helper_id)
            if username:
                out[helper_id] = username
        return out

    @classmethod
    def telegram_linking_enabled(cls) -> bool:
        return cls.ENABLE_TELEGRAM and bool(cls.TELEGRAM_BOT_USERNAME)

    # agent loop
    MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "5"))
    # how many recent messages of history to send as context
    MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
    # ceiling on concurrent in-flight ai api calls across all users
    MAX_CONCURRENT_AI_CALLS = int(os.getenv("MAX_CONCURRENT_AI_CALLS", "6"))

    # database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///chordial.db")

    # scheduler
    DM_INTERVAL_MINUTES = int(os.getenv("DM_INTERVAL_MINUTES", "60"))
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "21"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "8"))

    # v3 personas: every card in src/personas/*.yaml is loaded, but only these
    # ids become live agents. one enabled helper = exactly v2 behavior.
    ENABLED_HELPERS = [
        h.strip()
        for h in os.getenv("ENABLED_HELPERS", "chordial").split(",")
        if h.strip()
    ]

    # proactive-outreach gate (see services/proactivity_gate.py): hard caps on
    # unanswered proactive messages plus exponential backoff between them.
    # replaces the old single-step DELAY_AFTER_IGNORED_HOURS rule. checked
    # BEFORE any generation - a denied tick costs zero tokens.
    GATE_PER_HELPER_CAP = int(os.getenv("GATE_PER_HELPER_CAP", "3"))
    GATE_CREW_CAP = int(os.getenv("GATE_CREW_CAP", "4"))
    GATE_BASE_INTERVAL_HOURS = float(os.getenv("GATE_BASE_INTERVAL_HOURS", "3"))

    # compressor (legacy per-message compression; off by default in favor of
    # full-history context, which is both simpler and cache-friendly)
    ENABLE_COMPRESSION = os.getenv("ENABLE_COMPRESSION", "false").lower() == "true"
    MIN_LENGTH_TO_COMPRESS = int(os.getenv("MIN_LENGTH_TO_COMPRESS", "100"))

    # features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
