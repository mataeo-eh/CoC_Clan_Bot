from __future__ import annotations

import discord
from discord import app_commands
from bot_core import bot, client

@bot.tree.command(name="set_clan", description="Set a default clan for this server")
@app_commands.describe(clan_name="Name of the clan", tag="Clan tag (e.g. #ABC123)")
async def set_clan(interaction: discord.Interaction, clan_name: str, tag: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used inside a Discord server.",
            ephemeral=True
        )
        return

    member = interaction.user
    # Only allow members with the Administrator permission to configure
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need the Administrator permission to configure this command.",
            ephemeral=True
        )
        return
    client.set_server_clan(interaction.guild.id, clan_name, tag)
    await interaction.response.send_message(f"✅ Set {clan_name} to {tag} for this server.")
