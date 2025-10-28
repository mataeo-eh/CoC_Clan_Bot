from __future__ import annotations

import discord
from discord.ext import commands

from ENV.Keys import (
    COC_API_key,
    Discord_Bot_API_Key,
    Discord_bot_test_guild_ID,
)
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
