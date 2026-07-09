import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # discord
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # ai provider selection: "anthropic" (default) or "openai"
    AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()

    # anthropic
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # chat model: the persona-facing model the user talks to
    CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-sonnet-5")
    # utility model: cheap model for background jobs (summaries, classification, etc)
    UTILITY_MODEL = os.getenv("UTILITY_MODEL", "claude-haiku-4-5")
    # effort for chat turns: low | medium | high (anthropic only; maps to output_config.effort)
    CHAT_EFFORT = os.getenv("CHAT_EFFORT", "low")
    # max output tokens for a chat/scheduled turn (leaves headroom for adaptive thinking)
    CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "2048"))

    # openai (kept as an alternate provider)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
    COMPRESSOR_MODEL = os.getenv("COMPRESSOR_MODEL", "gpt-4o-mini")

    # notion (the dainframe workspace)
    # an internal integration token: https://www.notion.so/my-integrations
    # when unset, notion tools are simply not registered (chordial still runs).
    NOTION_API_KEY = os.getenv("NOTION_API_KEY")
    # rest api version pin. 2022-06-28 is stable and works with database ids.
    NOTION_API_VERSION = os.getenv("NOTION_API_VERSION", "2022-06-28")
    # database ids default to the dainframe's tasks/projects/cycles; override
    # via env if the integration points at a different workspace.
    NOTION_TASKS_DB_ID = os.getenv("NOTION_TASKS_DB_ID", "9d5b5399-f284-481b-8d2a-e4797c6db18a")
    NOTION_PROJECTS_DB_ID = os.getenv("NOTION_PROJECTS_DB_ID", "0af777e5-3988-4a65-b9a0-1672524d9952")
    NOTION_CYCLES_DB_ID = os.getenv("NOTION_CYCLES_DB_ID", "c21c7869-4672-4bf1-8cd1-d5af73282572")
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
        return cls.notion_enabled() and cls.AGENDA_ENABLED

    # completion reconciler: after the companion replies, a cheap utility-model
    # pass that marks tasks done which the user mentioned finishing in passing.
    # only effective when the agenda is available (it needs the open-task list).
    RECONCILER_ENABLED = os.getenv("RECONCILER_ENABLED", "true").lower() == "true"

    # telegram (second platform). bot token from @BotFather; username (no '@')
    # is used for link-code deep links (https://t.me/<username>?start=<code>).
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
    ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
    # how long a platform link code stays redeemable
    LINK_CODE_TTL_MINUTES = int(os.getenv("LINK_CODE_TTL_MINUTES", "15"))

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
    DELAY_AFTER_IGNORED_HOURS = int(os.getenv("DELAY_AFTER_IGNORED_HOURS", "24"))
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "21"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "8"))

    # compressor (legacy per-message compression; off by default in favor of
    # full-history context, which is both simpler and cache-friendly)
    ENABLE_COMPRESSION = os.getenv("ENABLE_COMPRESSION", "false").lower() == "true"
    MIN_LENGTH_TO_COMPRESS = int(os.getenv("MIN_LENGTH_TO_COMPRESS", "100"))

    # features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
