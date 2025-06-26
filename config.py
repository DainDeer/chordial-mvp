import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # discord
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    DISCORD_TARGET_USER_ID = int(os.getenv("DISCORD_TARGET_USER_ID", "267136853159706638"))
    
    # openai
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "chatgpt-4o-latest")
    COMPRESSOR_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    
    # database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///chordial.db")
    
    # scheduler
    DM_INTERVAL_MINUTES = int(os.getenv("DM_INTERVAL_MINUTES", "60"))
    DELAY_AFTER_IGNORED_HOURS = int(os.getenv("DELAY_AFTER_IGNORED_HOURS", "24"))
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "21"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "8"))

    # compressor
    MIN_LENGTH_TO_COMPRESS = int(os.getenv("MIN_LENGTH_TO_COMPRESS", "100"))

    # features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
    ENABLE_WEB = os.getenv("ENABLE_WEB", "false").lower() == "true"