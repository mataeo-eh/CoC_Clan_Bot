from __future__ import annotations

import discord
from discord.ext import commands

import os
from dotenv import load_dotenv

load_dotenv()
try:
    COC_API_key = os.getenv("COC_API_KEY")
    Discord_Bot_API_Key = os.getenv("DISCORD_BOT_API_KEY")
    Discord_bot_test_guild_ID = os.getenv("DISCORD_BOT_TEST_GUILD_ID")
except Exception as e:
    print(f"Error loading environment variables: {e}")
    raise


from COC_API import CoCAPI


__all__ = [
    "intents",
    "bot",
    "client",
    "COC_API_key",
    "Discord_Bot_API_Key",
    "Discord_bot_test_guild_ID",
    "Dkey",
]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=None, intents=intents, help_command=None)
client = CoCAPI(COC_API_key)
Dkey = Discord_Bot_API_Key
