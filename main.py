from typing import Optional
from datetime import datetime, timedelta
from ENV.Keys import COC_API_key, Discord_Bot_API_Key, email, password, Discord_bot_test_guild_ID
from ENV.Clan_Configs import server_config
import discord
import coc
from discord.ext import commands
from discord import app_commands
from COC_API import CoCAPI, GuildNotConfiguredError, ClanNotConfiguredError
 

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

WAR_INFO_OPTION_MAP = {
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


def _format_war_info_value(label: str, value):
    """Convert war info values into readable strings for Discord responses."""
    if value is None:
        return "Not available"
    if label in {"preparation start time", "war day start time", "war ends"}:
        raw_time = None
        if isinstance(value, coc.miscmodels.Timestamp):
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
                phase_description = "Preparation phase"
            else:
                target = raw_time
                phase_description = "War" if label == "war day start time" else "War end"

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


@bot.event
async def on_ready():
    # Login to Clash of Clans API
    await client.login() 
    print(f"✅ {bot.user} is online and synced with Clash of Clans API")

    try:
        test_guild = discord.Object(id=Discord_bot_test_guild_ID)
        bot.tree.copy_global_to(guild=test_guild)
        synced = await bot.tree.sync(guild=test_guild)
        print(f"🔗 Synced {len(synced)} slash commands to guild {Discord_bot_test_guild_ID}")
    except Exception as e:
        print(f"Sync error: {e}")

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



@bot.tree.command(name="clan_war_info", description="Display the current war details for a configured clan")
@app_commands.describe(
    clan_name="Required: Name of the configured clan to inspect",
    home_clan="Select to include home clan details",
    opponent_clan="Select to include opponent clan details",
    clan_tag="Select to include the configured clan tag",
    war_tag="Select to include the unique war tag",
    war_state="Select to include the current war state",
    war_status="Select to include the war status",
    war_type="Select to include the war type",
    is_cwl="Select to include whether the war is a CWL match",
    war_size="Select to include the war size",
    attacks_per_member="Select to include the allowed attacks per member",
    total_attacks="Select to include the total attacks made so far",
    battle_modifier="Select to include any active battle modifier",
    preparation_start_time="Select to include when preparation day started",
    war_day_start_time="Select to include when war day started",
    war_ends="Select to include when the war ends",
    league_group="Select to include the associated league group",
    all_members="Select to include a list of participating members",
)
@app_commands.choices(
    home_clan=[app_commands.Choice(name="home clan",value="include")],
    opponent_clan=[app_commands.Choice(name="Include", value="include")],
    clan_tag=[app_commands.Choice(name="Include", value="include")],
    war_tag=[app_commands.Choice(name="Include", value="include")],
    war_state=[app_commands.Choice(name="Include", value="include")],
    war_status=[app_commands.Choice(name="Include", value="include")],
    war_type=[app_commands.Choice(name="Include", value="include")],
    is_cwl=[app_commands.Choice(name="Include", value="include")],
    war_size=[app_commands.Choice(name="Include", value="include")],
    attacks_per_member=[app_commands.Choice(name="Include", value="include")],
    total_attacks=[app_commands.Choice(name="Include", value="include")],
    battle_modifier=[app_commands.Choice(name="Include", value="include")],
    preparation_start_time=[app_commands.Choice(name="Include", value="include")],
    war_day_start_time=[app_commands.Choice(name="Include", value="include")],
    war_ends=[app_commands.Choice(name="Include", value="include")],
    league_group=[app_commands.Choice(name="Include", value="include")],
    all_members=[app_commands.Choice(name="Include", value="include")],
)
async def clan_war_info(
    interaction: "discord.Interaction",
    clan_name: "str",
    home_clan: "Optional[app_commands.Choice[str]]" = None,
    opponent_clan: "Optional[app_commands.Choice[str]]" = None,
    clan_tag: "Optional[app_commands.Choice[str]]" = None,
    war_tag: "Optional[app_commands.Choice[str]]" = None,
    war_state: "Optional[app_commands.Choice[str]]" = None,
    war_status: "Optional[app_commands.Choice[str]]" = None,
    war_type: "Optional[app_commands.Choice[str]]" = None,
    is_cwl: "Optional[app_commands.Choice[str]]" = None,
    war_size: "Optional[app_commands.Choice[str]]" = None,
    attacks_per_member: "Optional[app_commands.Choice[str]]" = None,
    total_attacks: "Optional[app_commands.Choice[str]]" = None,
    battle_modifier: "Optional[app_commands.Choice[str]]" = None,
    preparation_start_time: "Optional[app_commands.Choice[str]]" = None,
    war_day_start_time: "Optional[app_commands.Choice[str]]" = None,
    war_ends: "Optional[app_commands.Choice[str]]" = None,
    league_group: "Optional[app_commands.Choice[str]]" = None,
    all_members: "Optional[app_commands.Choice[str]]" = None,
):
    if interaction.guild is None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❌ This command is only available inside a Discord server.")
        return

    await interaction.response.defer(thinking=True)

    try:
        info = await client.get_clan_war_info(clan_name, interaction.guild.id) 
    except GuildNotConfiguredError:
        await interaction.followup.send(
            "⚠️ This server has not configured any clans yet. An administrator should run `/set_clan` first."
        )
        return
    except ClanNotConfiguredError:
        await interaction.followup.send(
            f"❌ `{clan_name}` is not configured for this server. Check the name or run `/set_clan` to add it."
        )
        return
    except coc.errors.PrivateWarLog:
        await interaction.followup.send("⚠️ This clan's war log is private.")
        return
    except coc.errors.NotFound:
        await interaction.followup.send("❌ Clan or war information not found.")
        return
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Unable to fetch war info: {exc}")
        return

    option_flags = {
        "home_clan": home_clan is not None,
        "opponent_clan": opponent_clan is not None,
        "clan_tag": clan_tag is not None,
        "war_tag": war_tag is not None,
        "war_state": war_state is not None,
        "war_status": war_status is not None,
        "war_type": war_type is not None,
        "is_cwl": is_cwl is not None,
        "war_size": war_size is not None,
        "attacks_per_member": attacks_per_member is not None,
        "total_attacks": total_attacks is not None,
        "battle_modifier": battle_modifier is not None,
        "preparation_start_time": preparation_start_time is not None,
        "war_day_start_time": war_day_start_time is not None,
        "war_ends": war_ends is not None,
        "league_group": league_group is not None,
        "all_members": all_members is not None,
    }

    requested_labels = [
        WAR_INFO_OPTION_MAP[key] for key, enabled in option_flags.items() if enabled
    ]

    if not requested_labels:
        requested_labels = list(info.keys())

    embed = discord.Embed(
        title=f"{clan_name} Current War Information",
        color=discord.Color.blue()
    )

    for label in requested_labels:
        value = info.get(label)
        embed.add_field(
            name=label.title(),
            value=_format_war_info_value(label, value),
            inline=False
        )

    await interaction.followup.send(embed=embed)

@clan_war_info.autocomplete("clan_name")
async def clan_name_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []

    guild_cfg = server_config.get(interaction.guild.id, {})
    clan_tags = guild_cfg.get("Clan tags", {})

    suggestions = [
        app_commands.Choice(name=name, value=name)
        for name in clan_tags.keys()
        if current.lower() in name.lower()
    ]
    return suggestions[:25]  # Discord max

bot.run(Dkey)
