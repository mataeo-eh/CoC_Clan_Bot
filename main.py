import numpy as np
from ENV.Keys import COC_API_key, Discord_Bot_API_Key, email, password
import discord
import coc
from discord.ext import commands
import asyncio
from discord import app_commands
from COC_API import *
 

Jesus_Saves_Tag="#2GG82OG2U"
Christ_is_King_Clan_tag="#2JU8CQCPJ"
email = email
pw=password



CKey = COC_API_key
Dkey = Discord_Bot_API_Key


# Discord Bot setup
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Create the COC client
client = CoCAPI(CKey)



@bot.event
async def on_ready():
    # Login to Clash of Clans API
    await client.login() # pyright: ignore[reportCallIssue]
    print(f"✅ {bot.user} is online and synced with Clash of Clans API")

    try:
        synced = await bot.tree.sync()  # register slash commands globally
        print(f"🔗 Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.tree.command(name="set_clan", description="Set a default clan for this server")
@app_commands.describe(clan_name="Name of the clan", tag="Clan tag (e.g. #ABC123)")
async def set_clan(interaction: discord.Interaction, clan_name: str, tag: str):
    member_roles = [role.name for role in interaction.user.roles] # pyright: ignore[reportAttributeAccessIssue]
    
    # Only allow admins or specific roles to configure
    if "Admin" not in member_roles:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    client.set_server_clan(interaction.guild.id, clan_name, tag) # pyright: ignore[reportOptionalMemberAccess]
    await interaction.response.send_message(f"✅ Set {clan_name} to {tag} for this server.")












bot.run(Dkey)