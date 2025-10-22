from __future__ import annotations

from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from COC_API import ClanNotConfiguredError, CoCAPI, GuildNotConfiguredError
from coc.miscmodels import Timestamp
from ENV.Clan_Configs import server_config
from ENV.Keys import (
    COC_API_key,
    Discord_Bot_API_Key,
    Discord_bot_test_guild_ID,
    email,
    password,
)


WAR_INFO_LABELS: Dict[str, str] = {
    "home_clan": "home clan",
    "opponent_clan": "opponent clan",
    "clan_tag": "clan tag",
    "war_tag": "war tag",
    "war_state": "war state",
    "war_status": "war status",
    "war_type": "war type",
    "is_cwl": "is cwl",
    "war_size": "war size",
    "attacks_per_member": "attacks per member",
    "total_attacks": "total attacks",
    "battle_modifier": "battle modifier",
    "preparation_start_time": "preparation start time",
    "war_day_start_time": "war day start time",
    "war_ends": "war ends",
    "league_group": "league group",
    "all_members": "all members",
}


def _format_war_info_value(label: str, value) -> str:
    """Convert war info values into readable strings for Discord responses."""
    if value is None:
        return "Not available"

    if label in {"preparation start time", "war day start time", "war ends"}:
        raw_time = None

        if isinstance(value, Timestamp):
            raw_time = value.time
        elif hasattr(value, "time"):
            potential_time = getattr(value, "time")
            if isinstance(potential_time, datetime):
                raw_time = potential_time
        elif isinstance(value, datetime):
            raw_time = value

        if isinstance(raw_time, datetime):
            if raw_time.tzinfo is not None:
                now = datetime.now(raw_time.tzinfo)
            else:
                now = datetime.utcnow()

            if label == "preparation start time":
                target = raw_time + timedelta(hours=24)
            else:
                target = raw_time

            remaining = target - now
            if remaining.total_seconds() <= 0:
                if label == "preparation start time":
                    return "Preparation phase complete"
                if label == "war day start time":
                    return "War has started"
                return "War has ended"

            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            if label == "preparation start time":
                return f"{hours}h {minutes}m {seconds}s left in preparation phase"
            if label == "war day start time":
                return f"{hours}h {minutes}m {seconds}s until war starts"
            return f"{hours}h {minutes}m {seconds}s until war ends"

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "Not available"
        if label in {"war status", "war state", "war type"}:
            return text.replace("_", " ").replace("-", " ").title()
        return text

    if isinstance(value, list):
        if not value:
            return "None"
        preview = []
        for item in value:
            if hasattr(item, "name"):
                item_label = str(item.name)
                if hasattr(item, "tag"):
                    item_label = f"{item_label} ({item.tag})"
                preview.append(item_label)
            else:
                preview.append(str(item))
            if len(preview) == 10:
                break
        formatted = ", ".join(preview)
        if len(value) > len(preview):
            formatted += f", ... (+{len(value) - len(preview)} more)"
        return formatted

    if hasattr(value, "name"):
        item_label = str(value.name)
        if hasattr(value, "tag"):
            item_label = f"{item_label} ({value.tag})"
        return item_label

    return str(value)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
client = CoCAPI(COC_API_key)


async def _fetch_war_info(interaction: discord.Interaction, clan_name: str) -> Dict[str, object]:
    if interaction.guild is None:
        raise GuildNotConfiguredError("This command can only be used inside a Discord server.")
    return await client.get_clan_war_info(clan_name, interaction.guild.id)


async def _handle_common_errors(
    interaction: discord.Interaction,
    coroutine: Callable[[], Awaitable[None]],
) -> None:
    try:
        await coroutine()
    except GuildNotConfiguredError:
        await interaction.followup.send(
            "⚠️ This server has not configured any clans yet. An administrator should run `/set_clan` first."
        )
    except ClanNotConfiguredError as exc:
        await interaction.followup.send(f"❌ {exc}")
    except discord.app_commands.AppCommandError as exc:
        await interaction.followup.send(f"⚠️ Discord command error: {exc}")
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Unable to fetch war info: {exc}")


def _register_clan_autocomplete(command: app_commands.Command) -> app_commands.Command:
    @command.autocomplete("clan_name")
    async def clan_name_autocomplete(interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        guild_config = server_config.get(interaction.guild.id, {})
        clan_tags = guild_config.get("Clan tags", {})
        current_lower = current.lower()
        suggestions = [
            app_commands.Choice(name=name, value=name)
            for name in clan_tags.keys()
            if current_lower in name.lower()
        ]
        return suggestions[:25]

    return command


def _build_choice_display(label: str) -> str:
    return label.replace("_", " ").replace("-", " ").title()


WAR_INFO_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name=_build_choice_display(label_key), value=label_value)
    for label_key, label_value in WAR_INFO_LABELS.items()
]


clan_group = app_commands.Group(
    name="clan",
    description="Access Clash of Clans data for configured clans.",
)


@clan_group.command(name="war_info", description="Display selected Clash of Clans war details.")
@app_commands.describe(
    clan_name="Configured clan name to inspect.",
    category1="Pick the first category to show.",
    category2="Pick an additional category to show (optional).",
    category3="Pick an additional category to show (optional).",
    category4="Pick an additional category to show (optional).",
    category5="Pick an additional category to show (optional).",
)
@app_commands.choices(
    category1=list(WAR_INFO_CHOICES),
    category2=list(WAR_INFO_CHOICES),
    category3=list(WAR_INFO_CHOICES),
    category4=list(WAR_INFO_CHOICES),
    category5=list(WAR_INFO_CHOICES),
)
async def war_info(
    interaction: discord.Interaction,
    clan_name: str,
    category1: app_commands.Choice[str],
    category2: Optional[app_commands.Choice[str]] = None,
    category3: Optional[app_commands.Choice[str]] = None,
    category4: Optional[app_commands.Choice[str]] = None,
    category5: Optional[app_commands.Choice[str]] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    async def execute():
        selected_sections = [
            category1.value,
            *(choice.value for choice in (category2, category3, category4, category5) if choice),
        ]

        # Remove duplicates while preserving the order the user provided.
        seen = set()
        ordered_sections: List[str] = []
        for section in selected_sections:
            if section not in seen:
                seen.add(section)
                ordered_sections.append(section)

        info = await _fetch_war_info(interaction, clan_name)

        embed = discord.Embed(
            title=f"{clan_name} — War Information",
            description="Selected details from the Clash of Clans API.",
            color=discord.Color.blue(),
        )

        for section in ordered_sections:
            value = info.get(section)
            embed.add_field(
                name=_build_choice_display(section),
                value=_format_war_info_value(section, value),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    await _handle_common_errors(interaction, execute)


_register_clan_autocomplete(war_info)
bot.tree.add_command(clan_group)


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


bot.run(Discord_Bot_API_Key)
