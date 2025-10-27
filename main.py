import discord

from bot_core import bot, client, Discord_bot_test_guild_ID, Dkey


@bot.event
async def on_ready():
    await client.login()
    print(f"✅ {bot.user} is online and synced with Clash of Clans API")
    try:
        test_guild = discord.Object(id=Discord_bot_test_guild_ID)
        bot.tree.copy_global_to(guild=test_guild)
        synced = await bot.tree.sync(guild=test_guild)
        print(f"🔗 Synced {len(synced)} slash commands to guild {Discord_bot_test_guild_ID}")
    except Exception as exc:
        print(f"Sync error: {exc}")


if __name__ == "__main__":
    import Discord_Commands  # registers slash commands

    bot.run(Dkey)
