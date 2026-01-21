import discord

from bot_core import bot, client, Discord_bot_test_guild_ID, Dkey
from logger import get_logger, setup_logger

logger = setup_logger()
log = get_logger()


@bot.event
async def on_ready():
    """Handle bot ready event."""
    log.info("on_ready event fired")
    log.debug("Logging into Clash of Clans API")
    await client.login()
    log.debug("Clash of Clans login completed")
    print(f"✅ {bot.user} is online and synced with Clash of Clans API")
    try:
        log.debug("Synchronising commands to guild %s", Discord_bot_test_guild_ID)
        test_guild = discord.Object(id=Discord_bot_test_guild_ID)
        bot.tree.copy_global_to(guild=test_guild)
        synced = await bot.tree.sync(guild=test_guild)
        log.info("Synced %d slash commands to guild %s", len(synced), Discord_bot_test_guild_ID)
        print(f"🔗 Synced {len(synced)} slash commands to guild {Discord_bot_test_guild_ID}")
    except Exception as exc:
        log.exception("Sync error")
        print(f"Sync error: {exc}")

    # Kick off background war alert processing after commands are registered.
    try:
        from Discord_Commands import (
            ensure_report_schedule_loop_running,
            ensure_war_alert_loop_running,
        )

        log.debug("Starting background loops")
        ensure_war_alert_loop_running()
        ensure_report_schedule_loop_running()
    except Exception as exc:
        log.exception("Failed to start background loops")
        print(f"Failed to start background loops: {exc}")


if __name__ == "__main__":
    log.info("Starting bot runtime")
    import Discord_Commands  # registers slash commands
    print("[DEBUG] Attempting to run bot")
    bot.run(Dkey)
    print("[DEBUG] Bot run started")
