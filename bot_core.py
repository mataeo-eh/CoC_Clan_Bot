from __future__ import annotations

import discord
from discord.ext import commands

import os
import re
from dotenv import load_dotenv

load_dotenv()

ADDITIONAL_TEST_GUILD_IDS = (1372617867224416356,)


def _load_test_guild_ids() -> list[int]:
    """Load configured test guild IDs and include committed additional test guilds."""
    raw_guild_ids = (
        os.getenv("DISCORD_BOT_TEST_GUILD_IDS")
        or os.getenv("DISCORD_BOT_TEST_GUILD_ID")
        or os.getenv("Discord_bot_test_guild_ID")
    )
    if raw_guild_ids is None:
        raise ValueError("DISCORD_BOT_TEST_GUILD_ID environment variable not set")

    guild_ids: list[int] = []
    for raw_value in re.split(r"[\s,;]+", raw_guild_ids.strip()):
        if not raw_value:
            continue
        guild_id = int(raw_value)
        if guild_id not in guild_ids:
            guild_ids.append(guild_id)

    for guild_id in ADDITIONAL_TEST_GUILD_IDS:
        if guild_id not in guild_ids:
            guild_ids.append(guild_id)

    return guild_ids


try:
    COC_API_key = os.getenv("COC_API_KEY")
    if COC_API_key is None:
        raise ValueError("COC_API_KEY environment variable not set")
    Discord_Bot_API_Key = os.getenv("DISCORD_BOT_API_KEY")
    if Discord_Bot_API_Key is None:
        raise ValueError("DISCORD_BOT_API_KEY environment variable not set")
    Discord_bot_test_guild_IDs = _load_test_guild_ids()
    Discord_bot_test_guild_ID = Discord_bot_test_guild_IDs[0]
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
    "Discord_bot_test_guild_IDs",
    "Dkey",
]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=None, intents=intents, help_command=None)
client = CoCAPI(COC_API_key)
Dkey = Discord_Bot_API_Key
