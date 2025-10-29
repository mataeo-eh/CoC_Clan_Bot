from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import tasks

import coc

from bot_core import bot, client
from logger import get_logger, log_command_call

log = get_logger()
from COC_API import ClanNotConfiguredError, GuildNotConfiguredError
from ENV.Clan_Configs import save_server_config, server_config


MAX_MESSAGE_LENGTH = 1900
ALERT_ROLE_NAME = "War Alerts"
# Matches the poll frequency of the background alert loop (5 minutes).
ALERT_WINDOW_SECONDS = 300
README_URL = "https://github.com/mataeo/COC_Clan_Bot/blob/main/README.md"

# Cache of alert milestones sent per (guild, clan, war) tuple to avoid duplicates.
alert_state: Dict[Tuple[int, str, str], Set[str]] = {}


def _chunk_content(content: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split content into manageable chunks that respect Discord's 2000-character limit."""
    if not content:
        return ["(no data)"]

    lines = content.split("\n")
    chunks: List[str] = []
    current = ""

    for line in lines:
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue

        if len(current) + len(line) + (1 if current else 0) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)

    return chunks or ["(no data)"]


async def send_text_response(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = False,
    view: Optional[discord.ui.View] = None,
) -> None:
    """Send a text response, splitting into multiple messages when necessary."""
    log.debug("send_text_response called (ephemeral=%s, has_view=%s)", ephemeral, bool(view))
    chunks = _chunk_content(content)
    first_sender = (
        interaction.response.send_message
        if not interaction.response.is_done()
        else interaction.followup.send
    )

    first_chunk = chunks[0]
    log.debug("send_text_response sending first chunk (length=%d)", len(first_chunk))
    if view is not None:
        await first_sender(first_chunk, ephemeral=ephemeral, view=view)
    else:
        await first_sender(first_chunk, ephemeral=ephemeral)
    for chunk in chunks[1:]:
        log.debug("send_text_response sending follow-up chunk (length=%d)", len(chunk))
        await interaction.followup.send(chunk, ephemeral=ephemeral)


def _timestamp_to_datetime(ts: Optional[coc.Timestamp]) -> Optional[datetime]:
    """Convert a CoC timestamp wrapper into a timezone-aware datetime."""
    log.debug("_timestamp_to_datetime invoked")
    if ts is None:
        return None
    if hasattr(ts, "time"):
        return ts.time
    if isinstance(ts, datetime):
        return ts
    return None


def _find_alert_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Select a text channel where the bot can post war alerts."""
    log.debug("_find_alert_channel invoked")
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
    return None


async def send_channel_message(channel: discord.TextChannel, content: str) -> None:
    """Post text content to a channel, splitting when Discord's limit is exceeded."""
    log.debug("send_channel_message called")
    for chunk in _chunk_content(content):
        log.debug("send_channel_message chunk length=%d", len(chunk))
        await channel.send(chunk)


def _alert_key(guild_id: int, clan_name: str, war_tag: str) -> Tuple[int, str, str]:
    """Build the dictionary key used for alert de-duplication."""
    return guild_id, clan_name, war_tag


def _mark_alert_sent(guild_id: int, clan_name: str, war_tag: str, alert_id: str) -> bool:
    """Record an alert and return True if it has not been sent before."""
    sent = alert_state.setdefault(_alert_key(guild_id, clan_name, war_tag), set())
    if alert_id in sent:
        return False
    sent.add(alert_id)
    return True


def _within_threshold_window(value: Optional[float], *, threshold: float) -> bool:
    """Return True when a countdown is within the configured alert window of a threshold."""
    if value is None:
        return False
    if value < 0 or value > threshold:
        return False
    return (threshold - value) <= ALERT_WINDOW_SECONDS


def _elapsed_within_window(value: Optional[float], *, target: float) -> bool:
    """Return True when elapsed time since a milestone is inside the alert window."""
    if value is None:
        return False
    if value < target:
        return False
    return (value - target) <= ALERT_WINDOW_SECONDS


def _normalise_player_tag(raw_tag: str) -> Optional[str]:
    """Return a standardised player tag with a leading #."""
    if not isinstance(raw_tag, str):
        return None
    cleaned = raw_tag.strip().upper()
    if not cleaned:
        return None
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned.lstrip('#')}"
    return cleaned


# ---------------------------------------------------------------------------
# Slash command: /set_clan
# ---------------------------------------------------------------------------

@bot.tree.command(name="set_clan", description="Set a default clan for this server")
@app_commands.describe(
    clan_name="Name of the clan",
    tag="Clan tag (e.g. #ABC123)",
    enable_alerts="Whether automatic war alerts should be enabled for this clan",
)
async def set_clan(
    interaction: discord.Interaction,
    clan_name: str,
    tag: str,
    enable_alerts: bool,
):
    """Allow administrators to bind a clan name to its Clash of Clans tag."""
    log_command_call("set_clan")
    log.debug("set_clan invoked")
    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ You need the Administrator permission to configure this command.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    guild_id = guild.id
    normalized_tag = tag.upper()
    guild_config = _ensure_guild_config(guild_id)
    clans = guild_config["clans"]

    # Detect duplicate tags under a different clan alias.
    conflicting_name = next(
        (
            name
            for name, data in clans.items()
            if name != clan_name
            and isinstance(data, dict)
            and str(data.get("tag", "")).upper() == normalized_tag
        ),
        None,
    )

    if conflicting_name:
        log.debug(
            "set_clan detected duplicate tag %s between %s and %s in guild %s",
            normalized_tag,
            conflicting_name,
            clan_name,
            guild_id,
        )
        view = ReplaceClanTagView(
            guild=guild,
            existing_name=conflicting_name,
            new_name=clan_name,
            tag=normalized_tag,
            enable_alerts=enable_alerts,
        )
        await send_text_response(
            interaction,
            (
                f"⚠️ The tag {normalized_tag} is already linked to `{conflicting_name}`.\n"
                "Would you like to replace that clan name with this new one?"
            ),
            ephemeral=True,
            view=view,
        )
        return

    response, followup = _apply_clan_update(guild, clan_name, normalized_tag, enable_alerts)
    await send_text_response(interaction, response, ephemeral=True)
    if followup:
        await interaction.followup.send(followup, ephemeral=True)


@bot.tree.command(name="help", description="Show a quick primer on using the Clan Bot.")
async def help_command(interaction: discord.Interaction):
    """Provide a concise overview plus a link to the full documentation."""
    log_command_call("help")
    log.debug("help_command invoked")
    summary = (
        "Clan_Bot keeps your Clash of Clans server organised—fetch war intel, assign bases, "
        "and share updates with just a few prompts."
    )
    message = (
        f"{summary}\n\n"
        f"📘 Full guide: {README_URL}\n"
        "Tip: After entering any command’s required options, press enter to run it. "
        "Interactive menus or buttons appear right afterward to guide the rest of the workflow."
    )
    await send_text_response(interaction, message, ephemeral=True)


@bot.tree.command(
    name="choose_war_alert_channel",
    description="Select the text channel where war alerts will be posted for a clan.",
)
@app_commands.describe(clan_name="Choose a configured clan to update.")
async def choose_war_alert_channel(interaction: discord.Interaction, clan_name: str):
    """Allow administrators to pick the destination channel for war alerts."""
    log_command_call("choose_war_alert_channel")
    log.debug("choose_war_alert_channel invoked for %s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can configure alert destinations.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    guild_config = _ensure_guild_config(guild.id)
    clan_entry = guild_config["clans"].get(clan_name)
    if not isinstance(clan_entry, dict):
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured for this server.",
            ephemeral=True,
        )
        return

    bot_member = guild.me
    if bot_member is None:
        await send_text_response(
            interaction,
            "⚠️ I cannot resolve my guild membership to check channel permissions.",
            ephemeral=True,
        )
        return

    # Build category -> channel mapping only including channels both the bot and caller can use.
    channels_by_category: Dict[Optional[int], List[discord.TextChannel]] = {}
    def sort_key(ch: discord.TextChannel) -> Tuple[int, int, int]:
        category_position = ch.category.position if ch.category else -1
        return (category_position, ch.position, ch.id)

    for channel in sorted(guild.text_channels, key=sort_key):
        if not channel.permissions_for(bot_member).send_messages:
            continue
        if not channel.permissions_for(member).view_channel:
            continue
        category_id = channel.category_id
        channels_by_category.setdefault(category_id, []).append(channel)

    channels_by_category = {
        key: value for key, value in channels_by_category.items() if value
    }

    if not channels_by_category:
        await send_text_response(
            interaction,
            "⚠️ I could not find any text channels that both of us can access. "
            "Please adjust permissions or create a suitable channel first.",
            ephemeral=True,
        )
        return

    alerts = clan_entry.get("alerts", {})
    existing_channel_id = alerts.get("channel_id")
    if existing_channel_id:
        existing_channel = guild.get_channel(existing_channel_id)
        current_status = (
            f"Current alert channel: {existing_channel.mention}"
            if isinstance(existing_channel, discord.TextChannel)
            else f"Current alert channel ID: {existing_channel_id}"
        )
    else:
        current_status = (
            "Alerts currently use the default fallback channel until a dedicated one is selected."
        )

    view = ChooseWarAlertChannelView(
        guild=guild,
        clan_name=clan_name,
        channels_by_category=channels_by_category,
    )
    intro = (
        f"{current_status}\n"
        "1️⃣ Pick a channel category below, 2️⃣ choose the exact text channel, then 3️⃣ confirm the selection. "
        "Alerts use the channel you select as soon as you finish the flow."
    )
    await send_text_response(interaction, intro, ephemeral=True, view=view)


@bot.tree.command(
    name="link_player",
    description="Link or unlink Clash of Clans player tags to Discord members.",
)
@app_commands.describe(
    action="Choose 'link' to add a tag or 'unlink' to remove one.",
    player_tag="Player tag (e.g. #ABC123). The tag will be validated before saving.",
    alias="Optional nickname that appears in autocomplete. Defaults to the in-game name.",
    target_member="Only admins may manage tags for someone else.",
)
async def link_player(
    interaction: discord.Interaction,
    action: Literal["link", "unlink"],
    player_tag: str,
    alias: Optional[str] = None,
    target_member: Optional[discord.Member] = None,
):
    """Allow members to map their Discord identity to one or more Clash of Clans accounts."""
    log_command_call("link_player")
    log.debug(
        "link_player invoked action=%s tag=%s target=%s",
        action,
        player_tag,
        target_member.id if isinstance(target_member, discord.Member) else None,
    )

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    actor = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if actor is None:
        await send_text_response(
            interaction,
            "⚠️ I could not resolve your guild membership. Please try again.",
            ephemeral=True,
        )
        return

    target = target_member or actor
    action_lower = action.lower()
    normalized_tag = _normalise_player_tag(player_tag)
    if normalized_tag is None:
        await send_text_response(
            interaction,
            "⚠️ Please provide a valid player tag (for example `#ABC123`).",
            ephemeral=True,
        )
        return

    if target != actor and not actor.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can manage linked tags for other members.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    guild_config = _ensure_guild_config(guild.id)
    accounts = guild_config.setdefault("player_accounts", {})
    user_key = str(target.id)
    existing_entries = accounts.setdefault(user_key, [])

    if action_lower == "link":
        try:
            player_payload = await client.get_player(normalized_tag)
        except coc.errors.NotFound:
            await send_text_response(
                interaction,
                f"⚠️ I couldn't find a Clash of Clans profile with tag `{normalized_tag}`.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.exception("Unexpected error while linking player")
            await send_text_response(
                interaction,
                f"⚠️ Unable to verify that tag right now: {exc}",
                ephemeral=True,
            )
            return

        inferred_alias = alias.strip() if isinstance(alias, str) and alias.strip() else None
        if inferred_alias is None:
            inferred_alias = player_payload.get("profile", {}).get("name")
        if inferred_alias:
            inferred_alias = inferred_alias.strip()

        # Update existing entry if the tag is already linked.
        updated = False
        for record in existing_entries:
            if isinstance(record, dict) and record.get("tag") == normalized_tag:
                record["alias"] = inferred_alias
                updated = True
                break
        if not updated:
            existing_entries.append({"tag": normalized_tag, "alias": inferred_alias})

        save_server_config()
        alias_note = f" as `{inferred_alias}`" if inferred_alias else ""
        target_label = target.display_name if isinstance(target, discord.Member) else target.id
        await send_text_response(
            interaction,
            f"✅ Linked `{normalized_tag}`{alias_note} to {target_label}. "
            "You can now reference it quickly with `/player_info`.",
            ephemeral=True,
        )
        return

    if action_lower == "unlink":
        before = len(existing_entries)
        existing_entries[:] = [
            entry
            for entry in existing_entries
            if not (isinstance(entry, dict) and entry.get("tag") == normalized_tag)
        ]
        if not existing_entries:
            accounts.pop(user_key, None)
        if before == len(existing_entries):
            await send_text_response(
                interaction,
                f"⚠️ No link for `{normalized_tag}` was found for that member.",
                ephemeral=True,
            )
            return
        save_server_config()
        target_label = target.display_name if isinstance(target, discord.Member) else target.id
        await send_text_response(
            interaction,
            f"🗑️ Removed `{normalized_tag}` from {target_label}'s linked accounts.",
            ephemeral=True,
        )
        return

    await send_text_response(
        interaction,
        "⚠️ Please choose either 'link' or 'unlink' for the action.",
        ephemeral=True,
    )


@bot.tree.command(
    name="player_info",
    description="Display detailed information about a Clash of Clans player.",
)
@app_commands.describe(
    player_reference="Enter a player tag (e.g. #ABC123) or select a saved player name."
)
async def player_info(interaction: discord.Interaction, player_reference: str):
    """Provide an interactive view of player data with share controls."""
    log_command_call("player_info")
    log.debug("player_info invoked with reference %s", player_reference)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server so I can load saved player tags.",
            ephemeral=True,
        )
        return

    reference = player_reference.strip()
    if not reference:
        await send_text_response(
            interaction,
            "⚠️ Please provide a player tag (e.g. #ABC123) or choose a saved player name.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    guild_config = _ensure_guild_config(guild.id)
    player_tags: Dict[str, str] = guild_config.get("player_tags", {})
    player_accounts: Dict[str, List[Dict[str, Optional[str]]]] = guild_config.get("player_accounts", {})

    alias_lookup: Dict[str, str] = {}
    mention_lookup: Dict[str, str] = {}
    for user_id_str, records in player_accounts.items():
        if not isinstance(records, list):
            continue
        member = None
        if user_id_str.isdigit():
            member = guild.get_member(int(user_id_str))
        display_name = member.display_name if member else None
        first_tag: Optional[str] = None
        for record in records:
            if not isinstance(record, dict):
                continue
            tag = record.get("tag")
            normalised_tag = _normalise_player_tag(tag) if isinstance(tag, str) else None
            if normalised_tag is None:
                continue
            alias = record.get("alias")
            if isinstance(alias, str) and alias.strip():
                alias_lookup[alias.strip().lower()] = normalised_tag
            if display_name:
                alias_lookup.setdefault(display_name.lower(), normalised_tag)
            first_tag = first_tag or normalised_tag
        if first_tag:
            mention_lookup[user_id_str] = first_tag
            if display_name:
                alias_lookup.setdefault(f"@{display_name.lower()}", first_tag)

    normalised_reference = reference.upper() if reference.startswith("#") else None
    player_tag: Optional[str] = None

    if normalised_reference:
        player_tag = _normalise_player_tag(reference)
    else:
        lowered = reference.lower()
        # Mentions arrive as <@123> or <@!123>
        if reference.startswith("<@") and reference.endswith(">"):
            candidate = reference.strip("<@!>")
            player_tag = mention_lookup.get(candidate)
        if player_tag is None:
            player_tag = alias_lookup.get(lowered)
        if player_tag is None:
            player_tag = alias_lookup.get(reference)
        if player_tag is None:
            mapped_tag = player_tags.get(reference)
            if mapped_tag is None:
                mapped_tag = player_tags.get(reference.title())
            if mapped_tag is None:
                mapped_tag = player_tags.get(reference.lower())
            if mapped_tag:
                player_tag = _normalise_player_tag(mapped_tag)
        if player_tag is None:
            mapped_tag = alias_lookup.get(lowered.strip())
            if mapped_tag:
                player_tag = mapped_tag
        if player_tag is None:
            mapped_tag = player_tags.get(reference.lower())
            if mapped_tag:
                player_tag = _normalise_player_tag(mapped_tag)
        if player_tag is None:
            mapped_tag = player_tags.get(reference)
            if mapped_tag:
                player_tag = _normalise_player_tag(mapped_tag)
        if player_tag is None:
            # If the user typed a raw tag without leading '#'
            potential_tag = _normalise_player_tag(reference)
            if potential_tag and potential_tag.lstrip("#").isalnum():
                player_tag = potential_tag

    if player_tag is None:
        await send_text_response(
            interaction,
            (
                f"⚠️ I could not find a saved player named `{reference}`.\n"
                "Provide a full player tag like `#ABC123` or link the account with `/link_player` first."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        player_info = await client.get_player(player_tag)
    except coc.errors.NotFound:
        await interaction.followup.send(f"⚠️ I could not find a player with tag `{player_tag}`.", ephemeral=True)
        return
    except coc.errors.GatewayError as exc:
        await interaction.followup.send(
            f"⚠️ Clash of Clans API error while fetching `{player_tag}`: {exc}", ephemeral=True
        )
        return
    except Exception as exc:
        log.exception("Unexpected error retrieving player data")
        await interaction.followup.send(f"⚠️ Unable to fetch player info: {exc}", ephemeral=True)
        return

    profile = player_info.get("profile", {})
    player_name = profile.get("name") or "Unknown Player"
    header = f"{player_name} ({player_tag})"

    view = PlayerInfoView(header, player_info)
    initial_output = _build_player_output(header, [], player_info)
    view.last_output = initial_output
    await interaction.followup.send(initial_output, ephemeral=True, view=view)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

WAR_INFO_FIELD_MAP: Dict[str, str] = {
    "home clan": "Home clan overview",
    "opponent clan": "Opponent clan overview",
    "clan tag": "Registered clan tag",
    "war tag": "Unique war identifier",
    "war state": "Current war state",
    "war status": "War result status",
    "war type": "War classification",
    "is cwl": "Is this a CWL war?",
    "war size": "War size",
    "attacks per member": "Attacks per member",
    "all attacks done this war": "Total attacks launched so far",
    "battle modifier": "Battle modifier",
    "preparation start time": "Preparation phase remaining",
    "war day start time": "War start countdown",
    "war end time": "War end countdown",
    "league group": "League group summary",
    "all members in war": "Clan members participating",
}


PLAYER_INFO_FIELD_MAP: Dict[str, str] = {
    "profile": "Profile overview",
    "clan": "Current clan",
    "league": "League status",
    "trophies_overview": "Trophies overview",
    "war_stats": "War statistics",
    "donations": "Donation summary",
    "heroes": "Hero levels",
    "troops": "Troop levels",
    "spells": "Spell levels",
    "achievements": "Achievement highlights",
}


def _fmt_numeric(value: Optional[int]) -> str:
    return f"{value:,}" if isinstance(value, int) else "N/A"


def _format_unit_list(units: List[Dict[str, Any]], *, limit: int = 10, label: str = "Unit") -> str:
    if not units:
        return f"No {label.lower()} data available."
    lines = []
    for entry in units[:limit]:
        name = entry.get("name", "Unknown")
        level = entry.get("level")
        max_level = entry.get("max_level")
        village = entry.get("village")
        category = entry.get("category")
        suffix_parts = []
        if max_level:
            suffix_parts.append(f"max {max_level}")
        if village:
            suffix_parts.append(village.replace("_", " ").title())
        if category:
            suffix_parts.append(category.title())
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(f"• {name}: Lv{level}{suffix}")
    if len(units) > limit:
        lines.append(f"… (+{len(units) - limit} more)")
    return "\n".join(lines)


def _format_achievement_list(achievements: List[Dict[str, Any]], *, limit: int = 5) -> str:
    if not achievements:
        return "No achievements recorded."
    sorted_achievements = sorted(achievements, key=lambda item: item.get("stars", 0), reverse=True)
    lines = []
    for achievement in sorted_achievements[:limit]:
        name = achievement.get("name", "Unknown")
        stars = achievement.get("stars", 0)
        value = achievement.get("value", 0)
        target = achievement.get("target")
        info = achievement.get("info")
        progress = (
            f"{value:,}/{target:,}" if isinstance(value, int) and isinstance(target, int) and target else f"{value:,}"
        )
        detail = f"• {name}: ⭐ {stars} — {progress}"
        if info:
            detail += f" ({info})"
        lines.append(detail)
    if len(sorted_achievements) > limit:
        lines.append(f"… (+{len(sorted_achievements) - limit} more)")
    return "\n".join(lines)


def _format_timestamp_delta(source: datetime, duration_hours: int = 0) -> str:
    """Format a timestamp relative to now as an hours/minutes/seconds countdown."""
    if source.tzinfo is not None:
        now = datetime.now(source.tzinfo)
    else:
        now = datetime.utcnow()
    target = source + timedelta(hours=duration_hours)
    remaining = target - now
    if remaining.total_seconds() <= 0:
        return "Completed"
    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _format_war_value(key: str, value) -> str:
    """Human readable formatter for war information values."""
    log.debug("_format_war_value invoked for key %s", key)
    if value is None:
        return "Not available"

    if key == "all members in war" and isinstance(value, Iterable):
        members: List[str] = []
        for member in value:
            name = getattr(member, "name", "Unknown")
            th = getattr(member, "town_hall", "?")
            stars = getattr(member, "star_count", 0)
            members.append(f"{name} (TH{th}) ⭐ {stars}")
        return "\n".join(members) if members else "No members listed."

    if key == "all attacks done this war" and isinstance(value, Iterable):
        try:
            count = len(value)
        except TypeError:
            count = sum(1 for _ in value)
        return "No attacks launched" if count == 0 else f"{count} attacks launched"

    if key in {"preparation start time", "war day start time", "war end time"}:
        source = getattr(value, "time", value)
        if not isinstance(source, datetime):
            return str(value)
        if source.tzinfo is None:
            source = source.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        if key == "war day start time":
            if now >= source:
                return "War Started"
            return f"War begins in: {_format_timestamp_delta(source, 0)}"
        if key == "war end time":
            if now >= source:
                return "War Ended"
            return f"War ends in: {_format_timestamp_delta(source, 0)}"
        delta_text = _format_timestamp_delta(source, 24)
        return "Preparation Complete" if delta_text == "Completed" else f"Preparation phase remaining: {delta_text}"

    if key in {"home clan", "opponent clan"} and hasattr(value, "name"):
        return (
            f"{value.name} (TH avg unknown) — Stars: {getattr(value, 'stars', 'N/A')} "
            f"| Attacks used: {getattr(value, 'attacks_used', 'N/A')} "
            f"| Destruction: {getattr(value, 'destruction', 'N/A')}%"
        )

    if key == "league group" and hasattr(value, "season"):
        return f"Season {value.season} • State: {value.state}"

    if isinstance(value, bool):
        return "Yes" if value else "No"

    if hasattr(value, "name"):
        name = getattr(value, "name")
        tag = getattr(value, "tag", None)
        return f"{name} ({tag})" if tag else name

    if isinstance(value, (list, tuple)):
        preview = ", ".join(str(item) for item in value[:10])
        if len(value) > 10:
            preview += f", … (+{len(value) - 10} more)"
        return preview

    return str(value)


def _build_war_output(clan_name: str, selections: List[str], war_info: Dict[str, object]) -> str:
    """Render the selected war information fields into plain text."""
    log.debug("_build_war_output invoked for clan %s", clan_name)
    lines: List[str] = [f"**{clan_name} — War Snapshot**"]
    if not selections:
        lines.append(
            "After submitting the command, use the dropdown below to choose which war details to view. "
            "The buttons let you broadcast the current selection or keep a private copy."
        )
        return "\n".join(lines)

    for key in selections:
        label = WAR_INFO_FIELD_MAP.get(key, key.title())
        value = _format_war_value(key, war_info.get(key))
        lines.append(f"**{label}:**\n{value}")
    return "\n\n".join(lines)


def _format_player_value(key: str, player_info: Dict[str, Any]) -> str:
    """Human readable formatter for player information values."""
    if key == "profile":
        profile = player_info.get("profile", {})
        return (
            f"Name: {profile.get('name', 'Unknown')}\n"
            f"Tag: {profile.get('tag', 'N/A')}\n"
            f"Exp Level: {_fmt_numeric(profile.get('exp_level'))}\n"
            f"Town Hall: TH{profile.get('town_hall_level') or '?'}\n"
            f"Builder Hall: BH{profile.get('builder_hall_level') or '?'}"
        )

    if key == "clan":
        clan = player_info.get("clan", {})
        if not clan.get("name"):
            return "Not currently in a clan."
        return (
            f"Clan: {clan.get('name')}\n"
            f"Tag: {clan.get('tag', 'N/A')}\n"
            f"Role: {str(clan.get('role') or 'Member').replace('_', ' ').title()}"
        )

    if key == "league":
        league = player_info.get("league")
        return league or "Unranked"

    if key == "trophies_overview":
        return (
            f"Home: {_fmt_numeric(player_info.get('trophies'))} "
            f"(Best: {_fmt_numeric(player_info.get('best_trophies'))})\n"
            f"Versus: {_fmt_numeric(player_info.get('versus_trophies'))}"
        )

    if key == "war_stats":
        return (
            f"War stars: {_fmt_numeric(player_info.get('war_stars'))}\n"
            f"Attack wins: {_fmt_numeric(player_info.get('attack_wins'))}\n"
            f"Defense wins: {_fmt_numeric(player_info.get('defense_wins'))}"
        )

    if key == "donations":
        return (
            f"Donations sent: {_fmt_numeric(player_info.get('donations'))}\n"
            f"Donations received: {_fmt_numeric(player_info.get('donations_received'))}"
        )

    if key == "heroes":
        return _format_unit_list(player_info.get("heroes", []), label="Hero")

    if key == "troops":
        return _format_unit_list(player_info.get("troops", []), label="Troop")

    if key == "spells":
        return _format_unit_list(player_info.get("spells", []), label="Spell")

    if key == "achievements":
        return _format_achievement_list(player_info.get("achievements", []))

    value = player_info.get(key)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "Not available"
    return str(value)


def _build_player_output(player_label: str, selections: List[str], player_info: Dict[str, Any]) -> str:
    """Render the selected player information fields into plain text."""
    log.debug("_build_player_output invoked for player %s", player_label)
    lines: List[str] = [f"**{player_label} — Player Snapshot**"]
    if not selections:
        lines.append(
            "After the command sends, pick the player details you need from the dropdown. "
            "Use the buttons to broadcast the current view or grab a private copy."
        )
        return "\n".join(lines)

    for key in selections:
        label = PLAYER_INFO_FIELD_MAP.get(key, key.replace("_", " ").title())
        value = _format_player_value(key, player_info)
        lines.append(f"**{label}:**\n{value}")
    return "\n\n".join(lines)


def _normalise_player_accounts_map(raw: Any) -> Dict[str, List[Dict[str, Optional[str]]]]:
    """Ensure player account mappings use the expected structure."""
    if not isinstance(raw, dict):
        return {}

    normalised: Dict[str, List[Dict[str, Optional[str]]]] = {}
    for user_id, records in raw.items():
        key = str(user_id)
        entries: List[Dict[str, Optional[str]]] = []
        if isinstance(records, list):
            source_iterable = records
        elif isinstance(records, dict):
            source_iterable = [{"alias": alias, "tag": tag} for alias, tag in records.items()]
        else:
            continue

        for record in source_iterable:
            if isinstance(record, dict):
                tag = record.get("tag")
                if not isinstance(tag, str) or not tag.strip():
                    continue
                alias = record.get("alias")
                entries.append(
                    {
                        "tag": tag.strip().upper(),
                        "alias": alias.strip() if isinstance(alias, str) and alias.strip() else None,
                    }
                )
            elif isinstance(record, str) and record.strip():
                entries.append({"tag": record.strip().upper(), "alias": None})
        if entries:
            normalised[key] = entries
    return normalised


def _ensure_guild_config(guild_id: int) -> Dict[str, Any]:
    """Return the guild config, ensuring required keys exist."""
    guild_config = server_config.setdefault(guild_id, {"clans": {}, "player_tags": {}})
    clans = guild_config.setdefault("clans", {})
    for clan_data in clans.values():
        if not isinstance(clan_data, dict):
            continue
        alerts = clan_data.setdefault("alerts", {})
        if not isinstance(alerts, dict):
            clan_data["alerts"] = {"enabled": True, "channel_id": None}
        else:
            alerts.setdefault("enabled", True)
            alerts.setdefault("channel_id", None)
    guild_config.setdefault("player_tags", {})
    accounts = _normalise_player_accounts_map(guild_config.get("player_accounts", {}))
    guild_config["player_accounts"] = accounts
    return guild_config


def _clan_names_for_guild(guild_id: int) -> Dict[str, str]:
    """Return a mapping of clan name -> tag for a guild."""
    log.debug("_clan_names_for_guild called")
    guild_config = _ensure_guild_config(guild_id)
    clans = guild_config.get("clans", {}) or {}
    return {
        name: data.get("tag")
        for name, data in clans.items()
        if isinstance(data, dict) and data.get("tag")
    }


def _apply_clan_update(
    guild: discord.Guild,
    clan_name: str,
    tag: str,
    enable_alerts: bool,
    *,
    preserve_channel: Optional[int] = None,
) -> Tuple[str, Optional[str]]:
    """Persist clan details and return response and follow-up messages."""
    guild_config = _ensure_guild_config(guild.id)
    clans = guild_config["clans"]

    previous_entry = clans.get(clan_name, {})
    previous_alerts = previous_entry.get("alerts", {}) if isinstance(previous_entry, dict) else {}
    previous_enabled = bool(previous_alerts.get("enabled", False))
    previous_channel = (
        preserve_channel
        if preserve_channel is not None
        else previous_alerts.get("channel_id")
    )

    client.set_server_clan(guild.id, clan_name, tag, alerts_enabled=enable_alerts)

    updated_entry = clans.get(clan_name, {})
    alerts = updated_entry.setdefault("alerts", {"enabled": enable_alerts, "channel_id": None})

    if previous_channel is not None:
        alerts["channel_id"] = previous_channel
    elif enable_alerts and (not previous_enabled or alerts.get("channel_id") is None):
        fallback_channel = _find_alert_channel(guild)
        if fallback_channel:
            alerts["channel_id"] = fallback_channel.id

    save_server_config()

    response = (
        f"✅ `{clan_name}` now points to {tag.upper()} for this server.\n"
        f"📣 War alerts enabled: {'Yes' if enable_alerts else 'No'}."
    )

    followup: Optional[str] = None
    if enable_alerts:
        channel_id = alerts.get("channel_id")
        if channel_id:
            channel_obj = guild.get_channel(channel_id)
            channel_reference = channel_obj.mention if isinstance(channel_obj, discord.TextChannel) else f"<#{channel_id}>"
            followup = (
                f"ℹ️ Alerts for `{clan_name}` will post in {channel_reference} unless you choose another channel "
                "with `/choose_war_alert_channel`."
            )
        else:
            followup = (
                "⚠️ I could not find a default channel to use for alerts. "
                "Please run `/choose_war_alert_channel` to pick one manually."
            )
    return response, followup


def _format_alert_message(role: Optional[discord.Role], message: str) -> str:
    """Prefix alert text with the subscribed role mention when available."""
    log.debug("_format_alert_message invoked")
    prefix = f"{role.mention} " if role else ""
    return f"{prefix}{message}".strip()


def _collect_war_alerts(
    guild: discord.Guild,
    clan_name: str,
    tag: str,
    war: coc.wars.ClanWar,
    role: Optional[discord.Role],
    now: datetime,
) -> List[str]:
    """Determine which alerts should fire for the current war snapshot."""
    log.debug("_collect_war_alerts invoked")
    state_value_str = war.state.value if hasattr(war.state, 'value') else war.state
    if state_value_str in {'notInWar', 'inMatchmaking'}:
        return []

    messages: List[str] = []  # Collected alert strings to return
    war_tag = war.war_tag or tag
    start_dt = _timestamp_to_datetime(war.start_time)
    end_dt = _timestamp_to_datetime(war.end_time)
    if start_dt and start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt and end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    start_seconds_remaining = (
        (start_dt - now).total_seconds() if start_dt is not None else None
    )
    end_seconds_remaining = (
        (end_dt - now).total_seconds() if end_dt is not None else None
    )
    seconds_since_start = (
        (now - start_dt).total_seconds() if start_dt is not None else None
    )
    seconds_since_end = (
        (now - end_dt).total_seconds() if end_dt is not None else None
    )

    def queue(alert_id: str, text: str) -> None:
        """Queue alert text when it has not already been sent."""
        if _mark_alert_sent(guild.id, clan_name, war_tag, alert_id):
            messages.append(_format_alert_message(role, text))

    if state_value_str in {'preparation', 'inWar'}:
        if _within_threshold_window(start_seconds_remaining, threshold=3600):
            queue("start_1h", f"War for {clan_name} starts in 1 hour.")
        if _within_threshold_window(start_seconds_remaining, threshold=300):
            queue("start_5m", f"War for {clan_name} starts in 5 minutes.")

    if state_value_str in {'inWar', 'warEnded'}:
        if _elapsed_within_window(seconds_since_start, target=300):
            queue("start_plus_5m", f"War for {clan_name} started 5 minutes ago. Good luck!")

    if state_value_str in {'preparation', 'inWar'}:
        if _within_threshold_window(end_seconds_remaining, threshold=43200):
            queue("end_12h", f"War for {clan_name} ends in 12 hours.")
        if _within_threshold_window(end_seconds_remaining, threshold=3600):
            queue("end_1h", f"War for {clan_name} ends in 1 hour.")
        if _within_threshold_window(end_seconds_remaining, threshold=300):
            queue("end_5m", f"War for {clan_name} ends in 5 minutes.")

    if state_value_str in {'warEnded', 'inWar'}:
        if _elapsed_within_window(seconds_since_end, target=0):
            home_stars = getattr(war.clan, 'stars', '?')
            enemy_stars = getattr(war.opponent, 'stars', '?')
            status_raw = war.status.value if hasattr(war.status, 'value') else war.status
            status_value = status_raw or state_value_str
            queue(
                'end_result',
                (
                    f"War for {clan_name} versus {war.opponent.name} has {status_value}. "
                    f"Final stars: {home_stars}-{enemy_stars}."
                ),
            )

    return messages




# Poll every five minutes so 5-minute alert thresholds are respected.
@tasks.loop(minutes=5)
async def war_alert_loop() -> None:
    """Poll tracked clans and emit time-based war reminders."""
    log.debug("war_alert_loop tick")
    now = datetime.now(timezone.utc)
    for guild_id, config in server_config.items():
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue  # Skip guilds the bot is not currently connected to

        clans: Dict[str, Dict[str, Any]] = config.get("clans", {})  # type: ignore[assignment]
        if not clans:
            continue  # Nothing configured for this guild

        alert_role = discord.utils.get(guild.roles, name=ALERT_ROLE_NAME)
        default_channel = _find_alert_channel(guild)

        for clan_name, clan_data in clans.items():
            if not isinstance(clan_data, dict):
                continue
            tag = clan_data.get("tag")
            if not tag:
                continue

            alerts_cfg = clan_data.get("alerts", {}) if isinstance(clan_data.get("alerts", {}), dict) else {}
            if not alerts_cfg.get("enabled", True):
                continue  # Admins disabled tracking for this clan

            target_channel: Optional[discord.TextChannel]
            channel_id = alerts_cfg.get("channel_id")
            if channel_id:
                candidate = guild.get_channel(channel_id)
                if not isinstance(candidate, discord.TextChannel):
                    log.debug(
                        "Skipping alerts for %s in guild %s: stored channel missing",
                        clan_name,
                        guild.id,
                    )
                    continue
                if guild.me is None or not candidate.permissions_for(guild.me).send_messages:
                    log.debug(
                        "Skipping alerts for %s in guild %s: insufficient permissions for channel %s",
                        clan_name,
                        guild.id,
                        candidate.id,
                    )
                    continue
                target_channel = candidate
            else:
                target_channel = default_channel
                if target_channel is None:
                    log.debug(
                        "Skipping alerts for %s in guild %s: no default channel available",
                        clan_name,
                        guild.id,
                    )
                    continue

            try:
                war = await client.get_clan_war_raw(tag)
            except (coc.errors.PrivateWarLog, coc.errors.NotFound, coc.errors.GatewayError):
                continue  # Skip clans without accessible war data
            except Exception:
                continue  # Fail-safe for unexpected library errors

            for alert in _collect_war_alerts(guild, clan_name, tag, war, alert_role, now):
                await send_channel_message(target_channel, alert)


@war_alert_loop.before_loop
async def _war_alert_loop_ready() -> None:
    """Delay the alert loop until the bot session is ready."""
    log.debug("Waiting for bot readiness before starting alert loop")
    await bot.wait_until_ready()


def ensure_war_alert_loop_running() -> None:
    """Start the alert loop once the bot is ready."""
    log.debug("ensure_war_alert_loop_running called")
    if not war_alert_loop.is_running():
        war_alert_loop.start()


class WarInfoView(discord.ui.View):
    """Interactive view for displaying war information with sharing controls."""

    def __init__(self, clan_name: str, war_info: Dict[str, object], *, timeout: float = 180):
        log.debug("WarInfoView initialised for clan %s", clan_name)
        super().__init__(timeout=timeout)
        self.clan_name = clan_name
        self.war_info = war_info
        self.last_output: Optional[str] = None

    @discord.ui.select(
        placeholder="Select the war details to display",
        min_values=1,
        max_values=min(5, len(WAR_INFO_FIELD_MAP)),
        options=[
            discord.SelectOption(label=label, value=key)
            for key, label in WAR_INFO_FIELD_MAP.items()
        ],
    )
    async def select_fields(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        selector: discord.ui.Select,
    ):
        log.debug("WarInfoView.select_fields triggered")
        await interaction.response.defer(ephemeral=True, thinking=True)
        selections = list(selector.values)
        self.last_output = _build_war_output(self.clan_name, selections, self.war_info)
        await send_text_response(interaction, self.last_output, ephemeral=True)

    @discord.ui.button(label="Broadcast", style=discord.ButtonStyle.green, emoji="📣")
    async def broadcast(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("WarInfoView.broadcast triggered")
        if self.last_output is None:
            await send_text_response(
                interaction,
                "📌 Pick at least one detail from the dropdown first.",
                ephemeral=True,
            )
            return
        await send_text_response(interaction, self.last_output, ephemeral=False)

    @discord.ui.button(label="Private Copy", style=discord.ButtonStyle.blurple, emoji="📝")
    async def private(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("WarInfoView.private triggered")
        if self.last_output is None:
            await send_text_response(
                interaction,
                "📌 Pick at least one detail from the dropdown first.",
                ephemeral=True,
            )
            return
        await send_text_response(interaction, self.last_output, ephemeral=True)


@bot.tree.command(name="clan_war_info_menu", description="Explore war data using a select menu.")
@app_commands.describe(clan_name="Configured clan to inspect.")
async def clan_war_info_menu(interaction: discord.Interaction, clan_name: str):
    """Provide an interactive view of war details using a select menu and share buttons."""
    log_command_call("clan_war_info_menu")
    log.debug("clan_war_info_menu invoked")
    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command is only available inside a Discord server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        war_info = await client.get_clan_war_info(clan_name, interaction.guild.id)
    except GuildNotConfiguredError:
        await send_text_response(
            interaction,
            "⚠️ This server has no clans configured. Ask an admin to run `/set_clan`.",
            ephemeral=True,
        )
        return
    except ClanNotConfiguredError as exc:
        await send_text_response(interaction, str(exc), ephemeral=True)
        return
    except discord.HTTPException as exc:
        await send_text_response(interaction, f"⚠️ Discord error: {exc}", ephemeral=True)
        return
    except Exception as exc:
        await send_text_response(
            interaction, f"⚠️ Unable to fetch war info: {exc}", ephemeral=True
        )
        return

    view = WarInfoView(clan_name, war_info)
    initial_output = _build_war_output(clan_name, [], war_info)
    view.last_output = initial_output
    await send_text_response(interaction, initial_output, ephemeral=True, view=view)


class PlayerInfoView(discord.ui.View):
    """Interactive view for displaying player information with sharing controls."""

    def __init__(self, player_label: str, player_info: Dict[str, Any], *, timeout: float = 180):
        log.debug("PlayerInfoView initialised for player %s", player_label)
        super().__init__(timeout=timeout)
        self.player_label = player_label
        self.player_info = player_info
        self.last_output: Optional[str] = None

    @discord.ui.select(
        placeholder="Select the player details to display",
        min_values=1,
        max_values=min(5, len(PLAYER_INFO_FIELD_MAP)),
        options=[
            discord.SelectOption(label=label, value=key)
            for key, label in PLAYER_INFO_FIELD_MAP.items()
        ],
    )
    async def select_fields(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        selector: discord.ui.Select,
    ):
        log.debug("PlayerInfoView.select_fields triggered")
        await interaction.response.defer(ephemeral=True, thinking=True)
        selections = list(selector.values)
        self.last_output = _build_player_output(self.player_label, selections, self.player_info)
        await send_text_response(interaction, self.last_output, ephemeral=True)

    @discord.ui.button(label="Broadcast", style=discord.ButtonStyle.green, emoji="📣")
    async def broadcast(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("PlayerInfoView.broadcast triggered")
        if self.last_output is None:
            await send_text_response(
                interaction,
                "📌 Pick at least one detail from the dropdown first.",
                ephemeral=True,
            )
            return
        await send_text_response(interaction, self.last_output, ephemeral=False)

    @discord.ui.button(label="Private Copy", style=discord.ButtonStyle.blurple, emoji="📝")
    async def private(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("PlayerInfoView.private triggered")
        if self.last_output is None:
            await send_text_response(
                interaction,
                "📌 Pick at least one detail from the dropdown first.",
                ephemeral=True,
            )
            return
        await send_text_response(interaction, self.last_output, ephemeral=True)

class ReplaceClanTagView(discord.ui.View):
    """Confirmation prompt for replacing an existing clan mapped to the same tag."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        existing_name: str,
        new_name: str,
        tag: str,
        enable_alerts: bool,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.existing_name = existing_name
        self.new_name = new_name
        self.tag = tag
        self.enable_alerts = enable_alerts

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Yes, replace it", style=discord.ButtonStyle.danger, emoji="♻️")
    async def confirm(  # type: ignore[override]
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        log.debug(
            "ReplaceClanTagView confirm: replacing %s with %s for guild %s",
            self.existing_name,
            self.new_name,
            self.guild.id,
        )
        guild_config = _ensure_guild_config(self.guild.id)
        clans = guild_config["clans"]
        preserved_channel: Optional[int] = None
        existing_entry = clans.pop(self.existing_name, None)
        if isinstance(existing_entry, dict):
            preserved_channel = (
                existing_entry.get("alerts", {}).get("channel_id")
                if isinstance(existing_entry.get("alerts", {}), dict)
                else None
            )
        response, followup = _apply_clan_update(
            self.guild,
            self.new_name,
            self.tag,
            self.enable_alerts,
            preserve_channel=preserved_channel,
        )
        notice = f"{response}\n\n`{self.existing_name}` has been removed."
        self._disable()
        await interaction.response.edit_message(content=notice, view=self)
        if followup:
            await interaction.followup.send(followup, ephemeral=True)
        self.stop()

    @discord.ui.button(label="No, keep existing", style=discord.ButtonStyle.secondary, emoji="✋")
    async def cancel(  # type: ignore[override]
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        log.debug(
            "ReplaceClanTagView cancel: keeping %s for guild %s",
            self.existing_name,
            self.guild.id,
        )
        self._disable()
        await interaction.response.edit_message(
            content="ℹ️ No changes were made; existing clan mappings remain intact.", view=self
        )
        self.stop()


class ChannelChoiceSelect(discord.ui.Select):
    """Select component for choosing the destination text channel."""

    def __init__(
        self,
        parent_view: "ChooseWarAlertChannelView",
        channels: List[discord.TextChannel],
        *,
        limited: bool = False,
    ):
        self.parent_view = parent_view
        self.limited = limited
        options = self._build_options(channels)
        placeholder = (
            "Select a text channel (filtered list)"
            if limited
            else "Select a text channel for alerts"
        )
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    @staticmethod
    def _build_options(channels: List[discord.TextChannel]) -> List[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=channel.name[:100],
                description=f"#{channel.name} in {channel.category.name if channel.category else 'No category'}",
                value=str(channel.id),
            )
            for channel in channels
        ]

    def update_options(self, channels: List[discord.TextChannel], *, limited: bool) -> None:
        """Refresh the select options based on a filtered channel list."""
        self.options = self._build_options(channels)
        self.placeholder = (
            "Select a text channel (filtered list)"
            if limited
            else "Select a text channel for alerts"
        )
        self.limited = limited

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel_id = int(self.values[0])
        channel = self.parent_view.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ That channel is no longer available. Please choose another.", ephemeral=True
            )
            return
        await self.parent_view.complete_selection(interaction, channel)


class ChannelFilterModal(discord.ui.Modal):
    """Modal dialog to filter the channel list when more than 25 options are present."""

    def __init__(self, parent_view: "ChooseWarAlertChannelView"):
        super().__init__(title="Filter Channels")
        self.parent_view = parent_view
        self.query = discord.ui.TextInput(
            label="Filter by name",
            placeholder="Enter part of the channel name (case-insensitive)",
            required=False,
            max_length=50,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = self.query.value.strip().lower()
        channels = self.parent_view.current_channel_candidates
        if query:
            channels = [channel for channel in channels if query in channel.name.lower()]
        if not channels:
            await interaction.response.send_message(
                "⚠️ No channels matched that filter. Try a different phrase.", ephemeral=True
            )
            return
        limited = len(channels) > 25
        self.parent_view.update_channel_select_options(channels[:25], limited=limited)
        await interaction.response.edit_message(
            content=self.parent_view.render_status_message(), view=self.parent_view
        )


class ChannelFilterButton(discord.ui.Button):
    """Button that opens a modal to filter the channel list."""

    def __init__(self, parent_view: "ChooseWarAlertChannelView"):
        super().__init__(label="Filter channels", style=discord.ButtonStyle.primary, emoji="🔍")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.send_modal(ChannelFilterModal(self.parent_view))


class CategorySelect(discord.ui.Select):
    """Select component for choosing a channel category."""

    def __init__(
        self,
        parent_view: "ChooseWarAlertChannelView",
        category_options: List[discord.SelectOption],
    ):
        super().__init__(
            placeholder="Select a channel category",
            min_values=1,
            max_values=1,
            options=category_options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        category_value = self.values[0]
        category_id = None if category_value == "none" else int(category_value)
        await self.parent_view.handle_category_selection(interaction, category_id)


class ChooseWarAlertChannelView(discord.ui.View):
    """Interactive flow for selecting the alert destination channel."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        clan_name: str,
        channels_by_category: Dict[Optional[int], List[discord.TextChannel]],
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_name = clan_name
        self.channels_by_category = channels_by_category
        self.selected_category_id: Optional[int] = None
        self.current_channel_candidates: List[discord.TextChannel] = []
        self.channel_select: Optional[ChannelChoiceSelect] = None
        self.filter_button: Optional[ChannelFilterButton] = None
        self.category_selected = False

        category_options = self._build_category_options()
        self.add_item(CategorySelect(self, category_options))

    def _build_category_options(self) -> List[discord.SelectOption]:
        options: List[discord.SelectOption] = []
        for category_id, channels in self.channels_by_category.items():
            if not channels:
                continue
            if category_id is None:
                label = "No Category"
            else:
                category = self.guild.get_channel(category_id)
                if isinstance(category, discord.CategoryChannel):
                    label = category.name[:100]
                else:
                    label = "Unknown Category"
            options.append(
                discord.SelectOption(
                    label=label,
                    description=f"{len(channels)} channel(s)",
                    value="none" if category_id is None else str(category_id),
                )
            )
        if not options:
            options.append(discord.SelectOption(label="No eligible categories", value="none"))
        return options

    def render_status_message(self) -> str:
        if not self.category_selected:
            return (
                "Step 1: choose a category so I can list the channels you and I can both post in."
            )
        category_label = (
            "No Category"
            if self.selected_category_id is None
            else (
                self.guild.get_channel(self.selected_category_id).name
                if isinstance(self.guild.get_channel(self.selected_category_id), discord.CategoryChannel)
                else "Unknown Category"
            )
        )
        return (
            f"Category selected: **{category_label}**.\n"
            "Step 2: pick the alert channel below (use the 🔍 button if you need to filter the list). "
            "Step 3: confirm to save the choice."
        )

    async def handle_category_selection(
        self,
        interaction: discord.Interaction,
        category_id: Optional[int],
    ) -> None:
        self.selected_category_id = category_id
        self.category_selected = True
        self.current_channel_candidates = self.channels_by_category.get(category_id, [])

        # Remove previous widgets if they exist.
        if self.channel_select:
            self.remove_item(self.channel_select)
            self.channel_select = None
        if self.filter_button:
            self.remove_item(self.filter_button)
            self.filter_button = None

        if not self.current_channel_candidates:
            await interaction.response.edit_message(
                content="⚠️ No channels are available in that category. Please choose another.",
                view=self,
            )
            return

        limited = len(self.current_channel_candidates) > 25
        initial_channels = self.current_channel_candidates[:25]
        self.channel_select = ChannelChoiceSelect(self, initial_channels, limited=limited)
        self.add_item(self.channel_select)

        if limited:
            self.filter_button = ChannelFilterButton(self)
            self.add_item(self.filter_button)

        await interaction.response.edit_message(
            content=self.render_status_message(),
            view=self,
        )

    def update_channel_select_options(self, channels: List[discord.TextChannel], *, limited: bool) -> None:
        if self.channel_select is None:
            return
        self.channel_select.update_options(channels, limited=limited)

    async def complete_selection(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        log.debug(
            "choose_war_alert_channel selected %s for clan %s in guild %s",
            channel.id,
            self.clan_name,
            self.guild.id,
        )
        guild_config = _ensure_guild_config(self.guild.id)
        clan_entry = guild_config["clans"].get(self.clan_name)
        if not isinstance(clan_entry, dict):
            await interaction.response.send_message(
                "⚠️ That clan configuration no longer exists. Please re-run the command.",
                ephemeral=True,
            )
            return

        alerts = clan_entry.setdefault("alerts", {"enabled": True, "channel_id": None})
        alerts["channel_id"] = channel.id
        save_server_config()

        for child in self.children:
            child.disabled = True

        message = (
            f"✅ Alerts for `{self.clan_name}` will now post in {channel.mention}.\n"
            "⚠️ If I lose send permissions there, alerts will pause until you choose another channel."
        )
        await interaction.response.edit_message(content=message, view=self)
        self.stop()


class AssignBasesModeView(discord.ui.View):
    """Entry point that lets admins choose between per-player assignments or a general rule."""

    def __init__(
        self,
        *,
        interaction: discord.Interaction,
        clan_name: str,
        home_roster: Dict[int, str],
        max_enemy: int,
        alert_role: Optional[discord.Role],
    ):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.guild = interaction.guild
        self.clan_name = clan_name
        self.home_roster = home_roster
        self.max_enemy = max_enemy
        self.alert_role = alert_role
        self.channel: Optional[discord.TextChannel] = (
            interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        )

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    def intro_message(self) -> str:
        return (
            "Choose how you want to distribute assignments:\n"
            "• **Per Player Assignments** lets you build a list for each base.\n"
            "• **General Rule** posts a free-form instruction (e.g. “Mirror attacks”)."
        )

    @discord.ui.button(label="Per Player Assignments", style=discord.ButtonStyle.primary, emoji="🗂️")
    async def start_per_player(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        """Switch to the per-player assignment workflow."""
        log.debug("AssignBasesModeView -> per player path")
        self._disable()
        per_player_view = PerPlayerAssignmentView(
            parent=self,
            home_roster=self.home_roster,
            max_enemy=self.max_enemy,
            alert_role=self.alert_role,
        )
        await interaction.response.edit_message(
            content=per_player_view.render_message(),
            view=per_player_view,
        )

    @discord.ui.button(label="General Assignment Rule", style=discord.ButtonStyle.secondary, emoji="📝")
    async def start_general_rule(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        """Prompt the admin for a general rule to broadcast."""
        log.debug("AssignBasesModeView -> general rule path")
        modal = GeneralAssignmentModal(parent=self)
        await interaction.response.send_modal(modal)


class PerPlayerAssignmentView(discord.ui.View):
    """Interactive builder for per-player base assignments."""

    def __init__(
        self,
        *,
        parent: AssignBasesModeView,
        home_roster: Dict[int, str],
        max_enemy: int,
        alert_role: Optional[discord.Role],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.parent = parent
        self.guild = parent.guild
        self.clan_name = parent.clan_name
        self.home_roster = home_roster
        self.max_enemy = max_enemy
        self.alert_role = alert_role
        self.assignments: Dict[int, List[int]] = {}
        self.add_item(HomeBaseSelect(self))

    def render_message(self) -> str:
        if not self.assignments:
            details = "No assignments captured yet."
        else:
            lines = []
            for base in sorted(self.assignments.keys()):
                member_name = self.home_roster.get(base, f"Base {base}")
                targets = " and ".join(str(num) for num in self.assignments[base])
                lines.append(f"[{base}] {member_name}: {targets}")
            details = "\n".join(lines)
        return (
            "Per-player mode: pick a home base from the dropdown, enter the target base numbers when prompted, "
            "and repeat until you're ready to broadcast.\n"
            "Once the list looks good, press **Post Assignments**."
            f"\n\nCurrent assignments:\n{details}"
        )

    def update_assignment(self, base: int, targets: List[int]) -> None:
        self.assignments[base] = targets

    def clear_assignments(self) -> None:
        self.assignments.clear()

    def build_broadcast_content(self) -> Optional[str]:
        if not self.assignments:
            return None
        lines: List[str] = []
        for base in sorted(self.assignments.keys()):
            member_name = self.home_roster.get(base, f"Base {base}")
            target_text = " and ".join(str(num) for num in self.assignments[base])
            lines.append(f"[{base}] {member_name}: {target_text}")
        mention = f"{self.alert_role.mention} " if self.alert_role else ""
        return f"{mention}Assignments for `{self.clan_name}`\n" + "\n".join(lines)

    @discord.ui.button(label="Post Assignments", style=discord.ButtonStyle.success, emoji="📣")
    async def post_assignments(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        content = self.build_broadcast_content()
        if content is None:
            await interaction.response.send_message(
                "⚠️ Add at least one assignment before broadcasting.",
                ephemeral=True,
            )
            return

        log.debug(
            "PerPlayerAssignmentView posting assignments for clan %s: %s",
            self.clan_name,
            self.assignments,
        )
        channel = self.parent.channel
        if channel is None or not channel.permissions_for(self.guild.me).send_messages:
            await interaction.response.send_message(
                "⚠️ I don't have permission to post in this channel. Try again after adjusting permissions.",
                ephemeral=True,
            )
            return

        for chunk in _chunk_content(content):
            await channel.send(chunk)
        await interaction.response.edit_message(
            content="✅ Assignments posted to the channel.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Clear selections", style=discord.ButtonStyle.danger, emoji="🧹")
    async def clear_all(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        log.debug("PerPlayerAssignmentView clearing assignments for clan %s", self.clan_name)
        self.clear_assignments()
        await interaction.response.edit_message(
            content=self.render_message(),
            view=self,
        )


class HomeBaseSelect(discord.ui.Select):
    """Select component that lets admins choose the home base to configure."""

    def __init__(self, parent_view: PerPlayerAssignmentView):
        options = [
            discord.SelectOption(
                label=f"{position}. {name}",
                value=str(position),
                description="Select to assign enemy targets.",
            )
            for position, name in sorted(parent_view.home_roster.items())
        ]
        super().__init__(
            placeholder="Pick a home base to assign targets.",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        base = int(self.values[0])
        modal = AssignmentModal(parent_view=self.parent_view, base=base)
        await interaction.response.send_modal(modal)


class AssignmentModal(discord.ui.Modal):
    """Modal that captures up to two enemy base numbers for a selected home base."""

    def __init__(self, parent_view: PerPlayerAssignmentView, base: int):
        super().__init__(title=f"Assign targets for base {base}")
        self.parent_view = parent_view
        self.base = base
        self.targets = discord.ui.TextInput(
            label="Enemy base numbers",
            placeholder="Enter 1 or 2 numbers separated by a comma (e.g. 3,14)",
            required=True,
            max_length=20,
        )
        self.add_item(self.targets)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.targets.value.replace(" ", "")
        parts = [part for part in raw.split(",") if part]
        try:
            numbers = [int(part) for part in parts]
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Please enter whole numbers separated by commas.",
                ephemeral=True,
            )
            return

        if not numbers or len(numbers) > 2:
            await interaction.response.send_message(
                "⚠️ Provide one or two enemy base numbers.",
                ephemeral=True,
            )
            return

        for num in numbers:
            if num < 1 or num > self.parent_view.max_enemy:
                await interaction.response.send_message(
                    f"⚠️ Enemy base {num} is not present in the current war.",
                    ephemeral=True,
                )
                return

        self.parent_view.update_assignment(self.base, numbers)
        log.debug(
            "AssignmentModal stored targets %s for base %s in clan %s",
            numbers,
            self.base,
            self.parent_view.clan_name,
        )
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class GeneralAssignmentModal(discord.ui.Modal):
    """Modal that captures a free-form general assignment message."""

    def __init__(self, parent: AssignBasesModeView):
        super().__init__(title="Broadcast a general assignment")
        self.parent = parent
        self.instructions = discord.ui.TextInput(
            label="What should everyone do?",
            placeholder="Example: Everyone attack your mirror as soon as you are ready.",
            style=discord.TextStyle.paragraph,
            max_length=MAX_MESSAGE_LENGTH - 50,
        )
        self.add_item(self.instructions)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.instructions.value.strip()
        if not text:
            await interaction.response.send_message(
                "⚠️ The message cannot be empty.",
                ephemeral=True,
            )
            return

        channel = self.parent.channel
        if channel is None or not channel.permissions_for(self.parent.guild.me).send_messages:
            await interaction.response.send_message(
                "⚠️ I cannot send messages to this channel. Adjust permissions and try again.",
                ephemeral=True,
            )
            return

        mention = f"{self.parent.alert_role.mention} " if self.parent.alert_role else ""
        content = f"{mention}General assignment for `{self.parent.clan_name}`\n{text}"
        for chunk in _chunk_content(content):
            await channel.send(chunk)

        log.debug(
            "GeneralAssignmentModal broadcast for clan %s: %s",
            self.parent.clan_name,
            text,
        )

        await interaction.response.send_message(
            "✅ General assignment broadcast to the channel.",
            ephemeral=True,
        )
        self.parent._disable()
        try:
            await self.parent.interaction.edit_original_response(
                content="General assignment posted to the channel.",
                view=None,
            )
        except discord.HTTPException:
            log.debug("Unable to update original assign_bases message after general rule broadcast.")
        self.stop()


class RoleAssignmentView(discord.ui.View):
    """Allow users to assign themselves a clan role with visibility controls."""

    def __init__(self, guild: discord.Guild, clan_roles: List[str], *, timeout: float = 120):
        log.debug("RoleAssignmentView initialised with %d options", len(clan_roles))
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_roles = clan_roles
        self.last_message: Optional[str] = None

        options = [
            discord.SelectOption(label=name, value=name, emoji="🏷️") for name in clan_roles
        ]
        self.add_item(RoleSelect(options=options, parent_view=self))

    async def _send_no_selection(self, interaction: discord.Interaction):
        log.debug("RoleAssignmentView._send_no_selection called")
        await send_text_response(
            interaction,
            "📌 Choose a clan role from the dropdown first.",
            ephemeral=True,
        )

    @discord.ui.button(label="Broadcast", style=discord.ButtonStyle.green, emoji="📣")
    async def broadcast(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("RoleAssignmentView.broadcast invoked")
        if self.last_message is None:
            await self._send_no_selection(interaction)
            return
        await send_text_response(interaction, self.last_message, ephemeral=False)

    @discord.ui.button(label="Private Receipt", style=discord.ButtonStyle.blurple, emoji="📥")
    async def private(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        log.debug("RoleAssignmentView.private invoked")
        if self.last_message is None:
            await self._send_no_selection(interaction)
            return
        await send_text_response(interaction, self.last_message, ephemeral=True)


class RoleSelect(discord.ui.Select):
    """Select component responsible for assigning the chosen role."""

    def __init__(self, *, options: List[discord.SelectOption], parent_view: RoleAssignmentView):
        log.debug("RoleSelect initialised")
        super().__init__(
            placeholder="Select your clan role",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        log.debug("RoleSelect.callback invoked")
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self.parent_view.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            member = guild.get_member(interaction.user.id)
        if member is None:
            await send_text_response(
                interaction, "❌ Could not resolve your member object.", ephemeral=True
            )
            return

        role_name = self.values[0]
        role = discord.utils.get(guild.roles, name=role_name)

        created_role = False
        if role is None:
            if guild.me is None or not guild.me.guild_permissions.manage_roles:
                await send_text_response(
                    interaction,
                    f"⚠️ Role `{role_name}` does not exist and I lack permission to create it.",
                    ephemeral=True,
                )
                return
            try:
                role = await guild.create_role(name=role_name, reason="Auto clan role assignment")
                created_role = True
            except discord.Forbidden:
                await send_text_response(
                    interaction,
                    f"⚠️ I could not create the `{role_name}` role due to missing permissions.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as exc:
                await send_text_response(
                    interaction,
                    f"⚠️ Failed to create role: {exc}",
                    ephemeral=True,
                )
                return

        try:
            await member.add_roles(role, reason="Self-selected clan role assignment")
        except discord.Forbidden:
            await send_text_response(
                interaction,
                "⚠️ I cannot assign that role because it is higher than my highest role.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await send_text_response(
                interaction, f"⚠️ Failed to assign role: {exc}", ephemeral=True
            )
            return

        action = "created and assigned" if created_role else "assigned"
        message = f"✅ `{role_name}` has been {action} to {member.mention}."
        self.parent_view.last_message = message
        await send_text_response(interaction, message, ephemeral=True)


@bot.tree.command(name="toggle_war_alerts", description="Opt in or out of war alert pings.")
@app_commands.describe(enable="Choose True to receive alerts or False to opt out")
async def toggle_war_alerts(interaction: discord.Interaction, enable: bool):
    """Toggle the role used for mention-based war alerts."""
    log_command_call("toggle_war_alerts")
    log.debug("toggle_war_alerts invoked (enable=%s)", enable)
    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command is only available inside a Discord server.",
            ephemeral=True,
        )
        return

    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if member is None:
        await send_text_response(
            interaction,
            "❌ Could not resolve your guild membership for this server.",
            ephemeral=True,
        )
        return

    role = discord.utils.get(interaction.guild.roles, name=ALERT_ROLE_NAME)

    if enable:
        if role is None:
            if interaction.guild.me is None or not interaction.guild.me.guild_permissions.manage_roles:
                await send_text_response(
                    interaction,
                    "⚠️ I lack permission to create the war alert role. Please ask an admin to grant Manage Roles or create it manually.",
                    ephemeral=True,
                )
                return
            role = await interaction.guild.create_role(name=ALERT_ROLE_NAME, reason="Opt-in war alert notifications")
        try:
            await member.add_roles(role, reason="User opted into war alerts")
        except discord.Forbidden:
            await send_text_response(
                interaction,
                "⚠️ I cannot assign that role because my role is lower than it.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await send_text_response(
                interaction,
                f"⚠️ Failed to assign the alert role: {exc}.",
                ephemeral=True,
            )
            return
        await send_text_response(
            interaction,
            f"✅ {member.mention} will now receive war alerts.",
            ephemeral=True,
        )
    else:
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="User opted out of war alerts")
            except discord.HTTPException as exc:
                await send_text_response(
                    interaction,
                    f"⚠️ Failed to remove the alert role: {exc}.",
                    ephemeral=True,
                )
                return
            await send_text_response(
                interaction,
                f"✅ {member.mention} will no longer receive war alerts.",
                ephemeral=True,
            )
        else:
            await send_text_response(
                interaction,
                "ℹ️ You were not subscribed to war alerts.",
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# Slash command: /assign_bases
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="assign_bases",
    description="Assign war targets with an interactive menu or broadcast a general rule.",
)
@app_commands.describe(clan_name="Pick the clan that is currently in war.")
async def assign_bases(interaction: discord.Interaction, clan_name: str):
    """Present admins with interactive tools to share base assignments."""
    log_command_call("assign_bases")
    log.debug("assign_bases invoked for clan %s", clan_name)
    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command is only available inside a Discord server.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can assign war targets.",
            ephemeral=True,
        )
        return

    clan_tags = _clan_names_for_guild(interaction.guild.id)
    tag = clan_tags.get(clan_name)
    if not tag:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured for this server.",
            ephemeral=True,
        )
        return

    try:
        war = await client.get_clan_war_raw(tag)
    except coc.errors.PrivateWarLog:
        await send_text_response(
            interaction,
            "⚠️ This clan's war log is private; targets cannot be assigned.",
            ephemeral=True,
        )
        return
    except coc.errors.NotFound:
        await send_text_response(
            interaction,
            "⚠️ No active war found for this clan.",
            ephemeral=True,
        )
        return
    except Exception as exc:
        await send_text_response(
            interaction,
            f"⚠️ Unable to fetch war information: {exc}.",
            ephemeral=True,
        )
        return

    sorted_home = [
        member
        for member in sorted(war.clan.members, key=lambda m: getattr(m, "map_position", 0))
        if getattr(member, "map_position", None) is not None
    ]
    sorted_enemy = [
        member
        for member in sorted(war.opponent.members, key=lambda m: getattr(m, "map_position", 0))
        if getattr(member, "map_position", None) is not None
    ]

    home_roster = {
        member.map_position: getattr(member, "name", f"Base {member.map_position}")
        for member in sorted_home
    }
    max_enemy = len(sorted_enemy)

    alert_role = discord.utils.get(interaction.guild.roles, name=ALERT_ROLE_NAME)

    view = AssignBasesModeView(
        interaction=interaction,
        clan_name=clan_name,
        home_roster=home_roster,
        max_enemy=max_enemy,
        alert_role=alert_role,
    )
    intro = (
        "After submitting the command with the clan name, choose how you want to share assignments:\n"
        "• Use **Per Player Assignments** to build the familiar per-base list without memorising the syntax.\n"
        "• Use **General Assignment Rule** for a quick broadcast such as “everyone attack your mirror.”"
    )
    await send_text_response(interaction, intro, ephemeral=True, view=view)


# ---------------------------------------------------------------------------
# Autocomplete
@bot.tree.command(name="assign_clan_role", description="Self-assign your clan role via select menu.")
async def assign_clan_role(interaction: discord.Interaction):
    """Allow members to pick a clan role matching configured clans."""
    log_command_call("assign_clan_role")
    log.debug("assign_clan_role invoked")
    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command is only available inside a Discord server.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "⚠️ No clans are configured for this server. Ask an admin to run `/set_clan` first.",
            ephemeral=True,
        )
        return

    view = RoleAssignmentView(interaction.guild, list(clan_map.keys()))
    await send_text_response(
        interaction,
        (
            "Use the dropdown below to pick the clan role you want applied to your Discord account. "
            "Once you make a choice, use the buttons to decide whether the confirmation is public or private."
        ),
        ephemeral=True,
        view=view,
    )


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

@clan_war_info_menu.autocomplete("clan_name")
@assign_bases.autocomplete("clan_name")
@choose_war_alert_channel.autocomplete("clan_name")
async def clan_name_autocomplete(interaction: discord.Interaction, current: str):
    """Provide clan name suggestions from the server configuration."""
    if interaction.guild is None:
        return []
    clan_map = _clan_names_for_guild(interaction.guild.id)
    current_lower = current.lower()
    suggestions = [
        app_commands.Choice(name=name, value=name)
        for name in clan_map
        if current_lower in name.lower()
    ]
    return suggestions[:25]


@player_info.autocomplete("player_reference")
async def player_reference_autocomplete(interaction: discord.Interaction, current: str):
    """Provide player name suggestions sourced from the server configuration."""
    if interaction.guild is None:
        return []

    guild = interaction.guild
    guild_config = _ensure_guild_config(guild.id)
    player_tags: Dict[str, str] = guild_config.get("player_tags", {})
    player_accounts: Dict[str, List[Dict[str, Optional[str]]]] = guild_config.get("player_accounts", {})

    current_lower = current.lower()
    suggestions: List[app_commands.Choice[str]] = []
    seen_values: Set[str] = set()

    def add_choice(name: str, value: str) -> None:
        key = value.lower()
        if key in seen_values:
            return
        if current_lower and current_lower not in name.lower() and current_lower not in value.lower():
            return
        suggestions.append(app_commands.Choice(name=name, value=value))
        seen_values.add(key)

    # Linked accounts first.
    for user_id_str, records in player_accounts.items():
        if not isinstance(records, list):
            continue
        member = guild.get_member(int(user_id_str)) if user_id_str.isdigit() else None
        member_label = member.display_name if member else f"User {user_id_str}"
        for record in records:
            if not isinstance(record, dict):
                continue
            tag = record.get("tag")
            alias = record.get("alias")
            normalised_tag = _normalise_player_tag(tag) if isinstance(tag, str) else None
            if normalised_tag is None:
                continue
            label_alias = alias or member_label
            add_choice(f"{label_alias} — {normalised_tag}", label_alias)
            add_choice(normalised_tag, normalised_tag)
            if len(suggestions) >= 25:
                return suggestions[:25]

    # Global saved tags.
    for name, tag in player_tags.items():
        normalised_tag = _normalise_player_tag(tag)
        if normalised_tag is None:
            continue
        add_choice(f"{name} — {normalised_tag}", name)
        add_choice(normalised_tag, normalised_tag)
        if len(suggestions) >= 25:
            break

    return suggestions[:25]
