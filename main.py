import discord
from discord.ext import tasks, commands
import os
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Define the bot's intents
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True

# Create a bot instance
bot = commands.Bot(command_prefix="!", intents=intents)

# the user id of the person you want to dm
# to get your user id, right-click your name in discord and select "copy user id".
# you might need to enable developer mode in your discord settings under advanced.
TARGET_USER_ID = 267136853159706638 # TODO: dynamically ask in the server for the user id

@bot.event
async def on_ready():
    """
    this function is called when the bot is ready and connected to discord.
    """
    print(f'{bot.user} has connected to discord!')
    # start the scheduled message task
    send_scheduled_dm.start()

@tasks.loop(minutes=5) # you can change the interval to seconds, minutes, etc.
async def send_scheduled_dm():
    """
    this is the task that sends a dm on a schedule.
    """
    await bot.wait_until_ready()
    
    # fetch the user object using their id
    user = await bot.fetch_user(TARGET_USER_ID)
    
    if user:
        try:
            # here you'll generate your message with an ai model later
            message_to_send = f"hello! this is your scheduled message from chordial. the current time is :{datetime.now()}✨"
            await user.send(message_to_send)
            print(f"sent scheduled dm to user {user.name}")
        except discord.Forbidden:
            print(f"could not send dm to {user.name}. they might have dms disabled.")

# run the bot
bot.run(TOKEN)