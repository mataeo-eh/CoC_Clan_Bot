from __future__ import annotations

import discord
from discord.ext import commands

import os
from dotenv import load_dotenv

load_dotenv()
try:
    COC_API_key = os.getenv("COC_API_KEY")
    if COC_API_key is None:
        raise ValueError("COC_API_KEY environment variable not set")
    Discord_Bot_API_Key = os.getenv("DISCORD_BOT_API_KEY")
    if Discord_Bot_API_Key is None:
        raise ValueError("DISCORD_BOT_API_KEY environment variable not set")
    Discord_bot_test_guild_ID = os.getenv("DISCORD_BOT_TEST_GUILD_ID")
    if Discord_bot_test_guild_ID is None:
        raise ValueError("DISCORD_BOT_TEST_GUILD_ID environment variable not set")
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
