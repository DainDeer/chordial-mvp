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
    
    # database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///chordial.db")
    
    # scheduler
    DM_INTERVAL_MINUTES = int(os.getenv("DM_INTERVAL_MINUTES", "60"))
    
    # features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
    ENABLE_WEB = os.getenv("ENABLE_WEB", "false").lower() == "true"