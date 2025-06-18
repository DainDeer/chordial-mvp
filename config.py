import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Discord
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    DISCORD_TARGET_USER_ID = int(os.getenv("DISCORD_TARGET_USER_ID", "267136853159706638"))
    
    # OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "chatgpt-4o-latest")
    
    # Scheduler
    DM_INTERVAL_MINUTES = int(os.getenv("DM_INTERVAL_MINUTES", "5"))
    
    # Features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
    ENABLE_WEB = os.getenv("ENABLE_WEB", "false").lower() == "true"