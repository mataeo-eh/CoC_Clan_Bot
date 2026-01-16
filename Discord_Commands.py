from __future__ import annotations

import copy
import csv
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple
from uuid import uuid4

from collections import OrderedDict

import discord
from discord import app_commands
from discord.ext import tasks

import coc

from bot_core import bot, client
from logger import get_logger, log_command_call, get_usage_summary

log = get_logger()
from COC_API import ClanNotConfiguredError, GuildNotConfiguredError, notinWar
from Clan_Configs import save_server_config, server_config


MAX_MESSAGE_LENGTH = 1900
ALERT_ROLE_NAME = "War Alerts"
# Matches the poll frequency of the background alert loop (5 minutes).
ALERT_WINDOW_SECONDS = 300
README_URL = "https://github.com/mataeo/COC_Clan_Bot/blob/main/README.md"
WAR_NUDGE_REASONS = ("unused_attacks", "no_attacks", "low_stars")
DEFAULT_EVENT_DEFINITIONS: "OrderedDict[str, Dict[str, str]]" = OrderedDict(
    [
        ("clan_games", {"label": "Clan Games", "role_name": "Clan Games Alerts"}),
        ("raid_weekend", {"label": "Raid Weekend", "role_name": "Raid Weekend Alerts"}),
    ]
)
DASHBOARD_MODULES = {
    "war_overview": "War overview",
    "donation_snapshot": "Donation snapshot",
    "upgrade_queue": "Upgrade queue",
    "event_opt_ins": "Event opt-in summary",
}
DASHBOARD_FORMATS = {"embed", "csv", "both"}
REPORT_TYPES = ("dashboard", "donation_summary", "season_summary")
SCHEDULE_FREQUENCIES = ("daily", "weekly")
WEEKDAY_CHOICES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_MAP = {day: index for index, day in enumerate(WEEKDAY_CHOICES)}
MAX_UPGRADE_LOG_ENTRIES = 250
HELP_REMINDER = (
    "Tip: After entering any command’s required options, press enter to run it. "
    "Interactive menus or buttons appear in Discord right afterward."
)

DONATION_METRICS = ("top_donors", "low_donors", "negative_balance")
DONATION_METRIC_INFO: Dict[str, str] = {
    "top_donors": "Highlight the top donating members.",
    "low_donors": "Track members with low donation counts.",
    "negative_balance": "Flag members who received more troops than they donated.",
}

# Cache of alert milestones sent per (guild, clan, war) tuple to avoid duplicates.
alert_state: Dict[Tuple[int, str, str], Set[str]] = {}
_dirty_war_alert_state_guilds: Set[int] = set()
_war_alert_state_loaded = False


def _record_command_usage(interaction: discord.Interaction, command_name: str) -> None:
    """Log a command invocation with anonymised user metadata.

    Parameters:
        interaction (discord.Interaction): The Discord context that exposes the invoking user.
        command_name (str): Canonical name recorded in the telemetry counters.
    """
    user_id = getattr(interaction, "user", None)
    user_identifier = getattr(user_id, "id", None) if user_id is not None else None
    if not isinstance(user_identifier, int):
        user_identifier = None
    log_command_call(command_name, user_id=user_identifier)


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


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO formatted timestamps, tolerating trailing Z for UTC.

    Parameters:
        value (Optional[str]): Timestamp string saved in ISO 8601 form.

    Returns:
        Optional[datetime]: A timezone-aware datetime when parsing succeeds.
    """
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_datetime_utc(value: Optional[datetime]) -> str:
    """Format a datetime for display in UTC.

    Parameters:
        value (Optional[datetime]): Naive or timezone-aware datetime to convert for presentation.
    """
    if value is None:
        return "Never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _build_help_message(title: str, bullet_lines: Iterable[str]) -> str:
    """Create a formatted help blurb for specialised help commands."""
    body = "\n".join(f"• {line}" for line in bullet_lines)
    return f"**{title}**\n{body}\n\n{HELP_REMINDER}"


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


def _load_war_alert_state_from_config() -> None:
    """Hydrate in-memory alert de-duplication state from persisted config."""
    global _war_alert_state_loaded
    if _war_alert_state_loaded:
        return

    for guild_id, guild_config in server_config.items():
        stored = guild_config.get("war_alert_state")
        if not isinstance(stored, dict):
            continue

        for clan_name, wars in stored.items():
            if not isinstance(clan_name, str) or not isinstance(wars, dict):
                continue
            for war_tag, sent_ids in wars.items():
                if not isinstance(war_tag, str) or not isinstance(sent_ids, list):
                    continue
                cleaned = {value for value in sent_ids if isinstance(value, str) and value}
                if cleaned:
                    alert_state[_alert_key(guild_id, clan_name, war_tag)] = cleaned

    _war_alert_state_loaded = True


def _serialise_war_alert_state_for_guild(guild_id: int) -> Dict[str, Dict[str, List[str]]]:
    payload: Dict[str, Dict[str, List[str]]] = {}
    for (key_guild_id, clan_name, war_tag), sent in alert_state.items():
        if key_guild_id != guild_id:
            continue
        payload.setdefault(clan_name, {})[war_tag] = sorted(sent)
    return payload


def _persist_war_alert_state_for_guild(guild_id: int) -> bool:
    """Persist alert de-duplication state for a guild; returns True if updated."""
    guild_config = _ensure_guild_config(guild_id)
    current = guild_config.get("war_alert_state")
    if not isinstance(current, dict):
        current = {}

    payload = _serialise_war_alert_state_for_guild(guild_id)
    if payload == current:
        return False

    guild_config["war_alert_state"] = payload
    return True


def _clear_war_alert_state_for_clan(guild_id: int, clan_name: str) -> None:
    removed = False
    for key in [k for k in alert_state.keys() if k[0] == guild_id and k[1] == clan_name]:
        alert_state.pop(key, None)
        removed = True
    if removed:
        _dirty_war_alert_state_guilds.add(guild_id)


def _prune_war_alert_state_for_clan(guild_id: int, clan_name: str, keep_war_tag: str) -> None:
    removed = False
    for key in [k for k in alert_state.keys() if k[0] == guild_id and k[1] == clan_name and k[2] != keep_war_tag]:
        alert_state.pop(key, None)
        removed = True
    if removed:
        _dirty_war_alert_state_guilds.add(guild_id)


def _mark_alert_sent(guild_id: int, clan_name: str, war_tag: str, alert_id: str) -> bool:
    """Record an alert and return True if it has not been sent before."""
    sent = alert_state.setdefault(_alert_key(guild_id, clan_name, war_tag), set())
    if alert_id in sent:
        return False
    sent.add(alert_id)
    _dirty_war_alert_state_guilds.add(guild_id)
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


def _alias_key_variants(text: str) -> Set[str]:
    """Return a set of normalised lookup keys for player aliases or references."""
    if not isinstance(text, str):
        return set()
    trimmed = text.strip()
    if not trimmed:
        return set()
    lowered = trimmed.casefold()
    keys: Set[str] = set()
    keys.add(lowered)

    # Remove common leading symbols.
    for prefix in ("#", "@", "@!"):
        if lowered.startswith(prefix):
            candidate = lowered[len(prefix) :]
            if candidate:
                keys.add(candidate)

    # Collapse whitespace for relaxed matching.
    compact = lowered.replace(" ", "")
    if compact:
        keys.add(compact)

    # Provide versions with and without a hash prefix for user convenience.
    def _with_hash(value: str) -> Optional[str]:
        return f"#{value}" if value else None

    if not lowered.startswith("#"):
        hashed = _with_hash(lowered)
        if hashed:
            keys.add(hashed)
        if compact:
            hashed_compact = _with_hash(compact)
            if hashed_compact:
                keys.add(hashed_compact)
    else:
        no_hash = lowered.lstrip("#")
        if no_hash:
            keys.add(no_hash)
        compact_no_hash = compact.lstrip("#")
        if compact_no_hash:
            keys.add(compact_no_hash)

    # Handle mention syntax <@123> or <@!123>
    if lowered.startswith("<@") and lowered.endswith(">"):
        mention_inner = lowered[2:-1]
        if mention_inner.startswith("!"):
            mention_inner = mention_inner[1:]
        if mention_inner:
            keys.add(mention_inner)

    # Remove empty strings that may have been introduced.
    return {key for key in keys if key}


def _register_alias(lookup: Dict[str, str], alias: str, tag: str) -> None:
    """Register an alias and its variants to point at the provided tag."""
    for key in _alias_key_variants(alias):
        lookup.setdefault(key, tag)


def _build_player_lookup(guild: discord.Guild) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Create lookup tables for resolving player references to tags."""
    guild_config = _ensure_guild_config(guild.id)
    alias_map: Dict[str, str] = {}
    tag_map: Dict[str, str] = {}

    # Linked accounts stored per Discord member.
    player_accounts = guild_config.get("player_accounts", {})
    for user_id_str, records in player_accounts.items():
        if not isinstance(records, list):
            continue
        member: Optional[discord.Member] = None
        if user_id_str.isdigit():
            member = guild.get_member(int(user_id_str))
        name_candidates = {
            getattr(member, "display_name", None),
            getattr(member, "name", None),
            getattr(member, "global_name", None),
            getattr(member, "nick", None),
        } if member else set()
        name_candidates.discard(None)

        first_tag: Optional[str] = None
        for record in records:
            if not isinstance(record, dict):
                continue
            tag = record.get("tag")
            normalised_tag = _normalise_player_tag(tag) if isinstance(tag, str) else None
            if normalised_tag is None:
                continue
            tag_map.setdefault(normalised_tag, normalised_tag)
            _register_alias(alias_map, normalised_tag, normalised_tag)
            first_tag = first_tag or normalised_tag

            alias_value = record.get("alias")
            if isinstance(alias_value, str) and alias_value.strip():
                _register_alias(alias_map, alias_value, normalised_tag)

            for name in name_candidates:
                if isinstance(name, str) and name.strip():
                    _register_alias(alias_map, name, normalised_tag)
                    _register_alias(alias_map, f"@{name}", normalised_tag)

        if first_tag:
            _register_alias(alias_map, user_id_str, first_tag)
            if member is not None:
                mention_variants = (
                    f"<@{member.id}>",
                    f"<@!{member.id}>",
                    str(member.id),
                )
                for variant in mention_variants:
                    _register_alias(alias_map, variant, first_tag)

    # Legacy global mappings.
    for alias, tag in guild_config.get("player_tags", {}).items():
        normalised_tag = _normalise_player_tag(tag)
        if normalised_tag is None:
            continue
        tag_map.setdefault(normalised_tag, normalised_tag)
        _register_alias(alias_map, alias, normalised_tag)
        _register_alias(alias_map, normalised_tag, normalised_tag)

    return alias_map, tag_map


def _resolve_player_reference(guild: discord.Guild, reference: str) -> Optional[str]:
    """Resolve a user-provided reference (alias, mention, or tag) into a normalised tag."""
    if not isinstance(reference, str):
        return None
    candidate = reference.strip()
    if not candidate:
        return None

    # Direct tag handling first.
    if candidate.startswith("#"):
        direct_tag = _normalise_player_tag(candidate)
        if direct_tag:
            return direct_tag

    alias_map, tag_map = _build_player_lookup(guild)

    # Mentions such as <@123> or <@!123>.
    if candidate.startswith("<@") and candidate.endswith(">"):
        inner = candidate[2:-1].lstrip("!")
        resolved = alias_map.get(inner.casefold())
        if resolved:
            return resolved

    # User typed a bare numeric ID.
    if candidate.isdigit():
        resolved = alias_map.get(candidate.casefold())
        if resolved:
            return resolved

    # Alias or display name variations.
    for key in _alias_key_variants(candidate):
        resolved = alias_map.get(key)
        if resolved:
            return resolved

    # Final fallback: treat as tag without a leading hash.
    fallback_tag = _normalise_player_tag(candidate)
    if fallback_tag and fallback_tag in tag_map:
        return fallback_tag

    return None


def _summarise_linked_accounts(guild: discord.Guild, member_id: int) -> str:
    """Return a human-readable summary of linked accounts for a guild member."""
    guild_config = _ensure_guild_config(guild.id)
    accounts = guild_config.get("player_accounts", {}).get(str(member_id), [])
    summaries: List[str] = []
    for record in accounts:
        if not isinstance(record, dict):
            continue
        tag = _normalise_player_tag(record.get("tag"))
        if tag is None:
            continue
        alias = record.get("alias")
        if isinstance(alias, str) and alias.strip():
            summaries.append(f"{alias.strip()} ({tag})")
        else:
            summaries.append(tag)
    return ", ".join(summaries) if summaries else "None linked yet"


DURATION_COMPONENT_RE = re.compile(
    r"(?P<value>\d+)\s*(?P<unit>d(?:ays?)?|h(?:ours?)?|hr?s?|m(?:in(?:utes?)?)?)",
    re.IGNORECASE,
)
DURATION_COLON_RE = re.compile(r"^\s*(\d+):(\d{2})(?::(\d{2}))?\s*$")


def _parse_upgrade_duration(value: str) -> Optional[timedelta]:
    """Convert a human friendly duration string into a timedelta."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None

    total_seconds = 0
    matched = False
    for match in DURATION_COMPONENT_RE.finditer(cleaned):
        matched = True
        amount = int(match.group("value"))
        unit = match.group("unit").lower()
        if unit in {"d", "day", "days"}:
            total_seconds += amount * 86400
        elif unit in {"h", "hour", "hours", "hr", "hrs"}:
            total_seconds += amount * 3600
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            total_seconds += amount * 60
        else:
            matched = False
            break

    if matched and total_seconds > 0:
        return timedelta(seconds=total_seconds)

    colon_match = DURATION_COLON_RE.match(cleaned)
    if colon_match:
        hours = int(colon_match.group(1))
        minutes = int(colon_match.group(2))
        seconds = int(colon_match.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return timedelta(seconds=total)

    return None


def _format_eta(timestamp: datetime) -> str:
    """Format a completion timestamp for Discord display."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    unix_ts = int(timestamp.timestamp())
    return f"<t:{unix_ts}:R> ({timestamp.strftime('%Y-%m-%d %H:%M UTC')})"


class PlayerLinkError(Exception):
    """Raised when a link or unlink action fails validation."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def _link_player_account(
    *,
    guild: discord.Guild,
    actor: discord.Member,
    target: discord.Member,
    action: Literal["link", "unlink"],
    player_tag: str,
    alias: Optional[str],
) -> str:
    """Perform the underlying link/unlink operation and return a user-facing message."""
    if guild.id != actor.guild.id or guild.id != target.guild.id:
        raise PlayerLinkError("⚠️ Players can only be managed inside their originating server.")

    action_lower = action.lower()
    if action_lower not in {"link", "unlink"}:
        raise PlayerLinkError("⚠️ Unknown action; please choose link or unlink.")

    normalised_tag = _normalise_player_tag(player_tag)
    if normalised_tag is None:
        raise PlayerLinkError("⚠️ Please provide a valid player tag like `#ABC123`.")

    if target != actor and not actor.guild_permissions.administrator:
        raise PlayerLinkError("❌ Only administrators can manage linked tags for other members.")

    guild_config = _ensure_guild_config(guild.id)
    accounts = guild_config.setdefault("player_accounts", {})
    user_key = str(target.id)
    existing_entries = accounts.setdefault(user_key, [])

    if action_lower == "link":
        try:
            player_payload = await client.get_player(normalised_tag)
        except coc.errors.NotFound:
            raise PlayerLinkError(f"⚠️ I couldn't find a Clash of Clans profile with tag `{normalised_tag}`.")
        except Exception as exc:  # pylint: disable=broad-except
            log.exception("Unexpected error while linking player tag", exc_info=exc)
            raise PlayerLinkError(f"⚠️ Unable to verify that tag right now: {exc}") from exc

        inferred_alias = alias.strip() if isinstance(alias, str) and alias.strip() else None
        if inferred_alias is None:
            inferred_alias = player_payload.get("profile", {}).get("name")
        if isinstance(inferred_alias, str):
            inferred_alias = inferred_alias.strip() or None

        updated = False
        for record in existing_entries:
            if isinstance(record, dict) and record.get("tag") == normalised_tag:
                record["alias"] = inferred_alias
                updated = True
                break
        if not updated:
            existing_entries.append({"tag": normalised_tag, "alias": inferred_alias})

        save_server_config()
        alias_note = f" as `{inferred_alias}`" if inferred_alias else ""
        target_label = target.display_name if isinstance(target, discord.Member) else str(target.id)
        return f"✅ Linked `{normalised_tag}`{alias_note} to {target_label}."

    # Unlink branch.
    before = len(existing_entries)
    existing_entries[:] = [
        entry
        for entry in existing_entries
        if not (isinstance(entry, dict) and entry.get("tag") == normalised_tag)
    ]
    if not existing_entries:
        accounts.pop(user_key, None)
    if before == len(existing_entries):
        raise PlayerLinkError(f"⚠️ No link for `{normalised_tag}` was found for that member.")

    save_server_config()
    target_label = target.display_name if isinstance(target, discord.Member) else str(target.id)
    return f"✅ Removed `{normalised_tag}` from {target_label}."


# ---------------------------------------------------------------------------
# Slash command: /set_clan
# ---------------------------------------------------------------------------
@bot.tree.command(name="set_clan", description="Manage the clans configured for this server.")
@app_commands.describe(
    clan_name="Optional clan to load when opening the editor.",
)
async def set_clan(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
) -> None:
    """Launch the interactive clan manager for this server."""
    _record_command_usage(interaction, "set_clan")
    log.debug("set_clan invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "You need the Administrator permission to configure this command.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    selected_clan = clan_name if isinstance(clan_name, str) and clan_name in clan_map else None

    view = SetClanView(
        guild=interaction.guild,
        selected_clan=selected_clan,
        actor=interaction.user,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture set_clan view message: %s", exc)


# ---------------------------------------------------------------------------
# Slash command: /help
# ---------------------------------------------------------------------------
@bot.tree.command(name="help", description="Show a quick primer on using the Clan Bot.")
async def help_command(interaction: discord.Interaction):
    """Provide a concise overview plus a link to the full documentation."""
    _record_command_usage(interaction, "help")
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

# ---------------------------------------------------------------------------
# Slash command: /help_war_info
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_war_info", description="Explain how to use the war info menu.")
async def help_war_info(interaction: discord.Interaction) -> None:
    """Describe the workflow for the interactive war information command."""
    _record_command_usage(interaction, "help_war_info")
    log.debug("help_war_info invoked")
    bullets = [
        "Run `/clan_war_info_menu` and pick a configured clan name.",
        "Use the dropdown to choose which sections (members, status, timers) you want to see.",
        "Press **Broadcast** to share the latest selection with the channel or **Private Copy** to keep it for yourself.",
    ]
    await send_text_response(
        interaction,
        _build_help_message("War Info Helper", bullets),
        ephemeral=True,
    )

# ---------------------------------------------------------------------------
# Slash command: /help_assign_bases
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_assign_bases", description="Explain the target assignment workflow.")
async def help_assign_bases(interaction: discord.Interaction) -> None:
    """Outline how to share assignments with `/assign_bases`."""
    _record_command_usage(interaction, "help_assign_bases")
    log.debug("help_assign_bases invoked")
    bullets = [
        "Call `/assign_bases` and pick the clan you want to coordinate.",
        "Use **Per Player Assignments** to select a home base, enter one or two enemy targets, and repeat as needed.",
        "Choose **Post Assignments** when finished—the bot formats the summary and pings the alert role automatically.",
        "Use **General Assignment Rule** for broad reminders (for example, mirrors-only or cleanup hour).",
    ]
    await send_text_response(
        interaction,
        _build_help_message("Assign Bases Helper", bullets),
        ephemeral=True,
    )

# ---------------------------------------------------------------------------
# Slash command: /help_plan_upgrade
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_plan_upgrade", description="Explain how to log planned upgrades.")
async def help_plan_upgrade(interaction: discord.Interaction) -> None:
    """Explain how members can submit upgrade plans."""
    _record_command_usage(interaction, "help_plan_upgrade")
    log.debug("help_plan_upgrade invoked")
    bullets = [
        "Link each Clash account to your Discord profile with `/link_player`.",
        "Run `/plan_upgrade`, pick your linked account, and use **Enter Upgrade Details** to supply the building, levels, and duration.",
        "Review the draft summary, add optional notes or clan association, then press **Submit Upgrade** to post in the configured channel.",
        "Admins set or change the destination channel with `/set_upgrade_channel`.",
    ]
    await send_text_response(
        interaction,
        _build_help_message("Upgrade Planner Helper", bullets),
        ephemeral=True,
    )

# ---------------------------------------------------------------------------
# Slash command: /help_dashboard
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_dashboard", description="Explain how to configure and post dashboards.")
async def help_dashboard(interaction: discord.Interaction) -> None:
    """Describe the dashboard configuration and posting commands."""
    _record_command_usage(interaction, "help_dashboard")
    log.debug("help_dashboard invoked")
    bullets = [
        "Admins run `/configure_dashboard` to pick modules (war overview, donations, upgrades, event opt-ins) and a default channel.",
        "Anyone can call `/dashboard` for a configured clan; override modules or format with the optional fields when needed.",
        "Select `embed`, `csv`, or `both` to choose between an embed preview and a downloadable CSV snapshot.",
    ]
    await send_text_response(
        interaction,
        _build_help_message("Dashboard Helper", bullets),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /help_schedule_report
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_schedule_report", description="Explain how to automate recurring reports.")
async def help_schedule_report(interaction: discord.Interaction) -> None:
    """Summarise the scheduled report command family."""
    _record_command_usage(interaction, "help_schedule_report")
    log.debug("help_schedule_report invoked")
    bullets = [
        "Run `/schedule_report` to open the interactive editor, pick the clan, report type, cadence, and time, then press **Save**.",
        "Use the on-screen buttons to adjust dashboard modules/format or toggle season summary sections as needed.",
        "Run `/list_schedules` to review upcoming jobs and `/cancel_schedule` with an ID to remove an entry.",
        "The scheduler posts automatically as soon as the next run time arrives.",
    ]
    await send_text_response(
        interaction,
        _build_help_message("Scheduled Reports Helper", bullets),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /help_usage
# ---------------------------------------------------------------------------
@bot.tree.command(name="help_usage", description="Show aggregate command usage analytics (admin only).")
async def help_usage(interaction: discord.Interaction):
    """Display anonymised command analytics for administrators.

    Parameters:
        interaction (discord.Interaction): Invocation context; must originate from a server administrator.
    """
    _record_command_usage(interaction, "help_usage")
    log.debug("help_usage invoked")

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can view usage analytics.",
            ephemeral=True,
        )
        return

    summary = get_usage_summary()
    lines = [
        "📊 **Command Usage Overview**",
        f"Total invocations logged: {summary.get('total_invocations', 0)}",
        f"Approximate unique users: {summary.get('unique_users', 0)}",
        f"Average commands per user: {summary.get('average_per_user', 0.0):.2f}",
        "",
        "Top commands:",
    ]

    top_commands = summary.get("top_commands", [])
    if top_commands:
        for index, entry in enumerate(top_commands, start=1):
            lines.append(
                f"{index}. {entry.get('name')} — {entry.get('count', 0)} call(s) "
                f"(last used { _format_datetime_utc(entry.get('last_invoked')) })"
            )
    else:
        lines.append("No commands have been recorded yet.")

    top_counts = summary.get("top_user_counts", [])
    lines.extend(
        [
            "",
            "Top anonymous user activity:",
        ]
    )
    if top_counts:
        for index, count in enumerate(top_counts, start=1):
            lines.append(f"User #{index}: {count} call(s)")
    else:
        lines.append("No user activity recorded yet.")

    await send_text_response(
        interaction,
        "\n".join(lines),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /choose_war_alert_channel
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="choose_war_alert_channel",
    description="Select the text channel where war alerts will be posted for a clan.",
)
@app_commands.describe(clan_name="Choose a configured clan to update.")
async def choose_war_alert_channel(interaction: discord.Interaction, clan_name: str):
    """Allow administrators to pick the destination channel for war alerts."""
    _record_command_usage(interaction, "choose_war_alert_channel")
    log.debug("choose_war_alert_channel invoked for %s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException as exc:
            log.warning("Failed to defer interaction for choose_war_alert_channel: %s", exc)

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


# ---------------------------------------------------------------------------
# Slash command: /configure_war_nudge
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="configure_war_nudge",
    description="Add, remove, or list war nudge reasons for a clan.",
)
@app_commands.describe(
    clan_name="Optional clan to preselect for configuration.",
)
async def configure_war_nudge(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
):
    """Maintain the list of war nudge reasons stored per clan."""
    _record_command_usage(interaction, "configure_war_nudge")
    log.debug("configure_war_nudge invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "⚠️ Only administrators can configure war nudges.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "⚠️ No clans are configured yet. Use `/set_clan` before managing war nudges.",
            ephemeral=True,
        )
        return

    default_clan = clan_name if clan_name in clan_map else next(iter(clan_map))
    view = WarNudgeConfigView(interaction.guild, default_clan)

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Unable to capture configure_war_nudge message handle: %s", exc)


# ---------------------------------------------------------------------------
# Slash command: /war_nudge
# ---------------------------------------------------------------------------
@bot.tree.command(name="war_nudge", description="Send a targeted reminder to war participants.")
@app_commands.describe(
    clan_name="Configured clan currently in war.",
    reason_name="Which configured reason to evaluate.",
)
async def war_nudge(interaction: discord.Interaction, clan_name: str, reason_name: str):
    """Evaluate the configured reason and post a nudge for matching members."""
    _record_command_usage(interaction, "war_nudge")
    log.debug("war_nudge invoked for clan=%s reason=%s", clan_name, reason_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured for this server.",
            ephemeral=True,
        )
        return

    reasons = clan_entry.get("war_nudge", {}).get("reasons", [])
    selected_reason = None
    for reason in reasons:
        if reason.get("name", "").lower() == reason_name.lower():
            selected_reason = reason
            break

    if selected_reason is None:
        await send_text_response(
            interaction,
            (
                f"⚠️ I couldn't find a war nudge reason named `{reason_name}`. "
                "Use `/configure_war_nudge` to list available reasons."
            ),
            ephemeral=True,
        )
        return

    clan_tags = _clan_names_for_guild(interaction.guild.id)
    tag = clan_tags.get(clan_name)
    if not tag:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` has no stored clan tag.",
            ephemeral=True,
        )
        return

    try:
        war = await client.get_clan_war_raw(tag)
    except coc.errors.PrivateWarLog:
        await send_text_response(
            interaction,
            "⚠️ This clan's war log is private; I can't evaluate current war data.",
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

    reason_type = selected_reason.get("type")
    if reason_type not in WAR_NUDGE_REASONS:
        await send_text_response(
            interaction,
            "⚠️ This reason was saved with an unsupported type. Please reconfigure it.",
            ephemeral=True,
        )
        return

    targets = _collect_war_nudge_targets(war, reason_type)
    if not targets:
        await send_text_response(
            interaction,
            "✅ Everyone is on track—no nudge required for that reason.",
            ephemeral=True,
        )
        return

    lines = []
    for member, info in targets:
        tag = getattr(member, "tag", None)
        discord_member = _lookup_member_by_tag(interaction.guild, tag) if tag else None
        name = getattr(member, "name", "Unknown")
        display = discord_member.mention if discord_member else name
        if reason_type == "unused_attacks":
            lines.append(
                f"• {display} — {info.get('remaining', '?')} attack(s) remaining."
            )
        elif reason_type == "no_attacks":
            lines.append(
                f"• {display} — has not attacked yet."
            )
        elif reason_type == "low_stars":
            lines.append(
                f"• {display} — best attack {info.get('best_stars', 0)}⭐ ({info.get('used', 0)} attempt(s))."
            )

    mention_prefix = _build_reason_mention(interaction.guild, selected_reason)
    description = selected_reason.get("description") or ""
    header_parts = [
        part for part in [mention_prefix, f"Nudge for `{clan_name}` — {selected_reason.get('name', 'Unnamed')}"] if part
    ]
    if description:
        header_parts.append(description)
    content = "\n".join(header_parts + [""] + lines)

    await send_text_response(
        interaction,
        content,
        ephemeral=False,
    )


# ---------------------------------------------------------------------------
# Slash command: /configure_dashboard
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="configure_dashboard",
    description="Interactively configure the dashboard modules for a clan.",
)
@app_commands.describe(
    clan_name="Configured clan to update.",
    channel="Optional default channel for dashboard posts.",
)
async def configure_dashboard(
    interaction: discord.Interaction,
    clan_name: str,
    channel: Optional[discord.TextChannel] = None,
):
    """Provide an interactive selector for dashboard modules and format."""
    _record_command_usage(interaction, "configure_dashboard")
    log.debug("configure_dashboard invoked clan=%s channel=%s", clan_name, getattr(channel, "id", None))

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can configure dashboards.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured.",
            ephemeral=True,
        )
        return

    modules, fmt, default_channel_id = _dashboard_defaults(clan_entry)
    default_channel = channel
    if default_channel is None and isinstance(default_channel_id, int):
        default_channel = interaction.guild.get_channel(default_channel_id)

    view = DashboardConfigView(
        guild=interaction.guild,
        clan_name=clan_name,
        initial_modules=modules,
        initial_format=fmt,
        channel=default_channel,
    )
    await send_text_response(
        interaction,
        view.render_message(),
        ephemeral=True,
        view=view,
    )


# ---------------------------------------------------------------------------
# Slash command: /dashboard
# ---------------------------------------------------------------------------
@bot.tree.command(name="dashboard", description="Display the configured dashboard for a clan.")
@app_commands.describe(
    clan_name="Optional clan to preselect when opening the dashboard UI.",
)
async def dashboard(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
):
    """Render dashboard content using an interactive UI."""
    _record_command_usage(interaction, "dashboard")
    log.debug("dashboard invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "No clans are configured yet. Use `/set_clan` before running the dashboard.",
            ephemeral=True,
        )
        return

    selected_clan = clan_name if isinstance(clan_name, str) and clan_name in clan_map else next(iter(clan_map))
    clan_entry = _get_clan_entry(interaction.guild.id, selected_clan)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"{selected_clan} is not configured.",
            ephemeral=True,
        )
        return

    modules, fmt, default_channel_id = _dashboard_defaults(clan_entry)
    default_channel = None
    if isinstance(default_channel_id, int):
        candidate = interaction.guild.get_channel(default_channel_id)
        if isinstance(candidate, discord.TextChannel):
            default_channel = candidate

    fallback_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    initial_channel = default_channel or fallback_channel

    view = DashboardRunView(
        guild=interaction.guild,
        clan_map=clan_map,
        selected_clan=selected_clan,
        initial_modules=modules,
        initial_format=fmt,
        initial_channel=initial_channel,
        fallback_channel=fallback_channel,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture dashboard view message: %s", exc)


# ---------------------------------------------------------------------------
# Slash command: /link_player
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="link_player",
    description="Link or unlink Clash of Clans player tags to Discord members.",
)
@app_commands.describe(
    action="Optional action to preselect (link or unlink).",
    player_tag="Optional player tag (e.g. #ABC123) to pre-fill when opening the view.",
    alias="Optional nickname to pre-fill (defaults to the in-game name when linking).",
    target_member="Only admins may manage tags for someone else.",
)
async def link_player(
    interaction: discord.Interaction,
    action: Optional[Literal["link", "unlink"]] = None,
    player_tag: Optional[str] = None,
    alias: Optional[str] = None,
    target_member: Optional[discord.Member] = None,
) -> None:
    """Launch an interactive view for linking or unlinking Clash of Clans accounts."""
    _record_command_usage(interaction, "link_player")
    log.debug(
        "link_player invoked action=%s tag=%s target=%s",
        action,
        player_tag,
        target_member.id if isinstance(target_member, discord.Member) else None,
    )

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "⚠️ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    actor = (
        interaction.user
        if isinstance(interaction.user, discord.Member)
        else interaction.guild.get_member(interaction.user.id)
    )
    if actor is None:
        await send_text_response(
            interaction,
            "I could not resolve your guild membership. Please try again.",
            ephemeral=True,
        )
        return

    initial_action = action if action in {"link", "unlink"} else "link"
    cleaned_tag = player_tag.strip() if isinstance(player_tag, str) else ""
    cleaned_alias = alias.strip() if isinstance(alias, str) and alias.strip() else None

    target: discord.Member = actor
    if isinstance(target_member, discord.Member) and target_member.id != actor.id:
        if actor.guild_permissions.administrator:
            target = target_member
        else:
            await send_text_response(
                interaction,
                "Only administrators can manage linked tags for other members.",
                ephemeral=True,
            )
            return

    view = LinkPlayerView(
        guild=interaction.guild,
        actor=actor,
        selected_action=initial_action,
        initial_tag=cleaned_tag,
        initial_alias=cleaned_alias,
        initial_target=target,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture link_player view message: %s", exc)



# ---------------------------------------------------------------------------
# Slash command: /save_war_plan
# ---------------------------------------------------------------------------
@bot.tree.command(name="save_war_plan", description="Save or update a war plan template for a clan.")
@app_commands.describe(
    clan_name="Optional clan to preselect when opening the editor.",
    plan_name="Optional plan to preselect when the editor opens.",
)
async def save_war_plan(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
    plan_name: Optional[str] = None,
):
    """Launch an interactive editor for creating or updating war plans."""
    _record_command_usage(interaction, "save_war_plan")
    log.debug("save_war_plan invoked clan=%s plan=%s", clan_name, plan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "Only administrators can save war plans.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "No clans are configured yet. Use `/set_clan` before saving war plans.",
            ephemeral=True,
        )
        return

    selected_clan = None
    if isinstance(clan_name, str) and clan_name in clan_map:
        selected_clan = clan_name
    else:
        selected_clan = next(iter(clan_map))

    if isinstance(plan_name, str):
        plan_name = plan_name.strip() or None

    view = WarPlanView(
        guild=interaction.guild,
        clan_map=clan_map,
        selected_clan=selected_clan,
        preselected_plan=plan_name,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture save_war_plan view message: %s", exc)


# ---------------------------------------------------------------------------
# Slash command: /list_war_plans
# ---------------------------------------------------------------------------
@bot.tree.command(name="list_war_plans", description="List saved war plan templates for a clan.")
@app_commands.describe(clan_name="Configured clan to inspect.")
async def list_war_plans(interaction: discord.Interaction, clan_name: str):
    """Return the stored plan names for quick reference."""
    _record_command_usage(interaction, "list_war_plans")
    log.debug("list_war_plans invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured.",
            ephemeral=True,
        )
        return

    war_plans = clan_entry.get("war_plans", {})
    if not war_plans:
        await send_text_response(
            interaction,
            f"ℹ️ No war plans are stored for `{clan_name}`.",
            ephemeral=True,
        )
        return

    lines = [
        f"• **{name}** (last updated {plan.get('updated_at', 'unknown')})"
        for name, plan in war_plans.items()
    ]
    await send_text_response(
        interaction,
        f"War plans for `{clan_name}`:\n" + "\n".join(lines),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /war_plan
# ---------------------------------------------------------------------------
@bot.tree.command(name="war_plan", description="Post a saved war plan template.")
@app_commands.describe(
    clan_name="Optional clan to preselect when opening the poster.",
    plan_name="Optional plan to preselect in the poster.",
    target_channel="Optional channel to preselect (defaults to the current channel).",
)
async def war_plan(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
    plan_name: Optional[str] = None,
    target_channel: Optional[discord.TextChannel] = None,
) -> None:
    """Launch an interactive flow for posting a stored war plan."""
    _record_command_usage(interaction, "war_plan")
    log.debug("war_plan invoked clan=%s plan=%s", clan_name, plan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if isinstance(plan_name, str):
        plan_name = plan_name.strip() or None

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "No clans are configured yet. Use `/set_clan` before posting war plans.",
            ephemeral=True,
        )
        return

    if isinstance(clan_name, str) and clan_name in clan_map:
        selected_clan = clan_name
    else:
        selected_clan = next(iter(clan_map))

    explicit_channel = target_channel if isinstance(target_channel, discord.TextChannel) else None
    fallback_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

    view = WarPlanPostView(
        guild=interaction.guild,
        clan_map=clan_map,
        selected_clan=selected_clan,
        preselected_plan=plan_name,
        explicit_channel=explicit_channel,
        fallback_channel=fallback_channel,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture war_plan view message: %s", exc)
# Slash command: /player_info
# ---------------------------------------------------------------------------
@bot.tree.command(name="player_info", description="Display detailed information about a Clash of Clans player.")
@app_commands.describe(
    player_reference="Enter a player tag (e.g. #ABC123) or select a saved player name."
)
async def player_info(interaction: discord.Interaction, player_reference: str):
    """Provide an interactive view of player data with share controls."""
    _record_command_usage(interaction, "player_info")
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
    resolved_tag = _resolve_player_reference(guild, reference)
    if resolved_tag is None:
        resolved_tag = _normalise_player_tag(reference)

    if resolved_tag is None:
        await send_text_response(
            interaction,
            (
                f"⚠️ I could not find a saved player named `{reference}`.\n"
                "Provide a full player tag like `#ABC123` or link the account with `/link_player` first."
            ),
            ephemeral=True,
        )
        return

    log.debug("player_info resolved %s -> %s", reference, resolved_tag)

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        player_info = await client.get_player(resolved_tag)
    except coc.errors.NotFound:
        await interaction.followup.send(f"⚠️ I could not find a player with tag `{resolved_tag}`.", ephemeral=True)
        return
    except coc.errors.GatewayError as exc:
        await interaction.followup.send(
            f"⚠️ Clash of Clans API error while fetching `{resolved_tag}`: {exc}", ephemeral=True
        )
        return
    except Exception as exc:
        log.exception("Unexpected error retrieving player data")
        await interaction.followup.send(f"⚠️ Unable to fetch player info: {exc}", ephemeral=True)
        return

    profile = player_info.get("profile", {})
    player_name = profile.get("name") or "Unknown Player"
    header = f"{player_name} ({resolved_tag})"

    view = PlayerInfoView(header, player_info)
    initial_output = _build_player_output(header, [], player_info)
    view.last_output = initial_output
    await interaction.followup.send(initial_output, ephemeral=True, view=view)


# ---------------------------------------------------------------------------
# Slash command: /plan_upgrade
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="plan_upgrade",
    description="Submit a planned upgrade for your linked account.",
)
@app_commands.describe(
    clan_name="Optional clan to preselect when planning the upgrade.",
)
async def plan_upgrade(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
):
    """Launch the interactive planner used to record upgrade details."""
    _record_command_usage(interaction, "plan_upgrade")
    log.debug("plan_upgrade invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    member = (
        interaction.user
        if isinstance(interaction.user, discord.Member)
        else interaction.guild.get_member(interaction.user.id)
    )
    if member is None:
        await send_text_response(
            interaction,
            "I couldn't resolve your member account for this server.",
            ephemeral=True,
        )
        return

    guild_config = _ensure_guild_config(interaction.guild.id)
    raw_accounts = guild_config.get("player_accounts", {}).get(str(member.id), [])
    linked_accounts: List[Dict[str, Optional[str]]] = []
    for record in raw_accounts:
        if not isinstance(record, dict):
            continue
        tag = _normalise_player_tag(record.get("tag"))
        if tag is None:
            continue
        alias_value = record.get("alias")
        linked_accounts.append(
            {
                "tag": tag,
                "alias": alias_value.strip() if isinstance(alias_value, str) and alias_value.strip() else None,
            }
        )

    if not linked_accounts:
        await send_text_response(
            interaction,
            "Link at least one Clash of Clans account with `/link_player` before planning an upgrade.",
            ephemeral=True,
        )
        return

    configured_clans = _clan_names_for_guild(interaction.guild.id)

    channel_id = guild_config.get("channels", {}).get("upgrade")
    destination = interaction.guild.get_channel(channel_id) if isinstance(channel_id, int) else None
    if destination is None:
        await send_text_response(
            interaction,
            "No upgrade channel is configured yet. Ask an administrator to run `/set_upgrade_channel`.",
            ephemeral=True,
        )
        return
    if not destination.permissions_for(destination.guild.me).send_messages:
        await send_text_response(
            interaction,
            "I don't have permission to post in the configured upgrade channel.",
            ephemeral=True,
        )
        return

    preselected_clan = None
    if isinstance(clan_name, str) and clan_name.strip():
        candidate = clan_name.strip()
        if candidate not in configured_clans:
            await send_text_response(
                interaction,
                f"`{candidate}` is not a configured clan for this server.",
                ephemeral=True,
            )
            return
        preselected_clan = candidate
    elif configured_clans:
        preselected_clan = next(iter(configured_clans))

    view = PlanUpgradeView(
        guild=interaction.guild,
        member=member,
        accounts=linked_accounts,
        destination_channel=destination,
        clan_map=configured_clans,
        selected_clan=preselected_clan,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture plan_upgrade view message: %s", exc)

# ---------------------------------------------------------------------------
# Slash command: /set_upgrade_channel
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="set_upgrade_channel",
    description="Choose the channel where planned upgrades will be posted.",
)
@app_commands.describe(channel="Channel where upgrade notices should be sent.")
async def set_upgrade_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Store the guild-wide upgrade channel in the config."""
    _record_command_usage(interaction, "set_upgrade_channel")
    log.debug("set_upgrade_channel invoked channel=%s", channel.id)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can set the upgrade channel.",
            ephemeral=True,
        )
        return
    if not channel.permissions_for(channel.guild.me).send_messages:
        await send_text_response(
            interaction,
            "⚠️ I do not have permission to send messages in that channel.",
            ephemeral=True,
        )
        return

    guild_config = _ensure_guild_config(interaction.guild.id)
    guild_config.setdefault("channels", {})["upgrade"] = channel.id
    save_server_config()
    await send_text_response(
        interaction,
        f"✅ Upgrade notices will now be posted in {channel.mention}.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /configure_donation_metrics
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="configure_donation_metrics",
    description="Adjust which donation metrics are highlighted for a clan.",
)
@app_commands.describe(
    clan_name="Optional clan to preselect for configuration.",
)
async def configure_donation_metrics(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
):
    """Update donation-tracking preferences for a clan."""
    _record_command_usage(interaction, "configure_donation_metrics")
    log.debug("configure_donation_metrics invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "⚠️ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "⚠️ Only administrators can configure donation metrics.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "⚠️ No clans are configured yet. Use `/set_clan` before managing donation metrics.",
            ephemeral=True,
        )
        return

    default_clan = clan_name if clan_name in clan_map else next(iter(clan_map))
    view = DonationConfigView(interaction.guild, default_clan)

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Unable to capture configure_donation_metrics message handle: %s", exc)
# ---------------------------------------------------------------------------
# Slash command: /set_donation_channel
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="set_donation_channel",
    description="Choose the channel where donation summaries will be posted.",
)
@app_commands.describe(
    clan_name="Configured clan to update.",
    channel="Channel that should receive donation summaries.",
)
async def set_donation_channel(
    interaction: discord.Interaction,
    clan_name: str,
    channel: discord.TextChannel,
):
    """Store the donation summary channel for a clan."""
    _record_command_usage(interaction, "set_donation_channel")
    log.debug("set_donation_channel invoked clan=%s channel=%s", clan_name, channel.id)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can set the donation channel.",
            ephemeral=True,
        )
        return
    if not channel.permissions_for(channel.guild.me).send_messages:
        await send_text_response(
            interaction,
            "⚠️ I don't have permission to post in that channel.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured.",
            ephemeral=True,
        )
        return

    clan_entry.setdefault("donation_tracking", {})["channel_id"] = channel.id
    save_server_config()
    await send_text_response(
        interaction,
        f"✅ Donation summaries for `{clan_name}` will post in {channel.mention}.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /donation_summary
# ---------------------------------------------------------------------------
@bot.tree.command(name="donation_summary", description="Generate a donation leaderboard for a clan.")
@app_commands.describe(
    clan_name="Configured clan to analyse.",
    target_channel="Optional channel to post the summary in.",
)
async def donation_summary(
    interaction: discord.Interaction,
    clan_name: str,
    target_channel: Optional[discord.TextChannel] = None,
):
    """Pull donation stats using the configured metrics and broadcast the summary."""
    _record_command_usage(interaction, "donation_summary")
    log.debug("donation_summary invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        payload, default_channel_id, context = await _compose_donation_summary(
            interaction.guild,
            clan_name,
            clan_entry,
        )
    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        return

    destination = target_channel
    if destination is None:
        destination = (
            interaction.guild.get_channel(default_channel_id)
            if isinstance(default_channel_id, int)
            else None
        )
    if destination is None:
        destination = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

    if destination is None:
        await interaction.followup.send(
            "⚠️ I couldn't find a suitable channel to post the summary.",
            ephemeral=True,
        )
        return
    if not destination.permissions_for(destination.guild.me).send_messages:
        await interaction.followup.send(
            "⚠️ I don't have permission to post in the selected channel.",
            ephemeral=True,
        )
        return

    for chunk in _chunk_content(payload):
        await destination.send(chunk)
    csv_payload = _create_csv_file(context.get("csv_sections", []))
    if csv_payload:
        await destination.send(file=discord.File(BytesIO(csv_payload), filename="donation_summary.csv"))

    await interaction.followup.send(
        f"✅ Donation summary posted to {destination.mention}.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /configure_event_role
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="configure_event_role",
    description="Interactively manage event alert roles for this server.",
)
@app_commands.describe(
    event_key="Optional event to preselect when the view opens.",
)
async def configure_event_role(
    interaction: discord.Interaction,
    event_key: Optional[str] = None,
):
    '''Allow administrators to manage event opt-in roles via an interactive UI.'''
    _record_command_usage(interaction, 'configure_event_role')
    log.debug('configure_event_role invoked event_key=%s', event_key)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            'This command must be used inside a Discord server.',
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            'Only administrators can configure event roles.',
            ephemeral=True,
        )
        return

    events = _get_event_roles_for_guild(interaction.guild.id)
    if not events:
        await send_text_response(
            interaction,
            'Event role settings are unavailable. Try again later.',
            ephemeral=True,
        )
        return

    selected_key = event_key if isinstance(event_key, str) and event_key in events else next(iter(events))
    view = EventRoleConfigView(
        guild=interaction.guild,
        events=events,
        selected_key=selected_key,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning('Unable to capture configure_event_role message handle: %s', exc)


# ---------------------------------------------------------------------------
# Slash command: /event_alert_opt
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="event_alert_opt",
    description="Opt yourself (or, if an admin, another member) into event alerts.",
)
@app_commands.describe(
    event_type="Which event alert to toggle.",
    enable="True to add the role, False to remove it.",
    target_member="Admins can toggle the role for another member.",
)
async def event_alert_opt(
    interaction: discord.Interaction,
    event_type: str,
    enable: bool,
    target_member: Optional[discord.Member] = None,
):
    """Toggle event opt-in roles."""
    _record_command_usage(interaction, "event_alert_opt")
    log.debug("event_alert_opt invoked event=%s enable=%s target=%s", event_type, enable, getattr(target_member, "id", None))

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    actor = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    if actor is None:
        await send_text_response(
            interaction,
            "I couldn't resolve your member account.",
            ephemeral=True,
        )
        return

    events = _get_event_roles_for_guild(interaction.guild.id)
    resolved_key = None
    resolved_entry: Optional[Dict[str, Any]] = None
    if events:
        resolved_key, resolved_entry = _resolve_event_selection(interaction.guild, event_type)
    if resolved_key is None or resolved_entry is None:
        available = ", ".join(entry.get("label", key) for key, entry in events.items())
        details = f" Available events: {available}." if available else ""
        await send_text_response(
            interaction,
            f"I couldn't find an event named `{event_type}`.{details}",
            ephemeral=True,
        )
        return

    target = target_member or actor
    if target != actor and not actor.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "Only administrators can toggle event roles for other members.",
            ephemeral=True,
        )
        return

    role = _get_event_role(interaction.guild, resolved_key)
    label = resolved_entry.get("label", resolved_key.replace("_", " " ).title())
    if role is None:
        await send_text_response(
            interaction,
            f"{label} does not have a role configured yet. Ask an administrator to run `/configure_event_role` first.",
            ephemeral=True,
        )
        return

    try:
        if enable:
            await target.add_roles(role, reason="Event alert opt-in")
        else:
            await target.remove_roles(role, reason="Event alert opt-out")
    except discord.Forbidden:
        await send_text_response(
            interaction,
            "You don't have permission to modify that role for the target member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await send_text_response(
            interaction,
            f"Failed to update roles: {exc}",
            ephemeral=True,
        )
        return

    action = "now receiving" if enable else "no longer receiving"
    await send_text_response(
        interaction,
        f"{target.mention} is {action} {label} alerts.",
        ephemeral=True,
    )



@event_alert_opt.autocomplete("event_type")
async def event_alert_opt_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Provide dynamic autocomplete entries for configured events."""
    if interaction.guild is None:
        return []
    events = _get_event_roles_for_guild(interaction.guild.id)
    normalized = current.strip().casefold() if current else ""
    choices: List[app_commands.Choice[str]] = []
    for key, entry in events.items():
        label = entry.get("label", _default_event_label(key))
        if normalized and normalized not in label.casefold() and normalized not in key.casefold():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=key))
        if len(choices) >= 25:
            break
    return choices

# ---------------------------------------------------------------------------
# Slash command: /register_me
# ---------------------------------------------------------------------------
@bot.tree.command(name="register_me", description="Guided onboarding for new clan members.")
async def register_me(interaction: discord.Interaction):
    """Provide buttons and guidance to help new members get set up quickly."""
    _record_command_usage(interaction, "register_me")
    log.debug("register_me invoked")

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    war_alert_role = discord.utils.get(interaction.guild.roles, name=ALERT_ROLE_NAME)
    event_roles: List[Dict[str, Any]] = []
    for key, entry in _get_event_roles_for_guild(interaction.guild.id).items():
        label = entry.get("label", _default_event_label(key))
        role = _get_event_role(interaction.guild, key)
        event_roles.append(
            {
                "key": key,
                "label": label,
                "role": role,
            }
        )

    view = RegisterMeView(
        member=interaction.user,
        war_alert_role=war_alert_role,
        event_roles=event_roles,
    )

    await interaction.response.send_message(
        view.build_intro_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture register_me message handle: %s", exc)



# ---------------------------------------------------------------------------
# Slash command: /set_season_summary_channel
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="set_season_summary_channel",
    description="Choose the channel used for end-of-season summaries.",
)
@app_commands.describe(
    clan_name="Configured clan to update.",
    channel="Channel to receive the summary output.",
)
async def set_season_summary_channel(
    interaction: discord.Interaction,
    clan_name: str,
    channel: discord.TextChannel,
):
    """Store the destination channel for seasonal summaries."""
    _record_command_usage(interaction, "set_season_summary_channel")
    log.debug("set_season_summary_channel invoked clan=%s channel=%s", clan_name, channel.id)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can set the summary channel.",
            ephemeral=True,
        )
        return
    if not channel.permissions_for(channel.guild.me).send_messages:
        await send_text_response(
            interaction,
            "⚠️ I do not have permission to post in that channel.",
            ephemeral=True,
        )
        return

    clan_entry = _get_clan_entry(interaction.guild.id, clan_name)
    if clan_entry is None:
        await send_text_response(
            interaction,
            f"⚠️ `{clan_name}` is not configured.",
            ephemeral=True,
        )
        return

    clan_entry.setdefault("season_summary", {})["channel_id"] = channel.id
    save_server_config()
    await send_text_response(
        interaction,
        f"✅ Seasonal summaries for `{clan_name}` will post in {channel.mention}.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /season_summary
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="season_summary",
    description="Generate an end-of-season summary for a clan.",
)
@app_commands.describe(
    clan_name="Optional clan to preselect when opening the composer.",
    channel="Optional channel to preselect for posting.",
)
async def season_summary(
    interaction: discord.Interaction,
    clan_name: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    """Launch the interactive composer used to build season summaries."""
    _record_command_usage(interaction, "season_summary")
    log.debug("season_summary invoked clan=%s channel=%s", clan_name, getattr(channel, "id", None))

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "Only administrators can generate seasonal summaries.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "No clans are configured yet. Use `/set_clan` before generating summaries.",
            ephemeral=True,
        )
        return

    selected_clan = clan_name if isinstance(clan_name, str) and clan_name in clan_map else next(iter(clan_map))

    channel_id = None
    if channel is not None:
        if not channel.permissions_for(channel.guild.me).send_messages:
            await send_text_response(
                interaction,
                "I do not have permission to send messages in that channel.",
                ephemeral=True,
            )
            return
        channel_id = channel.id

    fallback_channel = channel
    if fallback_channel is None and isinstance(interaction.channel, discord.TextChannel):
        fallback_channel = interaction.channel

    view = SeasonSummaryView(
        guild=interaction.guild,
        clan_map=clan_map,
        selected_clan=selected_clan,
        include_donations=True,
        include_wars=True,
        include_members=False,
        channel_id=channel_id,
        fallback_channel_id=fallback_channel.id if isinstance(fallback_channel, discord.TextChannel) else None,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture season_summary view message: %s", exc)


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


def _normalise_clan_tag(raw_tag: str) -> Optional[str]:
    """Normalise a clan tag string."""
    if not isinstance(raw_tag, str):
        return None
    cleaned = raw_tag.strip().upper()
    if not cleaned:
        return None
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    if any(
        char not in "#0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for char in cleaned
    ):
        return None
    if len(cleaned) < 6:
        return None
    return cleaned


def _default_event_label(event_key: str) -> str:
    """Return a human-friendly label for an event key."""
    if not isinstance(event_key, str) or not event_key:
        return "Event"
    template = DEFAULT_EVENT_DEFINITIONS.get(event_key)
    if isinstance(template, dict):
        label = template.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return event_key.replace("_", " ").title()


def _normalise_event_roles(container: Any) -> "OrderedDict[str, Dict[str, Any]]":
    """Convert legacy or partial event role config data into a consistent form."""
    events: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    if isinstance(container, dict):
        if isinstance(container.get("events"), dict):
            source_items = list(container["events"].items())
        else:
            source_items = [(key, value) for key, value in container.items() if key != "events"]
        for key, raw_entry in source_items:
            if not isinstance(key, str) or not key:
                continue
            if isinstance(raw_entry, dict):
                label_value = raw_entry.get("label", "")
                label = label_value.strip() if isinstance(label_value, str) else ""
                role_id_value = raw_entry.get("role_id")
                role_id = role_id_value if isinstance(role_id_value, int) else None
            elif isinstance(raw_entry, int):
                label = ""
                role_id = raw_entry
            else:
                continue

            if not label:
                label = _default_event_label(key)
            events[key] = {"label": label, "role_id": role_id}

    if not events:
        for key, template in DEFAULT_EVENT_DEFINITIONS.items():
            label = template.get("label", _default_event_label(key))
            events[key] = {"label": label, "role_id": None}

    return events


def _ensure_event_role_entries(guild_config: Dict[str, Any]) -> "OrderedDict[str, Dict[str, Any]]":
    """Ensure the guild's event role configuration uses the standard schema."""
    container = guild_config.get("event_roles")
    if not isinstance(container, dict):
        container = {}
        guild_config["event_roles"] = container

    events = _normalise_event_roles(container)
    preserved_keys = {key: value for key, value in container.items() if key != "events"}
    container.clear()
    container.update(preserved_keys)
    container["events"] = events
    return events


def _get_event_roles_for_guild(guild_id: int) -> "OrderedDict[str, Dict[str, Any]]":
    """Fetch a copy of the event role configuration for the given guild."""
    guild_config = _ensure_guild_config(guild_id)
    entries = _ensure_event_role_entries(guild_config)
    return OrderedDict(
        (key, {"label": value.get("label", _default_event_label(key)), "role_id": value.get("role_id") if isinstance(value.get("role_id"), int) else None})
        for key, value in entries.items()
    )


def _slugify_event_key(name: str, existing_keys: Iterable[str]) -> str:
    """Generate a stable event key from a human-friendly label."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not base:
        base = "event"
    candidate = base
    suffix = 2
    existing = set(existing_keys)
    while candidate in existing:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _resolve_event_selection(
    guild: discord.Guild,
    raw_value: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve user-supplied event text into a configured event entry."""
    events = _get_event_roles_for_guild(guild.id)
    if not raw_value or not raw_value.strip():
        return None, None

    def _normalise(text: str) -> str:
        return re.sub(r"[\s_\-]+", "", text.casefold())

    lookup = raw_value.strip()
    normalised_lookup = _normalise(lookup)

    for key, entry in events.items():
        if key.casefold() == lookup.casefold() or _normalise(key) == normalised_lookup:
            return key, entry

    for key, entry in events.items():
        label = entry.get("label")
        if isinstance(label, str):
            if label.casefold() == lookup.casefold() or _normalise(label) == normalised_lookup:
                return key, entry

    return None, None


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
        clan_data.setdefault("war_plans", {})
        war_nudge = clan_data.setdefault("war_nudge", {})
        if not isinstance(war_nudge.get("reasons"), list):
            war_nudge["reasons"] = []
        donation_tracking = clan_data.setdefault("donation_tracking", {})
        metrics = donation_tracking.setdefault("metrics", {})
        metrics.setdefault("top_donors", True)
        metrics.setdefault("low_donors", False)
        metrics.setdefault("negative_balance", False)
        donation_tracking.setdefault("channel_id", None)
        season_summary = clan_data.setdefault("season_summary", {})
        season_summary.setdefault("channel_id", None)
        dashboard = clan_data.setdefault("dashboard", {})
        modules = dashboard.get("modules") if isinstance(dashboard.get("modules"), list) else ["war_overview"]
        if not modules:
            modules = ["war_overview"]
        dashboard["modules"] = modules
        fmt = dashboard.get("format", "embed")
        if fmt not in DASHBOARD_FORMATS:
            fmt = "embed"
        dashboard["format"] = fmt
        dashboard.setdefault("channel_id", None)
    guild_config.setdefault("player_tags", {})
    accounts = _normalise_player_accounts_map(guild_config.get("player_accounts", {}))
    guild_config["player_accounts"] = accounts
    channels = guild_config.setdefault("channels", {})
    channels.setdefault("upgrade", None)
    channels.setdefault("donation", None)
    channels.setdefault("dashboard", None)
    _ensure_event_role_entries(guild_config)
    schedules: List[Dict[str, Any]] = []
    raw_schedules = guild_config.get("schedules", [])
    if isinstance(raw_schedules, list):
        for entry in raw_schedules:
            if not isinstance(entry, dict):
                continue
            schedules.append(
                {
                    "id": entry.get("id"),
                    "type": entry.get("type", "dashboard"),
                    "clan_name": entry.get("clan_name", ""),
                    "frequency": entry.get("frequency", "daily"),
                    "time_utc": entry.get("time_utc", "00:00"),
                    "weekday": entry.get("weekday"),
                    "channel_id": entry.get("channel_id"),
                    "next_run": entry.get("next_run"),
                    "options": entry.get("options", {}),
                }
            )
    guild_config["schedules"] = schedules
    raw_upgrade_log = guild_config.get("upgrade_log", [])
    normalised_log: List[Dict[str, Any]] = []
    if isinstance(raw_upgrade_log, list):
        for record in raw_upgrade_log[-MAX_UPGRADE_LOG_ENTRIES:]:
            if not isinstance(record, dict):
                continue
            normalised_log.append(
                {
                    "id": record.get("id"),
                    "timestamp": record.get("timestamp"),
                    "user_id": record.get("user_id"),
                    "user_name": record.get("user_name"),
                    "player_tag": record.get("player_tag"),
                    "alias": record.get("alias"),
                    "upgrade": record.get("upgrade"),
                    "notes": record.get("notes"),
                    "clan_name": record.get("clan_name"),
                    "clan_tag": record.get("clan_tag"),
                    "player_name": record.get("player_name"),
                }
            )
    guild_config["upgrade_log"] = normalised_log
    war_alert_state = guild_config.get("war_alert_state")
    if not isinstance(war_alert_state, dict):
        war_alert_state = {}
    # Ensure the nested shape is stable: clan_name -> war_tag -> list[str].
    normalised_state: Dict[str, Dict[str, List[str]]] = {}
    for clan_name, wars in war_alert_state.items():
        if not isinstance(clan_name, str) or not isinstance(wars, dict):
            continue
        clan_payload: Dict[str, List[str]] = {}
        for war_tag, sent_ids in wars.items():
            if not isinstance(war_tag, str) or not isinstance(sent_ids, list):
                continue
            cleaned = [value for value in sent_ids if isinstance(value, str) and value]
            if cleaned:
                clan_payload[war_tag] = cleaned
        if clan_payload:
            normalised_state[clan_name] = clan_payload
    guild_config["war_alert_state"] = normalised_state
    return guild_config


def _append_upgrade_log(guild_id: int, entry: Dict[str, Any]) -> None:
    """Persist a single upgrade submission, trimming the log to a safe size.

    Parameters:
        guild_id (int): Discord guild identifier owning the submission.
        entry (Dict[str, Any]): Payload describing the upgrade (tag, alias, notes, etc.).
    """
    guild_config = _ensure_guild_config(guild_id)
    upgrade_log = guild_config.setdefault("upgrade_log", [])
    if not isinstance(upgrade_log, list):
        upgrade_log = guild_config["upgrade_log"] = []
    upgrade_log.append(entry)
    if len(upgrade_log) > MAX_UPGRADE_LOG_ENTRIES:
        del upgrade_log[:-MAX_UPGRADE_LOG_ENTRIES]
    save_server_config()
    log.debug(
        "Stored upgrade submission guild=%s player_tag=%s alias=%s",
        guild_id,
        entry.get("player_tag"),
        entry.get("alias"),
    )


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


def _get_clan_entry(guild_id: int, clan_name: str) -> Optional[Dict[str, Any]]:
    """Return the stored clan entry if available."""
    guild_config = _ensure_guild_config(guild_id)
    clans = guild_config.get("clans", {})
    entry = clans.get(clan_name)
    return entry if isinstance(entry, dict) else None


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


def _collect_war_nudge_targets(
    war: coc.wars.ClanWar,
    reason_type: str,
) -> List[Tuple[Any, Dict[str, Any]]]:
    """Derive war participants that match the selected nudge reason."""
    targets: List[Tuple[Any, Dict[str, Any]]] = []
    per_member = getattr(war, "attacks_per_member", None)
    if per_member is None or per_member <= 0:
        per_member = 2  # sensible default

    for member in getattr(war.clan, "members", []):
        attacks = getattr(member, "attacks", []) or []
        used_attacks = getattr(member, "attacks_used", None)
        if used_attacks is None:
            used_attacks = len(attacks)
        remaining = getattr(member, "attacks_remaining", None)
        if remaining is None:
            remaining = max(per_member - used_attacks, 0)

        if reason_type == "unused_attacks" and remaining > 0:
            targets.append((member, {"remaining": remaining, "used": used_attacks}))
        elif reason_type == "no_attacks" and used_attacks == 0:
            targets.append((member, {"remaining": per_member, "used": 0}))
        elif reason_type == "low_stars":
            best_stars = 0
            for attack in attacks:
                stars = getattr(attack, "stars", 0)
                if stars > best_stars:
                    best_stars = stars
            if used_attacks > 0 and best_stars <= 1:
                targets.append((member, {"best_stars": best_stars, "used": used_attacks}))
    return targets


def _lookup_member_by_tag(
    guild: discord.Guild,
    tag: str,
) -> Optional[discord.Member]:
    """Attempt to resolve a Discord member from a player tag."""
    guild_config = _ensure_guild_config(guild.id)
    accounts = guild_config.get("player_accounts", {})
    for user_id_str, records in accounts.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("tag") == tag:
                if user_id_str.isdigit():
                    member = guild.get_member(int(user_id_str))
                    if member:
                        return member
    return None


def _build_reason_mention(guild: discord.Guild, reason: Dict[str, Any]) -> str:
    """Construct the prefix mention for a war nudge reason."""
    mention_parts: List[str] = []
    role_id = reason.get("mention_role_id")
    if isinstance(role_id, int):
        role = guild.get_role(role_id)
        if role:
            mention_parts.append(role.mention)
    user_id = reason.get("mention_user_id")
    if isinstance(user_id, int):
        member = guild.get_member(user_id)
        if member:
            mention_parts.append(member.mention)
    return " ".join(mention_parts)


def _get_event_role(guild: discord.Guild, event_type: str) -> Optional[discord.Role]:
    """Retrieve the configured event role for the given event type."""
    if guild is None or not isinstance(event_type, str):
        return None
    guild_config = _ensure_guild_config(guild.id)
    events = _ensure_event_role_entries(guild_config)
    entry = events.get(event_type)
    if not isinstance(entry, dict):
        return None
    role_id = entry.get("role_id")
    if isinstance(role_id, int):
        return guild.get_role(role_id)
    return None


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




async def _fetch_war_overview(clan_name: str, tag: str) -> Tuple[str, str]:
    try:
        war = await client.get_clan_war_raw(tag)
    except coc.errors.PrivateWarLog:
        return (
            "War Overview",
            "⚠️ War log is private; real-time details are unavailable.",
        )
    except coc.errors.NotFound:
        return (
            "War Overview",
            "ℹ️ This clan is not currently in a war.",
        )
    except Exception as exc:
        return (
            "War Overview",
            f"⚠️ Unable to fetch war data: {exc}",
        )

    state_value = war.state.value if hasattr(war.state, "value") else war.state
    start = _timestamp_to_datetime(war.start_time)
    end = _timestamp_to_datetime(war.end_time)
    lines = [
        f"State: {state_value}",
        f"War Tag: {war.war_tag or 'N/A'}",
        f"Team Size: {getattr(war, 'team_size', 'N/A')}",
        f"Score: {getattr(war.clan, 'stars', '?')} — {getattr(war.opponent, 'stars', '?')}",
    ]
    if start:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < start:
            lines.append(f"Begins: {start.isoformat()} ({_format_timestamp_delta(start, 0)} remaining)")
        else:
            lines.append(f"Began: {start.isoformat()}")
    if end:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < end:
            lines.append(f"Ends: {end.isoformat()} ({_format_timestamp_delta(end, 0)} remaining)")
        else:
            lines.append(f"Ended: {end.isoformat()}")

    return ("War Overview", "\n".join(lines))


async def _compose_donation_summary(
    guild: discord.Guild,
    clan_name: str,
    clan_entry: Dict[str, Any],
) -> Tuple[str, Optional[int], Dict[str, Any]]:
    donation_tracking = clan_entry.get("donation_tracking", {})
    metrics = donation_tracking.get("metrics", {})
    if not any(metrics.values()):
        raise ValueError("All donation metrics are disabled.")

    clan_tags = _clan_names_for_guild(guild.id)
    tag = clan_tags.get(clan_name)
    if not tag:
        raise ValueError(f"`{clan_name}` has no stored clan tag.")

    try:
        clan = await client.get_clan(tag)
    except Exception as exc:
        raise ValueError(f"Unable to fetch clan data: {exc}") from exc

    members = list(getattr(clan, "members", []))
    if not members:
        raise ValueError("I couldn't retrieve the member list for that clan.")

    sections: List[str] = [f"📈 **Donation Summary — {clan.name}**"]
    csv_sections: List[Tuple[str, List[str], List[List[str]]]] = []

    if metrics.get("top_donors", True):
        top_sorted = sorted(members, key=lambda m: getattr(m, "donations", 0), reverse=True)
        top_entries = [
            f"• {member.name}: {getattr(member, 'donations', 0):,} donated"
            for member in top_sorted[:5]
            if getattr(member, "donations", 0) > 0
        ]
        if top_entries:
            sections.append("🏅 **Top Donors**\n" + "\n".join(top_entries))
            csv_sections.append(
                (
                    "Top Donors",
                    ["Member", "Donated"],
                    [
                        [member.name, str(getattr(member, "donations", 0))]
                        for member in top_sorted[:10]
                    ],
                )
            )

    if metrics.get("low_donors"):
        low_sorted = sorted(members, key=lambda m: getattr(m, "donations", 0))
        low_entries = [
            f"• {member.name}: {getattr(member, 'donations', 0):,} donated"
            for member in low_sorted[:5]
        ]
        if low_entries:
            sections.append("🔻 **Lowest Donation Totals**\n" + "\n".join(low_entries))
            csv_sections.append(
                (
                    "Lowest Donation Totals",
                    ["Member", "Donated"],
                    [
                        [member.name, str(getattr(member, "donations", 0))]
                        for member in low_sorted[:10]
                    ],
                )
            )

    if metrics.get("negative_balance"):
        negative = [
            member
            for member in members
            if getattr(member, "donations", 0) - getattr(member, "donations_received", 0) < 0
        ]
        if negative:
            lines = [
                f"• {member.name}: {getattr(member, 'donations', 0):,} given vs {getattr(member, 'donations_received', 0):,} received"
                for member in negative[:5]
            ]
            sections.append("⚠️ **Negative Donation Balance**\n" + "\n".join(lines))
            csv_sections.append(
                (
                    "Negative Donation Balance",
                    ["Member", "Donated", "Received"],
                    [
                        [
                            member.name,
                            str(getattr(member, "donations", 0)),
                            str(getattr(member, "donations_received", 0)),
                        ]
                        for member in negative[:10]
                    ],
                )
            )

    payload = "\n\n".join(sections)
    context = {
        "csv_sections": csv_sections,
    }
    default_channel_id = donation_tracking.get("channel_id")
    return payload, default_channel_id, context


def _compose_event_opt_in_summary(guild: discord.Guild) -> Tuple[str, str]:
    entries = _get_event_roles_for_guild(guild.id)
    lines: List[str] = []
    for key, entry in entries.items():
        label = entry.get("label", _default_event_label(key))
        role = _get_event_role(guild, key)
        if role:
            lines.append(f"{label}: {len(role.members)} opted in")
        else:
            lines.append(f"{label}: no role configured")
    if not lines:
        lines.append("No event alert roles are configured yet.")
    return ("Event Opt-Ins", "\n".join(lines))


def _create_csv_file(sections: List[Tuple[str, List[str], List[List[str]]]]) -> Optional[bytes]:
    if not sections:
        return None
    buffer = StringIO()
    writer = csv.writer(buffer)
    for title, headers, rows in sections:
        writer.writerow([title])
        if headers:
            writer.writerow(headers)
        writer.writerows(rows)
        writer.writerow([])
    return buffer.getvalue().encode("utf-8")


def _sanitise_modules(modules: Iterable[str]) -> List[str]:
    clean = [module for module in modules if module in DASHBOARD_MODULES]
    return clean or ["war_overview"]


def _dashboard_defaults(clan_entry: Dict[str, Any]) -> Tuple[List[str], str, Optional[int]]:
    dashboard = clan_entry.get("dashboard", {}) if isinstance(clan_entry.get("dashboard"), dict) else {}
    modules = dashboard.get("modules", ["war_overview"])
    if not isinstance(modules, list):
        modules = ["war_overview"]
    fmt = dashboard.get("format", "embed")
    if fmt not in DASHBOARD_FORMATS:
        fmt = "embed"
    channel_id = dashboard.get("channel_id")
    return _sanitise_modules(modules), fmt, channel_id


async def _generate_dashboard_content(
    guild: discord.Guild,
    clan_name: str,
    modules: Iterable[str],
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, List[str], List[List[str]]]]]:
    clan_entry = _get_clan_entry(guild.id, clan_name)
    if clan_entry is None:
        raise ValueError(f"`{clan_name}` is not configured.")

    clan_tags = _clan_names_for_guild(guild.id)
    tag = clan_tags.get(clan_name)
    if not tag:
        raise ValueError(f"`{clan_name}` has no stored clan tag.")

    sections: List[Tuple[str, str]] = []
    csv_sections: List[Tuple[str, List[str], List[List[str]]]] = []

    for module in _sanitise_modules(modules):
        if module == "war_overview":
            title, text = await _fetch_war_overview(clan_name, tag)
            sections.append((title, text))
        elif module == "donation_snapshot":
            payload, _, context = await _compose_donation_summary(guild, clan_name, clan_entry)
            sections.append(("Donation Snapshot", payload))
            csv_sections.extend(context.get("csv_sections", []))
        elif module == "upgrade_queue":
            title, text, csv_data = await _compose_upgrade_snapshot(guild, clan_name, tag)
            sections.append((title, text))
            csv_sections.extend(csv_data)
        elif module == "event_opt_ins":
            sections.append(_compose_event_opt_in_summary(guild))

    return sections, csv_sections


async def _compose_upgrade_snapshot(
    guild: discord.Guild,
    clan_name: str,
    clan_tag: str,
) -> Tuple[str, str, List[Tuple[str, List[str], List[List[str]]]]]:
    """Summarise planned upgrades for members of a configured clan.

    Parameters:
        guild (discord.Guild): Discord guild requesting the snapshot.
        clan_name (str): Configured clan name used for display.
        clan_tag (str): Clash of Clans tag associated with the clan.

    Returns:
        Tuple[str, str, List[Tuple[str, List[str], List[List[str]]]]]: Title, human readable text, and optional CSV sections.
    """
    guild_config = _ensure_guild_config(guild.id)
    upgrade_log: List[Dict[str, Any]] = guild_config.get("upgrade_log", [])
    if not upgrade_log:
        return ("Upgrade Queue", "No planned upgrades logged for this server yet.", [])

    try:
        clan = await client.get_clan(clan_tag)
    except Exception as exc:
        raise ValueError(f"Unable to fetch clan roster: {exc}") from exc

    member_tags: Set[str] = {
        getattr(member, "tag")
        for member in getattr(clan, "members", [])
        if getattr(member, "tag", None)
    }

    def _matches(entry: Dict[str, Any]) -> bool:
        if entry.get("clan_name") == clan_name:
            return True
        if entry.get("clan_tag") == clan_tag:
            return True
        return entry.get("player_tag") in member_tags

    relevant_entries = [entry for entry in reversed(upgrade_log) if isinstance(entry, dict) and _matches(entry)]
    if not relevant_entries:
        return ("Upgrade Queue", "No recent upgrades logged for this clan.", [])

    lines: List[str] = []
    csv_rows: List[List[str]] = []
    for entry in relevant_entries[:10]:
        alias = entry.get("alias") or entry.get("player_tag") or "Unknown account"
        upgrade_desc = entry.get("upgrade") or "Upgrade not specified"
        submitter = entry.get("user_name") or "Unknown member"
        notes = entry.get("notes") or ""
        timestamp = _parse_iso_timestamp(entry.get("timestamp"))
        timestamp_text = timestamp.strftime("%Y-%m-%d %H:%M UTC") if timestamp else "Unknown time"

        line = f"• {alias}: {upgrade_desc} — logged by {submitter} on {timestamp_text}"
        if notes:
            line += f"\n  Notes: {notes}"
        lines.append(line)
        csv_rows.append(
            [
                alias,
                upgrade_desc,
                notes,
                submitter,
                timestamp_text,
            ]
        )

    csv_section = (
        "Upgrade Queue",
        ["Alias", "Upgrade", "Notes", "Submitted By", "Submitted (UTC)"],
        csv_rows,
    )
    return ("Upgrade Queue", "\n".join(lines), [csv_section])


def _create_dashboard_embed(
    clan_name: str,
    sections: List[Tuple[str, str]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Dashboard — {clan_name}",
        colour=discord.Colour.blurple(),
        timestamp=datetime.utcnow(),
    )
    for title, text in sections:
        text = text or "(no data)"
        if len(text) > 1024:
            chunks = _chunk_content(text, 1024)
            embed.add_field(name=title, value=chunks[0], inline=False)
            for chunk in chunks[1:]:
                embed.add_field(name=f"{title} (cont.)", value=chunk, inline=False)
        else:
            embed.add_field(name=title, value=text, inline=False)
    return embed


async def _send_dashboard(
    interaction: Optional[discord.Interaction],
    *,
    guild: discord.Guild,
    clan_name: str,
    modules: Iterable[str],
    output_format: str,
    destination: discord.TextChannel,
) -> None:
    sections, csv_sections = await _generate_dashboard_content(guild, clan_name, modules)
    if not sections:
        raise ValueError("No dashboard sections could be generated.")

    files = []
    embed: Optional[discord.Embed] = None
    if output_format in {"embed", "both"}:
        embed = _create_dashboard_embed(clan_name, sections)
    csv_payload = None
    if output_format in {"csv", "both"}:
        csv_payload = _create_csv_file(csv_sections)
        if csv_payload:
            files.append(discord.File(BytesIO(csv_payload), filename=f"dashboard_{clan_name}.csv"))

    if embed and files:
        await destination.send(embed=embed, file=files[0])
    elif embed:
        await destination.send(embed=embed)
    elif files:
        await destination.send(file=files[0])
    else:
        payload = "\n\n".join(text for _, text in sections)
        for chunk in _chunk_content(payload):
            await destination.send(chunk)

    if interaction is not None:
        await send_text_response(
            interaction,
            f"✅ Dashboard posted to {destination.mention}.",
            ephemeral=True,
        )


async def _compose_season_summary(
    guild: discord.Guild,
    clan_name: str,
    clan_entry: Dict[str, Any],
    *,
    include_donations: bool,
    include_wars: bool,
    include_members: bool,
) -> Tuple[str, Optional[int]]:
    clan_tags = _clan_names_for_guild(guild.id)
    tag = clan_tags.get(clan_name)
    if not tag:
        raise ValueError(f"`{clan_name}` has no stored tag.")

    try:
        clan = await client.get_clan(tag)
    except Exception as exc:
        raise ValueError(f"Unable to fetch clan data: {exc}") from exc

    members = list(getattr(clan, "members", []))
    sections: List[str] = [f"🏁 **Season Summary — {clan.name}**"]

    if include_wars:
        wars_section = (
            f"• War wins: {getattr(clan, 'war_wins', 'N/A')}\n"
            f"• War losses: {getattr(clan, 'war_losses', 'N/A')}\n"
            f"• War ties: {getattr(clan, 'war_ties', 'N/A')}\n"
            f"• Current streak: {getattr(clan, 'war_win_streak', 'N/A')}"
        )
        sections.append("⚔️ **War Performance**\n" + wars_section)

    if include_donations and members:
        top_donor = max(members, key=lambda m: getattr(m, "donations", 0))
        top_receiver = max(members, key=lambda m: getattr(m, "donations_received", 0))
        donation_lines = [
            f"• Top donor: {top_donor.name} ({getattr(top_donor, 'donations', 0):,})",
            f"• Most received: {top_receiver.name} ({getattr(top_receiver, 'donations_received', 0):,})",
        ]
        sections.append("🤝 **Donations**\n" + "\n".join(donation_lines))

    if include_members and members:
        top_trophies = sorted(members, key=lambda m: getattr(m, "trophies", 0), reverse=True)[:5]
        member_lines = [
            f"• {member.name}: {getattr(member, 'trophies', 0):,} trophies"
            for member in top_trophies
        ]
        sections.append("🏆 **Top Trophy Holders**\n" + "\n".join(member_lines))

    payload = "\n\n".join(sections)
    default_channel_id = clan_entry.get("season_summary", {}).get("channel_id")
    return payload, default_channel_id


def _parse_time_utc(time_str: str) -> Tuple[int, int]:
    try:
        hour_str, minute_str = time_str.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError("Time must be formatted as HH:MM in 24-hour UTC.") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("Time must be formatted as HH:MM in 24-hour UTC.")
    return hour, minute


def _calculate_next_run(
    frequency: str,
    time_utc: str,
    weekday: Optional[str] = None,
    reference: Optional[datetime] = None,
) -> str:
    hour, minute = _parse_time_utc(time_utc)
    ref = reference or datetime.utcnow()
    candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if frequency == "daily":
        if candidate <= ref:
            candidate += timedelta(days=1)
    elif frequency == "weekly":
        if weekday is None:
            raise ValueError("Weekday must be provided for weekly schedules.")
        weekday_index = WEEKDAY_MAP.get(weekday)
        if weekday_index is None:
            raise ValueError("Invalid weekday supplied.")
        days_ahead = (weekday_index - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= ref:
            days_ahead = 7
        candidate += timedelta(days=days_ahead)
    else:
        raise ValueError("Frequency must be daily or weekly.")

    candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.isoformat()


def _format_schedule_entry(schedule: Dict[str, Any]) -> str:
    next_run = schedule.get("next_run", "unknown")
    friendly = f"ID `{schedule.get('id', 'n/a')}` — {schedule.get('type', 'unknown')} for `{schedule.get('clan_name', '?')}`"
    friendly += f" every {schedule.get('frequency', 'daily')} at {schedule.get('time_utc', '00:00')} UTC"
    if schedule.get("frequency") == "weekly" and schedule.get("weekday"):
        friendly += f" on {schedule['weekday'].title()}"
    friendly += f" (next run: {next_run})"
    return friendly


async def _execute_schedule(guild: discord.Guild, schedule: Dict[str, Any]) -> None:
    schedule_type = schedule.get("type")
    clan_name = schedule.get("clan_name", "")
    clan_entry = _get_clan_entry(guild.id, clan_name)
    if clan_entry is None:
        log.debug("Skipping schedule %s: clan not configured", schedule.get("id"))
        return

    destination: Optional[discord.TextChannel] = None
    channel_id = schedule.get("channel_id")
    if isinstance(channel_id, int):
        destination = guild.get_channel(channel_id)

    if schedule_type == "dashboard":
        modules, default_format, default_channel_id = _dashboard_defaults(clan_entry)
        modules = _sanitise_modules(schedule.get("options", {}).get("modules", modules))
        format_override = schedule.get("options", {}).get("format", default_format)
        if destination is None and isinstance(default_channel_id, int):
            destination = guild.get_channel(default_channel_id)
        if destination is None:
            log.debug("Skipping dashboard schedule %s: no destination channel", schedule.get("id"))
            return
        if not destination.permissions_for(destination.guild.me).send_messages:
            log.debug("Skipping dashboard schedule %s: lacking channel permissions", schedule.get("id"))
            return
        await _send_dashboard(
            None,
            guild=guild,
            clan_name=clan_name,
            modules=modules,
            output_format=format_override,
            destination=destination,
        )
    elif schedule_type == "donation_summary":
        payload, default_channel_id, context = await _compose_donation_summary(guild, clan_name, clan_entry)
        if destination is None and isinstance(default_channel_id, int):
            destination = guild.get_channel(default_channel_id)
        if destination is None:
            log.debug("Skipping donation schedule %s: no destination channel", schedule.get("id"))
            return
        if not destination.permissions_for(destination.guild.me).send_messages:
            log.debug("Skipping donation schedule %s: lacking channel permissions", schedule.get("id"))
            return
        for chunk in _chunk_content(payload):
            await destination.send(chunk)
        csv_payload = _create_csv_file(context.get("csv_sections", []))
        if csv_payload:
            await destination.send(file=discord.File(BytesIO(csv_payload), filename=f"donation_summary_{clan_name}.csv"))
    elif schedule_type == "season_summary":
        options = schedule.get("options", {})
        include_d = options.get("include_donations", True)
        include_w = options.get("include_wars", True)
        include_m = options.get("include_members", False)
        payload, default_channel_id = await _compose_season_summary(
            guild,
            clan_name,
            clan_entry,
            include_donations=include_d,
            include_wars=include_w,
            include_members=include_m,
        )
        if destination is None and isinstance(default_channel_id, int):
            destination = guild.get_channel(default_channel_id)
        if destination is None:
            log.debug("Skipping season summary schedule %s: no destination channel", schedule.get("id"))
            return
        if not destination.permissions_for(destination.guild.me).send_messages:
            log.debug("Skipping season summary schedule %s: lacking channel permissions", schedule.get("id"))
            return
        for chunk in _chunk_content(payload):
            await destination.send(chunk)
    else:
        log.debug("Unknown schedule type %s", schedule_type)


# ---------------------------------------------------------------------------
# Slash command: /schedule_report
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="schedule_report",
    description="Create or update a scheduled report for a configured clan.",
)
@app_commands.describe(
    schedule_id="Optional schedule ID to load for editing.",
    clan_name="Optional clan to preselect when creating a new schedule.",
)
async def schedule_report(
    interaction: discord.Interaction,
    schedule_id: Optional[str] = None,
    clan_name: Optional[str] = None,
) -> None:
    """Launch the interactive scheduler used to manage automated reports."""
    _record_command_usage(interaction, "schedule_report")
    log.debug("schedule_report invoked schedule_id=%s clan=%s", schedule_id, clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "Only administrators can manage report schedules.",
            ephemeral=True,
        )
        return

    clan_map = _clan_names_for_guild(interaction.guild.id)
    if not clan_map:
        await send_text_response(
            interaction,
            "No clans are configured yet. Use `/set_clan` before scheduling reports.",
            ephemeral=True,
        )
        return

    guild_config = _ensure_guild_config(interaction.guild.id)
    schedules = copy.deepcopy(guild_config.get("schedules", []))

    valid_ids = {entry.get("id") for entry in schedules if isinstance(entry, dict)}
    selected_schedule_id = schedule_id if schedule_id in valid_ids else None

    preselected_clan: Optional[str] = None
    if isinstance(clan_name, str) and clan_name in clan_map:
        preselected_clan = clan_name
    elif selected_schedule_id:
        for entry in schedules:
            if entry.get("id") == selected_schedule_id:
                candidate = entry.get("clan_name")
                if isinstance(candidate, str) and candidate in clan_map:
                    preselected_clan = candidate
                break

    view = ScheduleConfigView(
        guild=interaction.guild,
        clan_map=clan_map,
        schedules=schedules,
        selected_schedule_id=selected_schedule_id,
        selected_clan=preselected_clan,
    )

    await interaction.response.send_message(
        view.render_message(),
        ephemeral=True,
        view=view,
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning("Failed to capture schedule_report view message: %s", exc)

# ---------------------------------------------------------------------------
# Slash command: /list_schedules
# ---------------------------------------------------------------------------
@bot.tree.command(name="list_schedules", description="List scheduled reports for this server.")
@app_commands.describe(clan_name="Optional filter for a specific configured clan.")
async def list_schedules(interaction: discord.Interaction, clan_name: Optional[str] = None) -> None:
    """Display the stored schedules, optionally filtered by clan.

    Parameters:
        interaction (discord.Interaction): Invocation context used for permission checks and responses.
        clan_name (Optional[str]): When provided, only schedules for the named clan are returned.
    """
    _record_command_usage(interaction, "list_schedules")
    log.debug("list_schedules invoked clan=%s", clan_name)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can view report schedules.",
            ephemeral=True,
        )
        return

    guild_config = _ensure_guild_config(interaction.guild.id)
    schedules = guild_config.get("schedules", [])
    if not schedules:
        await send_text_response(
            interaction,
            "ℹ️ No schedules have been configured yet.",
            ephemeral=True,
        )
        return

    filtered = schedules
    if isinstance(clan_name, str) and clan_name.strip():
        filtered = [entry for entry in schedules if entry.get("clan_name") == clan_name.strip()]
        if not filtered:
            await send_text_response(
                interaction,
                f"ℹ️ No schedules found for `{clan_name.strip()}`.",
                ephemeral=True,
            )
            return

    lines = ["🗓️ **Scheduled Reports**"] + [_format_schedule_entry(entry) for entry in filtered]
    await send_text_response(
        interaction,
        "\n".join(lines),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash command: /cancel_schedule
# ---------------------------------------------------------------------------
@bot.tree.command(name="cancel_schedule", description="Remove a scheduled report by its ID.")
@app_commands.describe(schedule_id="Use `/list_schedules` to find the ID.")
async def cancel_schedule(interaction: discord.Interaction, schedule_id: str) -> None:
    """Delete a stored schedule if it exists.

    Parameters:
        interaction (discord.Interaction): Invocation context used for permission checks and responses.
        schedule_id (str): Identifier from `/list_schedules` that should be removed.
    """
    _record_command_usage(interaction, "cancel_schedule")
    log.debug("cancel_schedule invoked id=%s", schedule_id)

    if interaction.guild is None:
        await send_text_response(
            interaction,
            "❌ This command must be used inside a Discord server.",
            ephemeral=True,
        )
        return
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
        await send_text_response(
            interaction,
            "❌ Only administrators can cancel report schedules.",
            ephemeral=True,
        )
        return

    guild_config = _ensure_guild_config(interaction.guild.id)
    schedules = guild_config.get("schedules", [])
    remaining = [entry for entry in schedules if entry.get("id") != schedule_id]
    if len(remaining) == len(schedules):
        await send_text_response(
            interaction,
            f"⚠️ I couldn't find a schedule with ID `{schedule_id}`.",
            ephemeral=True,
        )
        return

    guild_config["schedules"] = remaining
    save_server_config()
    await send_text_response(
        interaction,
        f"✅ Removed schedule `{schedule_id}`.",
        ephemeral=True,
    )


# Poll every five minutes so 5-minute alert thresholds are respected.
@tasks.loop(minutes=5)
async def war_alert_loop() -> None:
    """Poll tracked clans and emit time-based war reminders."""
    log.debug("war_alert_loop tick")
    now = datetime.now(timezone.utc)
    for guild_id in list(server_config.keys()):
        guild_config = _ensure_guild_config(guild_id)
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue  # Skip guilds the bot is not currently connected to

        clans: Dict[str, Dict[str, Any]] = guild_config.get("clans", {})  # type: ignore[assignment]
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
            except coc.errors.NotFound:
                _clear_war_alert_state_for_clan(guild.id, clan_name)
                continue  # Skip clans without accessible war data
            except (coc.errors.PrivateWarLog, coc.errors.GatewayError):
                continue  # Skip clans without accessible war data
            except Exception:
                continue  # Fail-safe for unexpected library errors

            _prune_war_alert_state_for_clan(guild.id, clan_name, getattr(war, "war_tag", None) or tag)
            for alert in _collect_war_alerts(guild, clan_name, tag, war, alert_role, now):
                await send_channel_message(target_channel, alert)

        if guild_id in _dirty_war_alert_state_guilds:
            if _persist_war_alert_state_for_guild(guild_id):
                save_server_config()
            _dirty_war_alert_state_guilds.discard(guild_id)


@war_alert_loop.before_loop
async def _war_alert_loop_ready() -> None:
    """Delay the alert loop until the bot session is ready."""
    log.debug("Waiting for bot readiness before starting alert loop")
    await bot.wait_until_ready()


def ensure_war_alert_loop_running() -> None:
    """Start the alert loop once the bot is ready."""
    log.debug("ensure_war_alert_loop_running called")
    _load_war_alert_state_from_config()
    if not war_alert_loop.is_running():
        war_alert_loop.start()


@tasks.loop(minutes=1)
async def report_schedule_loop() -> None:
    """Poll stored schedules and execute any that are due."""
    log.debug("report_schedule_loop tick")
    now = datetime.now(timezone.utc)
    for guild_id, _ in server_config.items():
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        guild_config = _ensure_guild_config(guild_id)
        schedules = guild_config.get("schedules", [])
        modified = False
        for schedule in schedules:
            next_run_str = schedule.get("next_run")
            next_run = _parse_iso_timestamp(next_run_str) if isinstance(next_run_str, str) else None
            if next_run is None or next_run <= now:
                try:
                    await _execute_schedule(guild, schedule)
                except Exception:
                    log.exception("Error while executing schedule %s", schedule.get("id"))
                finally:
                    schedule["next_run"] = _calculate_next_run(
                        schedule.get("frequency", "daily"),
                        schedule.get("time_utc", "00:00"),
                        weekday=schedule.get("weekday"),
                    )
                    modified = True
        if modified:
            save_server_config()


@report_schedule_loop.before_loop
async def _schedule_loop_ready() -> None:
    """Delay schedule processing until the bot is ready."""
    log.debug("Waiting for bot readiness before starting schedule loop")
    await bot.wait_until_ready()


def ensure_report_schedule_loop_running() -> None:
    """Ensure the scheduled report loop is active."""
    log.debug("ensure_report_schedule_loop_running called")
    if not report_schedule_loop.is_running():
        report_schedule_loop.start()


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


# ---------------------------------------------------------------------------
# Slash command: /clan_war_info_menu
# ---------------------------------------------------------------------------
@bot.tree.command(name="clan_war_info_menu", description="Explore war data using a select menu.")
@app_commands.describe(clan_name="Configured clan to inspect.")
async def clan_war_info_menu(interaction: discord.Interaction, clan_name: str):
    """Provide an interactive view of war details using a select menu and share buttons."""
    _record_command_usage(interaction, "clan_war_info_menu")
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
        enemy_positions: Iterable[int],
        alert_role: Optional[discord.Role],
    ):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.guild = interaction.guild
        self.clan_name = clan_name
        self.home_roster = home_roster
        self.enemy_positions = sorted(int(pos) for pos in enemy_positions)
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
            enemy_positions=self.enemy_positions,
            alert_role=self.alert_role,
        )
        await interaction.response.edit_message(
            content=per_player_view.render_message(),
            view=per_player_view,
        )
        if interaction.message is not None:
            per_player_view.message = interaction.message
        log.debug(
            "PerPlayerAssignmentView launched message_id=%s",
            getattr(per_player_view.message, "id", None),
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
        enemy_positions: Iterable[int],
        alert_role: Optional[discord.Role],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.parent = parent
        self.guild = parent.guild
        self.clan_name = parent.clan_name
        self.home_roster = home_roster
        self.enemy_positions = sorted(int(pos) for pos in enemy_positions)
        self._valid_enemy_positions: Set[int] = set(self.enemy_positions)
        self.alert_role = alert_role
        self.assignments: Dict[int, List[int]] = {}
        self.message: Optional[discord.Message] = None
        self._add_home_base_selects()
        log.debug("PerPlayerAssignmentView initialised children=%s", [
            (child.__class__.__name__, getattr(child, 'custom_id', None), getattr(child, 'row', None))
            for child in self.children
        ])

    def _add_home_base_selects(self) -> None:
        """Add one or more selects so every base can be chosen."""
        sorted_bases = sorted(self.home_roster.keys())
        if not sorted_bases:
            return

        chunks: List[List[int]] = [
            sorted_bases[i : i + 25] for i in range(0, len(sorted_bases), 25)
        ]
        buttons = [child for child in self.children if isinstance(child, discord.ui.Button)]
        for button in buttons:
            self.remove_item(button)
        for index, chunk in enumerate(chunks):
            if index >= 5:
                log.warning(
                    "PerPlayerAssignmentView has more than 5 select groups; truncating display after row %s",
                    index - 1,
                )
                break
            start = chunk[0]
            end = chunk[-1]
            if len(chunks) == 1:
                placeholder = "Pick a home base to assign targets."
            else:
                placeholder = f"Bases {start} - {end}"
            self.add_item(
                HomeBaseSelect(
                    parent_view=self,
                    base_numbers=chunk,
                    placeholder=placeholder,
                    row=index,
                    custom_id=f"home_base_select_{self.clan_name.replace(' ', '_')}_{start}_{end}",
                )
            )

        button_start_row = min(4, len(chunks))
        for idx, button in enumerate(buttons):
            button.row = min(4, button_start_row + idx)
            self.add_item(button)

    def _layout_action_buttons(self) -> None:
        """Ensure action buttons are placed on rows after the selects."""
        select_rows = sum(
            isinstance(child, HomeBaseSelect) for child in self.children
        )
        next_row = select_rows
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.row = next_row
                next_row += 1

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

    async def _refresh_message(self) -> None:
        """Update the interactive message with the latest assignment summary."""
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning(
                "PerPlayerAssignmentView failed to refresh message: %s",
                exc,
            )

    def _set_children_disabled(self, disabled: bool) -> None:
        for child in self.children:
            child.disabled = disabled

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

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        log.exception(
            "PerPlayerAssignmentView error item=%s values=%s",
            item,
            getattr(item, "values", None),
            exc_info=error,
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "⚠️ Something went wrong while updating assignments. Please try again.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "⚠️ Something went wrong while updating assignments. Please try again.",
                ephemeral=True,
            )

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
        if self.message is None and interaction.message is not None:
            self.message = interaction.message
        channel = self.parent.channel
        if channel is None or not channel.permissions_for(self.guild.me).send_messages:
            await interaction.response.send_message(
                "⚠️ I don't have permission to post in this channel. Try again after adjusting permissions.",
                ephemeral=True,
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        self._set_children_disabled(True)
        await self._refresh_message()

        try:
            for chunk in _chunk_content(content):
                await channel.send(chunk)
        except discord.HTTPException as exc:
            log.exception(
                "PerPlayerAssignmentView failed to post assignments for clan %s: %s",
                self.clan_name,
                exc,
            )
            self._set_children_disabled(False)
            await self._refresh_message()
            await interaction.followup.send(
                f"⚠️ Couldn't post assignments because Discord returned: {exc}.",
                ephemeral=True,
            )
            return

        success_text = "✅ Assignments posted to the channel."
        if self.message is not None:
            try:
                await self.message.edit(content=success_text, view=None)
            except discord.HTTPException as exc:
                log.warning(
                    "PerPlayerAssignmentView couldn't finalise the view after posting: %s",
                    exc,
                )
        await interaction.followup.send(success_text, ephemeral=True)
        self.stop()
    @discord.ui.button(label="Clear selections", style=discord.ButtonStyle.danger, emoji="🧹")
    async def clear_all(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        log.debug("PerPlayerAssignmentView clearing assignments for clan %s", self.clan_name)
        self.clear_assignments()
        if interaction.message is not None:
            self.message = interaction.message
        await interaction.response.edit_message(
            content=self.render_message(),
            view=self,
        )


class HomeBaseSelect(discord.ui.Select):
    """Select component that lets admins choose the home base to configure."""

    def __init__(
        self,
        *,
        parent_view: PerPlayerAssignmentView,
        base_numbers: List[int],
        placeholder: str,
        row: int,
        custom_id: str,
    ):
        options = [
            discord.SelectOption(
                label=f"{position}. {parent_view.home_roster.get(position, f'Base {position}')}",
                value=str(position),
                description="Select to assign enemy targets.",
            )
            for position in base_numbers
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
            custom_id=custom_id,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        log.debug("HomeBaseSelect callback triggered values=%s", self.values)
        try:
            base = int(self.values[0])
        except (ValueError, TypeError) as exc:
            log.warning("HomeBaseSelect received invalid base value %s: %s", self.values, exc)
            await interaction.response.send_message(
                "⚠️ Something went wrong reading that base selection. Please try again.",
                ephemeral=True,
            )
            return
        if interaction.message is not None:
            self.parent_view.message = interaction.message
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

        invalid_targets = [
            num for num in numbers if num not in self.parent_view._valid_enemy_positions
        ]
        if invalid_targets:
            guidance = ""
            if self.parent_view.enemy_positions:
                visible = ", ".join(str(num) for num in self.parent_view.enemy_positions[:10])
                if len(self.parent_view.enemy_positions) > 10:
                    visible += ", ..."
                guidance = f" Valid bases include: {visible}."
            await interaction.response.send_message(
                f"⚠️ Enemy base {invalid_targets[0]} is not present in the current war.{guidance}",
                ephemeral=True,
            )
            return

        self.parent_view.update_assignment(self.base, numbers)
        await self.parent_view._refresh_message()
        log.debug(
            "AssignmentModal stored targets %s for base %s in clan %s",
            numbers,
            self.base,
            self.parent_view.clan_name,
        )
        confirmation = f"✅ Stored assignment for base {self.base}."
        if not interaction.response.is_done():
            await interaction.response.send_message(
                confirmation,
                ephemeral=True,
                delete_after=5,
            )
        else:
            await interaction.followup.send(
                confirmation,
                ephemeral=True,
                delete_after=5,
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


class DashboardModuleSelect(discord.ui.Select):
    def __init__(self, parent_view: "DashboardConfigView", selected: List[str]):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=key in selected,
            )
            for key, label in DASHBOARD_MODULES.items()
        ]
        super().__init__(
            placeholder="Select dashboard modules",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if hasattr(self.parent_view, "handle_module_update"):
            self.parent_view.handle_module_update(list(self.values))
        else:
            self.parent_view.selected_modules = list(self.values)
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class DashboardFormatSelect(discord.ui.Select):
    def __init__(self, parent_view: "DashboardConfigView", selected: str):
        options = [
            discord.SelectOption(label=fmt.upper(), value=fmt, default=(fmt == selected))
            for fmt in sorted(DASHBOARD_FORMATS)
        ]
        super().__init__(
            placeholder="Select output format",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        new_format = self.values[0]
        if hasattr(self.parent_view, "handle_format_update"):
            self.parent_view.handle_format_update(new_format)
        else:
            self.parent_view.selected_format = new_format
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class DashboardConfigView(discord.ui.View):
    def __init__(
        self,
        *,
        guild: discord.Guild,
        clan_name: str,
        initial_modules: List[str],
        initial_format: str,
        channel: Optional[discord.TextChannel],
    ):
        super().__init__(timeout=300)
        self.guild = guild
        self.clan_name = clan_name
        self.selected_modules = _sanitise_modules(initial_modules)
        self.selected_format = initial_format if initial_format in DASHBOARD_FORMATS else "embed"
        self.channel = channel
        self.add_item(DashboardModuleSelect(self, self.selected_modules))
        self.add_item(DashboardFormatSelect(self, self.selected_format))

    def render_message(self) -> str:
        module_labels = [DASHBOARD_MODULES.get(m, m) for m in self.selected_modules]
        channel_text = self.channel.mention if self.channel else "Current or invoking channel"
        return (
            f"Configuration for `{self.clan_name}`\n"
            f"• Modules: {', '.join(module_labels)}\n"
            f"• Format: {self.selected_format.upper()}\n"
            f"• Default channel: {channel_text}\n\n"
            "Use the dropdowns to adjust modules and format, then press **Save** to persist your changes."
        )

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore[override]
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only administrators can update the dashboard configuration.",
                ephemeral=True,
            )
            return

        clan_entry = _get_clan_entry(self.guild.id, self.clan_name)
        if clan_entry is None:
            await interaction.response.send_message(
                f"⚠️ `{self.clan_name}` is not configured.",
                ephemeral=True,
            )
            return

        dashboard = clan_entry.setdefault("dashboard", {})
        dashboard["modules"] = self.selected_modules
        dashboard["format"] = self.selected_format
        if self.channel is not None:
            dashboard["channel_id"] = self.channel.id
        save_server_config()
        self.disable_all_items()
        await interaction.response.edit_message(
            content=f"✅ Dashboard settings saved for `{self.clan_name}`.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # type: ignore[override]
        self.disable_all_items()
        await interaction.response.edit_message(
            content="Configuration cancelled.",
            view=self,
        )

    async def on_timeout(self) -> None:
        self.disable_all_items()


class EventRoleCreateModal(discord.ui.Modal):
    """Modal to capture details when adding a new event entry."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(title="Add Event", timeout=None)
        self.parent_view = parent_view
        self.event_name = discord.ui.TextInput(
            label="Event name",
            placeholder="e.g. Clan Games",
            max_length=80,
        )
        self.add_item(self.event_name)
        self.role_name = discord.ui.TextInput(
            label="Role name (optional)",
            placeholder="Create and assign a new Discord role",
            required=False,
            max_length=100,
        )
        self.add_item(self.role_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_create_event(
            interaction,
            self.event_name.value,
            self.role_name.value,
        )


class EventRoleRenameModal(discord.ui.Modal):
    """Modal to rename an existing event entry."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(title="Rename Event", timeout=None)
        self.parent_view = parent_view
        self.new_name = discord.ui.TextInput(
            label="New name",
            default=parent_view.current_label,
            max_length=80,
        )
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_rename_event(
            interaction,
            self.new_name.value,
        )


class EventRoleCreateRoleModal(discord.ui.Modal):
    """Modal to capture a role name when creating a Discord role."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(title="Create Role", timeout=None)
        self.parent_view = parent_view
        suggested = f"{parent_view.current_label} Alerts".strip()
        self.role_name = discord.ui.TextInput(
            label="Role name",
            default=suggested[:100],
            max_length=100,
        )
        self.add_item(self.role_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_create_role(
            interaction,
            self.role_name.value,
        )


class EventRoleDeleteModal(discord.ui.Modal):
    """Modal that confirms removal of an event entry."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(title="Remove Event", timeout=None)
        self.parent_view = parent_view
        self.confirmation = discord.ui.TextInput(
            label=f"Type '{parent_view.current_label}' to confirm",
            placeholder=parent_view.current_label,
            max_length=80,
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_delete_event(
            interaction,
            self.confirmation.value,
        )


class EventRoleSelect(discord.ui.Select):
    """Dropdown for selecting which event to manage."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = []
        for key, entry in parent_view.events.items():
            label = entry.get("label") or _default_event_label(key)
            role = parent_view._role_from_entry(entry)
            role_id = entry.get("role_id")
            if role is not None:
                description = f"Role: {role.name}"
            elif isinstance(role_id, int):
                description = f"Role missing (ID {role_id})"
            else:
                description = "No role assigned"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=key,
                    description=description[:100],
                    default=(key == parent_view.selected_key),
                )
            )
        super().__init__(
            placeholder="Select an event to manage",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.selected_key = self.values[0]
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class EventRoleRoleSelect(discord.ui.RoleSelect):
    """Role selector for assigning an existing Discord role."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        self.parent_view = parent_view
        default_role = parent_view._role_from_entry(parent_view.current_entry)
        default_values = [default_role] if default_role is not None else []
        super().__init__(
            placeholder="Assign an existing role",
            min_values=0,
            max_values=1,
            default_values=default_values,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        role = self.values[0] if self.values else None
        self.parent_view.set_role_for_current(role.id if role else None)
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class EventRoleAddButton(discord.ui.Button):
    """Button that opens the modal to add a new event."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Add Event", style=discord.ButtonStyle.primary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can add events.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EventRoleCreateModal(self.parent_view))


class EventRoleRenameButton(discord.ui.Button):
    """Button that opens the rename modal for the selected event."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Rename Event", style=discord.ButtonStyle.secondary, row=2)
        self.parent_view = parent_view
        if not parent_view.events:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can rename events.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EventRoleRenameModal(self.parent_view))


class EventRoleCreateRoleButton(discord.ui.Button):
    """Button that opens a modal to create a Discord role."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Create Role", style=discord.ButtonStyle.success, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can create roles from this view.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EventRoleCreateRoleModal(self.parent_view))


class EventRoleClearRoleButton(discord.ui.Button):
    """Button that clears the assigned role from the selected event."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Clear Role", style=discord.ButtonStyle.secondary, row=2)
        self.parent_view = parent_view
        if parent_view.current_entry.get("role_id") is None:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can clear roles.",
                ephemeral=True,
            )
            return
        await self.parent_view.handle_clear_role(interaction)


class EventRoleDeleteButton(discord.ui.Button):
    """Button that opens the delete confirmation modal."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Remove Event", style=discord.ButtonStyle.danger, row=3)
        self.parent_view = parent_view
        if len(parent_view.events) <= 1:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can remove events.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EventRoleDeleteModal(self.parent_view))


class EventRoleSaveButton(discord.ui.Button):
    """Button that persists the pending configuration."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Save", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can save changes.",
                ephemeral=True,
            )
            return
        await self.parent_view.handle_save(interaction)


class EventRoleCancelButton(discord.ui.Button):
    """Button that cancels the interaction."""

    def __init__(self, parent_view: "EventRoleConfigView"):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_cancel(interaction)


class EventRoleConfigView(discord.ui.View):
    """Interactive interface for managing event alert roles."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        events: "OrderedDict[str, Dict[str, Any]]",
        selected_key: str,
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.message: Optional[discord.Message] = None
        copied_events = copy.deepcopy(events)
        self.events: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for key, entry in copied_events.items():
            if not isinstance(key, str):
                continue
            if isinstance(entry, dict):
                label_value = entry.get("label")
                role_id_value = entry.get("role_id")
            else:
                label_value = None
                role_id_value = None
            label = label_value if isinstance(label_value, str) and label_value.strip() else _default_event_label(key)
            role_id = role_id_value if isinstance(role_id_value, int) else None
            self.events[key] = {"label": label.strip(), "role_id": role_id}
        if not self.events:
            default_key = next(iter(DEFAULT_EVENT_DEFINITIONS.keys()), "event")
            self.events[default_key] = {"label": _default_event_label(default_key), "role_id": None}
        self.selected_key = selected_key if selected_key in self.events else next(iter(self.events))
        self.refresh_components()

    def _ensure_selected(self) -> None:
        if not self.events:
            return
        if self.selected_key not in self.events:
            self.selected_key = next(iter(self.events))

    @property
    def current_entry(self) -> Dict[str, Any]:
        self._ensure_selected()
        return self.events[self.selected_key]

    @property
    def current_label(self) -> str:
        entry = self.current_entry
        label = entry.get("label")
        if isinstance(label, str) and label.strip():
            return label
        return _default_event_label(self.selected_key)

    def user_is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else self.guild.get_member(interaction.user.id)
        return bool(member and member.guild_permissions.administrator)

    def refresh_components(self) -> None:
        self.clear_items()
        self._ensure_selected()
        self.add_item(EventRoleSelect(self))
        self.add_item(EventRoleRoleSelect(self))
        self.add_item(EventRoleAddButton(self))
        self.add_item(EventRoleRenameButton(self))
        self.add_item(EventRoleCreateRoleButton(self))
        self.add_item(EventRoleClearRoleButton(self))
        self.add_item(EventRoleDeleteButton(self))
        self.add_item(EventRoleSaveButton(self))
        self.add_item(EventRoleCancelButton(self))

    def render_message(self) -> str:
        self._ensure_selected()
        lines = ["**Event Role Configuration**"]
        for key, entry in self.events.items():
            role_desc = self._describe_role(entry)
            prefix = "▶️" if key == self.selected_key else "•"
            lines.append(f"{prefix} {entry.get('label', _default_event_label(key))} — {role_desc}")
        current_entry = self.current_entry
        lines.extend(
            [
                "",
                f"Managing: **{current_entry.get('label', self.selected_key)}**",
                f"Assigned role: {self._describe_role(current_entry)}",
                "",
                "Use the dropdown to choose an event. Create or assign roles with the controls below, then press **Save** to persist your changes.",
            ]
        )
        return "\n".join(lines)

    def _role_from_entry(self, entry: Dict[str, Any]) -> Optional[discord.Role]:
        role_id = entry.get("role_id")
        if isinstance(role_id, int):
            return self.guild.get_role(role_id)
        return None

    def _describe_role(self, entry: Dict[str, Any]) -> str:
        role = self._role_from_entry(entry)
        if role is not None:
            return f"{role.mention} ({role.name})"
        role_id = entry.get("role_id")
        if isinstance(role_id, int):
            return f"Missing role (ID {role_id})"
        return "Not assigned"

    def set_role_for_current(self, role_id: Optional[int]) -> None:
        self._ensure_selected()
        self.events[self.selected_key]["role_id"] = role_id

    async def handle_create_event(self, interaction: discord.Interaction, raw_label: str, raw_role_name: str) -> None:
        if not self.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can add events.",
                ephemeral=True,
            )
            return
        label = (raw_label or "").strip()
        if not label:
            await interaction.response.send_message(
                "⚠️ Provide a name for the new event.",
                ephemeral=True,
            )
            return
        if any(entry.get("label", "").lower() == label.lower() for entry in self.events.values()):
            await interaction.response.send_message(
                f"⚠️ An event named `{label}` already exists.",
                ephemeral=True,
            )
            return
        new_key = _slugify_event_key(label, self.events.keys())
        self.events[new_key] = {"label": label, "role_id": None}
        self.selected_key = new_key
        role_feedback: Optional[str] = None
        role_name = (raw_role_name or "").strip()
        if role_name:
            role, error = await self._create_role(role_name)
            if role is not None:
                self.events[new_key]["role_id"] = role.id
                role_feedback = f" Created role `{role.name}` and linked it."
            else:
                role_feedback = f" {error or 'Unable to create the role.'}"
        self.refresh_components()
        await self._commit_message_update()
        message = f"✅ Added event `{label}`."
        if role_feedback:
            message += role_feedback
        await interaction.response.send_message(message, ephemeral=True)

    async def handle_rename_event(self, interaction: discord.Interaction, raw_label: str) -> None:
        if not self.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can rename events.",
                ephemeral=True,
            )
            return
        new_label = (raw_label or "").strip()
        if not new_label:
            await interaction.response.send_message(
                "⚠️ Provide a new name for the event.",
                ephemeral=True,
            )
            return
        if any(key != self.selected_key and entry.get("label", "").lower() == new_label.lower() for key, entry in self.events.items()):
            await interaction.response.send_message(
                f"⚠️ Another event is already named `{new_label}`.",
                ephemeral=True,
            )
            return
        entry = self.current_entry
        old_label = entry.get("label", self.selected_key)
        entry["label"] = new_label
        self.refresh_components()
        await self._commit_message_update()
        await interaction.response.send_message(
            f"✅ Renamed `{old_label}` to `{new_label}`.",
            ephemeral=True,
        )

    async def handle_create_role(self, interaction: discord.Interaction, raw_role_name: str) -> None:
        if not self.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can create roles from this view.",
                ephemeral=True,
            )
            return
        role_name = (raw_role_name or "").strip()
        if not role_name:
            await interaction.response.send_message(
                "⚠️ Provide a name for the new role.",
                ephemeral=True,
            )
            return
        role, error = await self._create_role(role_name)
        if role is None:
            await interaction.response.send_message(error or "⚠️ Unable to create the role.", ephemeral=True)
            return
        self.current_entry["role_id"] = role.id
        self.refresh_components()
        await self._commit_message_update()
        await interaction.response.send_message(
            f"✅ Created role `{role.name}` and linked it to `{self.current_label}`.",
            ephemeral=True,
        )

    async def handle_delete_event(self, interaction: discord.Interaction, raw_confirmation: str) -> None:
        if not self.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can remove events.",
                ephemeral=True,
            )
            return
        if len(self.events) <= 1:
            await interaction.response.send_message(
                "⚠️ At least one event must remain configured.",
                ephemeral=True,
            )
            return
        confirmation = (raw_confirmation or "").strip()
        expected = self.current_label
        if confirmation.lower() != expected.lower():
            await interaction.response.send_message(
                f"⚠️ Type `{expected}` to confirm removal.",
                ephemeral=True,
            )
            return
        removed_key = self.selected_key
        removed_label = expected
        self.events.pop(removed_key, None)
        self._ensure_selected()
        self.refresh_components()
        await self._commit_message_update()
        await interaction.response.send_message(
            f"✅ Removed event `{removed_label}`.",
            ephemeral=True,
        )

    async def handle_clear_role(self, interaction: discord.Interaction) -> None:
        if not self.user_is_admin(interaction):
            await interaction.response.send_message(
                "⚠️ Only administrators can clear roles.",
                ephemeral=True,
            )
            return
        entry = self.current_entry
        if entry.get("role_id") is None:
            await interaction.response.send_message(
                "⚠️ No role is currently assigned.",
                ephemeral=True,
            )
            return
        entry["role_id"] = None
        self.refresh_components()
        if interaction.message is not None:
            self.message = interaction.message
        await interaction.response.edit_message(
            content=self.render_message(),
            view=self,
        )
        await interaction.followup.send(
            f"✅ Cleared the assigned role for `{self.current_label}`.",
            ephemeral=True,
        )

    async def handle_save(self, interaction: discord.Interaction) -> None:
        payload: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for key, entry in self.events.items():
            payload[key] = {
                "label": entry.get("label", _default_event_label(key)),
                "role_id": entry.get("role_id") if isinstance(entry.get("role_id"), int) else None,
            }
        guild_config = _ensure_guild_config(self.guild.id)
        config_events = _ensure_event_role_entries(guild_config)
        config_events.clear()
        for key, value in payload.items():
            config_events[key] = {"label": value["label"], "role_id": value["role_id"]}
        save_server_config()
        self.disable_all_items()
        if interaction.message is not None:
            self.message = interaction.message
        await interaction.response.edit_message(
            content="✅ Event role configuration saved.",
            view=self,
        )

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        if interaction.message is not None:
            self.message = interaction.message
        await interaction.response.edit_message(
            content="Configuration cancelled.",
            view=self,
        )

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()

    async def _commit_message_update(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh event role configuration view: %s", exc)

    async def _create_role(self, role_name: str) -> Tuple[Optional[discord.Role], Optional[str]]:
        bot_member = self.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return None, "⚠️ I lack Manage Roles permission to create roles."
        try:
            role = await self.guild.create_role(name=role_name, reason="Event role configuration")
        except discord.Forbidden:
            return None, "⚠️ I could not create the role due to missing permissions."
        except discord.HTTPException as exc:
            return None, f"⚠️ Failed to create role: {exc}"
        return role, None


class DashboardRunClanSelect(discord.ui.Select):
    '''Select menu for choosing which clan's dashboard to generate.'''

    def __init__(self, parent_view: "DashboardRunView"):
        options = [
            discord.SelectOption(label=name, value=name, default=(name == parent_view.selected_clan))
            for name in sorted(parent_view.clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Choose a clan",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        new_clan = self.values[0]
        self.parent_view.set_clan(new_clan)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class DashboardRunChannelSelect(discord.ui.ChannelSelect):
    '''Channel selector for dashboard delivery.'''

    def __init__(self, parent_view: "DashboardRunView"):
        self.parent_view = parent_view
        default_channel = parent_view.get_explicit_channel()
        default_values = [default_channel] if default_channel is not None else []
        super().__init__(
            placeholder="Pick a channel (leave blank to use the default/current channel)",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            default_values=default_values,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel = self.values[0] if self.values else None
        self.parent_view.set_channel(channel)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class DashboardRunPreviewButton(discord.ui.Button):
    '''Button that generates an ephemeral preview of the dashboard.'''

    def __init__(self, parent_view: "DashboardRunView"):
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.defer(ephemeral=True, thinking=True)
        clan_entry = _get_clan_entry(self.parent_view.guild.id, self.parent_view.selected_clan)
        if clan_entry is None:
            await interaction.followup.send(
                f"{self.parent_view.selected_clan} is no longer configured.",
                ephemeral=True,
            )
            return
        try:
            sections, csv_sections = await _generate_dashboard_content(
                self.parent_view.guild,
                self.parent_view.selected_clan,
                self.parent_view.selected_modules,
            )
        except ValueError as exc:
            await interaction.followup.send(f"{exc}", ephemeral=True)
            return

        if not sections:
            await interaction.followup.send("No dashboard content was produced.", ephemeral=True)
            return

        embed = None
        files = []
        if self.parent_view.selected_format in {"embed", "both"}:
            embed = _create_dashboard_embed(self.parent_view.selected_clan, sections)
        if self.parent_view.selected_format in {"csv", "both"}:
            csv_payload = _create_csv_file(csv_sections)
            if csv_payload:
                files.append(discord.File(BytesIO(csv_payload), filename=f"dashboard_{self.parent_view.selected_clan}.csv"))

        message = "Here is the current dashboard preview."
        if embed or files:
            await interaction.followup.send(
                message,
                embed=embed,
                files=files if files else None,
                ephemeral=True,
            )
        else:
            payload = "\n\n".join(text for _, text in sections)
            await interaction.followup.send(f"{message}\n{payload}", ephemeral=True)


class DashboardRunPostButton(discord.ui.Button):
    '''Button that posts the dashboard to the selected channel.'''

    def __init__(self, parent_view: "DashboardRunView"):
        super().__init__(label="Post Dashboard", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel = self.parent_view.get_destination_channel()
        if channel is None:
            await interaction.response.send_message(
                "I couldn't determine a channel to post in. Select one or run the command in a text channel.",
                ephemeral=True,
            )
            return
        if not channel.permissions_for(channel.guild.me).send_messages:
            await interaction.response.send_message(
                "I don't have permission to post in that channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        clan_entry = _get_clan_entry(self.parent_view.guild.id, self.parent_view.selected_clan)
        if clan_entry is None:
            await interaction.followup.send(
                f"`{self.parent_view.selected_clan}` is no longer configured.",
                ephemeral=True,
            )
            return
        try:
            await _send_dashboard(
                interaction,
                guild=self.parent_view.guild,
                clan_name=self.parent_view.selected_clan,
                modules=self.parent_view.selected_modules,
                output_format=self.parent_view.selected_format,
                destination=channel,
            )
        except ValueError as exc:
            await interaction.followup.send(f"{exc}", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Failed to post the dashboard: {exc}",
                ephemeral=True,
            )


class DashboardRunCancelButton(discord.ui.Button):
    '''Button that closes the dashboard view.'''

    def __init__(self, parent_view: "DashboardRunView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content="Dashboard view closed.",
            view=self.parent_view,
        )


class DashboardRunView(discord.ui.View):
    '''Interactive interface for generating dashboards on demand.'''

    def __init__(
        self,
        *,
        guild: discord.Guild,
        clan_map: Dict[str, str],
        selected_clan: str,
        initial_modules: List[str],
        initial_format: str,
        initial_channel: Optional[discord.TextChannel],
        fallback_channel: Optional[discord.TextChannel],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_map = clan_map
        self.message: Optional[discord.Message] = None
        self.selected_clan = selected_clan
        self.selected_modules = _sanitise_modules(initial_modules)
        self.selected_format = initial_format if initial_format in DASHBOARD_FORMATS else "embed"
        self.selected_channel_id = initial_channel.id if isinstance(initial_channel, discord.TextChannel) else None
        self.fallback_channel_id = fallback_channel.id if isinstance(fallback_channel, discord.TextChannel) else None
        self.refresh_components()

    def handle_module_update(self, values: Iterable[str]) -> None:
        self.selected_modules = _sanitise_modules(values)

    def handle_format_update(self, value: str) -> None:
        self.selected_format = value if value in DASHBOARD_FORMATS else "embed"

    def set_clan(self, clan_name: str) -> None:
        if clan_name not in self.clan_map:
            return
        self.selected_clan = clan_name
        modules, fmt, channel_id = self._clan_defaults(clan_name)
        self.selected_modules = _sanitise_modules(modules)
        self.selected_format = fmt if fmt in DASHBOARD_FORMATS else "embed"
        resolved = self._resolve_channel(channel_id)
        if resolved is not None:
            self.selected_channel_id = resolved.id
        else:
            self.selected_channel_id = self.fallback_channel_id
        self.refresh_components()

    def set_channel(self, channel: Optional[discord.abc.GuildChannel]) -> None:
        if isinstance(channel, discord.TextChannel):
            self.selected_channel_id = channel.id
        else:
            self.selected_channel_id = None

    def get_explicit_channel(self) -> Optional[discord.TextChannel]:
        return self._resolve_channel(self.selected_channel_id)

    def get_destination_channel(self) -> Optional[discord.TextChannel]:
        explicit = self.get_explicit_channel()
        if explicit is not None:
            return explicit
        return self._resolve_channel(self.fallback_channel_id)

    def _resolve_channel(self, channel_id: Optional[int]) -> Optional[discord.TextChannel]:
        if isinstance(channel_id, int):
            channel = self.guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    def _clan_defaults(self, clan_name: str) -> Tuple[List[str], str, Optional[int]]:
        clan_entry = _get_clan_entry(self.guild.id, clan_name)
        if clan_entry is None:
            return ["war_overview"], "embed", None
        modules, fmt, channel_id = _dashboard_defaults(clan_entry)
        return modules, fmt, channel_id if isinstance(channel_id, int) else None

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(DashboardRunClanSelect(self))
        self.add_item(DashboardModuleSelect(self, self.selected_modules))
        self.add_item(DashboardFormatSelect(self, self.selected_format))
        self.add_item(DashboardRunChannelSelect(self))
        self.add_item(DashboardRunPreviewButton(self))
        self.add_item(DashboardRunPostButton(self))
        self.add_item(DashboardRunCancelButton(self))

    def render_message(self) -> str:
        module_labels = [DASHBOARD_MODULES.get(m, m) for m in self.selected_modules]
        destination = self.get_destination_channel()
        if destination is not None:
            channel_text = destination.mention
        else:
            channel_text = "Current channel"
        return (
            f"Dashboard configuration for `{self.selected_clan}`\n"
            f"• Modules: {', '.join(module_labels)}\n"
            f"• Format: {self.selected_format.upper()}\n"
            f"• Destination: {channel_text}\n\n"
            "Adjust the dropdowns below, then press **Preview** or **Post Dashboard**."
        )

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(content="Dashboard view expired. Run the command again to continue.", view=None)
            except discord.HTTPException:
                pass


class UpgradeAccountSelect(discord.ui.Select):
    """Select menu for choosing which linked account to use."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = []
        for account in parent_view.accounts:
            tag = account["tag"]
            alias = account.get("alias")
            label = alias if alias else tag
            description = tag if alias else None
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=tag,
                    description=description,
                    default=(tag == parent_view.selected_account_tag),
                )
            )
        super().__init__(
            placeholder="Select a linked account",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        selected_tag = self.values[0]
        self.parent_view.set_account(selected_tag)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class UpgradeClanSelect(discord.ui.Select):
    """Select menu for optionally associating the upgrade with a clan."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="No clan (skip)",
                value="__none__",
                default=parent_view.selected_clan is None,
            )
        ]
        for name in sorted(parent_view.clan_map.keys(), key=str.casefold):
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name,
                    default=name == parent_view.selected_clan,
                )
            )
        super().__init__(
            placeholder="Associate a clan (optional)",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        value = self.values[0]
        self.parent_view.set_clan(None if value == "__none__" else value)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class UpgradeDetailsButton(discord.ui.Button):
    """Button that opens a modal to collect upgrade details."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        super().__init__(label="Enter Upgrade Details", style=discord.ButtonStyle.primary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.send_modal(UpgradeDetailsModal(self.parent_view))


class PlanUpgradeSubmitButton(discord.ui.Button):
    """Submit button that finalises the upgrade."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        super().__init__(label="Submit Upgrade", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_submit(interaction)


class PlanUpgradeCancelButton(discord.ui.Button):
    """Button that cancels the planning session."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_cancel(interaction)


class UpgradeDetailsModal(discord.ui.Modal):
    """Modal dialog for collecting the upgrade specifics."""

    def __init__(self, parent_view: "PlanUpgradeView"):
        super().__init__(title="Upgrade Details", timeout=None)
        self.parent_view = parent_view

        self.building = discord.ui.TextInput(
            label="Building or upgrade name",
            placeholder="e.g. Archer Tower",
            default=parent_view.building_name or "",
            max_length=80,
        )
        self.add_item(self.building)

        self.from_level = discord.ui.TextInput(
            label="Current level",
            placeholder="e.g. 12",
            default=str(parent_view.current_level) if parent_view.current_level is not None else "",
            max_length=6,
        )
        self.add_item(self.from_level)

        self.to_level = discord.ui.TextInput(
            label="Target level",
            placeholder="e.g. 13",
            default=str(parent_view.target_level) if parent_view.target_level is not None else "",
            max_length=6,
        )
        self.add_item(self.to_level)

        self.duration = discord.ui.TextInput(
            label="Upgrade duration",
            placeholder="Examples: 2d 6h, 20h, 12:30",
            default=parent_view.duration_text or "",
            max_length=40,
        )
        self.add_item(self.duration)

        self.notes = discord.ui.TextInput(
            label="Notes (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            default=parent_view.notes or "",
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        building_name = self.building.value.strip()
        if not building_name:
            await interaction.response.send_message(
                "Please provide the building or upgrade name.",
                ephemeral=True,
            )
            return

        try:
            current_level = int(self.from_level.value.strip())
        except (ValueError, AttributeError):
            await interaction.response.send_message(
                "Current level must be a whole number.",
                ephemeral=True,
            )
            return

        try:
            target_level = int(self.to_level.value.strip())
        except (ValueError, AttributeError):
            await interaction.response.send_message(
                "Target level must be a whole number.",
                ephemeral=True,
            )
            return

        if target_level < current_level:
            await interaction.response.send_message(
                "Target level must be greater than or equal to the current level.",
                ephemeral=True,
            )
            return

        duration_input = self.duration.value.strip()
        duration_td = _parse_upgrade_duration(duration_input)
        if duration_td is None:
            await interaction.response.send_message(
                "I couldn't understand that duration. Try formats like `2d 6h`, `20h`, or `12:30`.",
                ephemeral=True,
            )
            return

        notes_input = self.notes.value.strip() if self.notes.value else ""
        notes_value = notes_input if notes_input else None

        self.parent_view.set_details(
            building=building_name,
            current_level=current_level,
            target_level=target_level,
            duration_text=duration_input,
            duration=duration_td,
            notes=notes_value,
        )

        await interaction.response.send_message("Upgrade details updated.", ephemeral=True)
        await self.parent_view.refresh_view_message()


class PlanUpgradeView(discord.ui.View):
    """Interactive view that guides the user through logging an upgrade."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        accounts: List[Dict[str, Optional[str]]],
        destination_channel: discord.TextChannel,
        clan_map: Dict[str, str],
        selected_clan: Optional[str],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.member = member
        self.accounts = accounts
        self.destination_channel = destination_channel
        self.clan_map = clan_map
        self.account_lookup = {entry["tag"]: entry.get("alias") for entry in accounts}
        first_account = accounts[0]
        self.selected_account_tag = first_account["tag"]
        self.selected_account_alias = first_account.get("alias")
        self.selected_clan = selected_clan if selected_clan in clan_map else None
        self.building_name: Optional[str] = None
        self.current_level: Optional[int] = None
        self.target_level: Optional[int] = None
        self.duration_text: Optional[str] = None
        self.duration_timedelta: Optional[timedelta] = None
        self.notes: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.refresh_components()

    def set_account(self, tag: str) -> None:
        if tag not in self.account_lookup:
            return
        self.selected_account_tag = tag
        self.selected_account_alias = self.account_lookup.get(tag)
        self.refresh_components()

    def set_clan(self, clan_name: Optional[str]) -> None:
        if clan_name is not None and clan_name not in self.clan_map:
            return
        self.selected_clan = clan_name
        self.refresh_components()

    def set_details(
        self,
        *,
        building: str,
        current_level: int,
        target_level: int,
        duration_text: str,
        duration: timedelta,
        notes: Optional[str],
    ) -> None:
        self.building_name = building
        self.current_level = current_level
        self.target_level = target_level
        self.duration_text = duration_text
        self.duration_timedelta = duration
        self.notes = notes

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(UpgradeAccountSelect(self))
        if self.clan_map:
            self.add_item(UpgradeClanSelect(self))
        self.add_item(UpgradeDetailsButton(self))
        self.add_item(PlanUpgradeSubmitButton(self))
        self.add_item(PlanUpgradeCancelButton(self))

    def render_message(self) -> str:
        account_display = self._format_account_display()
        clan_display = self.selected_clan or "None selected"
        clan_line = (
            "Clan: No clans configured" if not self.clan_map else f"Clan: {clan_display}"
        )

        if self.building_name and self.current_level is not None and self.target_level is not None:
            upgrade_line = f"{self.building_name} {self.current_level} -> {self.target_level}"
        elif self.building_name:
            upgrade_line = f"{self.building_name} (levels not set)"
        else:
            upgrade_line = "Not set"

        if self.duration_timedelta is not None and self.duration_text:
            preview_eta = datetime.utcnow().replace(tzinfo=timezone.utc) + self.duration_timedelta
            duration_line = f"{self.duration_text} (completes {_format_eta(preview_eta)})"
        else:
            duration_line = "Not set"

        notes_line = self.notes if self.notes else "No additional notes."
        destination_line = f"Destination: {self.destination_channel.mention}"

        return "\n".join(
            [
                "**Planned Upgrade Draft**",
                f"Account: {account_display}",
                clan_line,
                f"Upgrade: {upgrade_line}",
                f"Duration: {duration_line}",
                f"Notes: {notes_line}",
                destination_line,
                "",
                "Use the controls below to update the details, then press **Submit Upgrade** when you're ready.",
            ]
        )

    async def refresh_view_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh plan_upgrade view message: %s", exc)

    async def handle_submit(self, interaction: discord.Interaction) -> None:
        if self.building_name is None or self.current_level is None or self.target_level is None:
            await interaction.response.send_message(
                "Provide the upgrade details before submitting.",
                ephemeral=True,
            )
            return
        if self.duration_timedelta is None or not self.duration_text:
            await interaction.response.send_message(
                "Set the upgrade duration before submitting.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if self.destination_channel is None:
            await interaction.followup.send(
                "The upgrade channel is no longer available. Ask an administrator to reconfigure it.",
                ephemeral=True,
            )
            return

        tag = self.selected_account_tag
        alias = self.selected_account_alias
        account_label = f"{alias} ({tag})" if alias else tag

        resolved_clan_name = self.selected_clan
        resolved_clan_tag = self.clan_map.get(resolved_clan_name) if resolved_clan_name else None

        player_name: Optional[str] = None
        try:
            player_payload = await client.get_player(tag)
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("plan_upgrade unable to fetch player payload for %s: %s", tag, exc)
            player_payload = None

        if isinstance(player_payload, dict):
            profile = player_payload.get("profile", {})
            player_name = profile.get("name")
            clan_info = player_payload.get("clan") or {}
            player_clan_tag = clan_info.get("tag")
            if player_clan_tag:
                if resolved_clan_tag is None:
                    resolved_clan_tag = player_clan_tag
                if resolved_clan_name is None:
                    for configured_name, configured_tag in self.clan_map.items():
                        if configured_tag == player_clan_tag:
                            resolved_clan_name = configured_name
                            break

        submission_dt = datetime.utcnow().replace(tzinfo=timezone.utc)
        submission_time = submission_dt.strftime("%Y-%m-%d %H:%M UTC")
        completion_dt = submission_dt + self.duration_timedelta
        completion_text = _format_eta(completion_dt)

        lines = [
            "**Planned Upgrade Submitted**",
            f"Member: {self.member.mention}",
            f"Account: `{account_label}`",
            f"Upgrade: {self.building_name} {self.current_level} -> {self.target_level}",
            f"Duration: {self.duration_text}",
            f"Completes: {completion_text}",
        ]
        if player_name:
            lines.append(f"In-game name: {player_name}")
        if resolved_clan_name:
            lines.append(f"Clan: `{resolved_clan_name}`")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        lines.append(f"Submitted: {submission_time}")

        payload = "\n".join(lines)
        for chunk in _chunk_content(payload):
            await self.destination_channel.send(chunk)

        log_entry = {
            "id": str(uuid4()),
            "timestamp": submission_dt.isoformat(),
            "user_id": self.member.id,
            "user_name": self.member.display_name,
            "player_tag": tag,
            "player_name": player_name,
            "alias": alias,
            "upgrade": self.building_name,
            "notes": self.notes,
            "clan_name": resolved_clan_name,
            "clan_tag": resolved_clan_tag,
            "from_level": self.current_level,
            "to_level": self.target_level,
            "duration": self.duration_text,
            "duration_seconds": int(self.duration_timedelta.total_seconds()),
            "eta": completion_dt.isoformat(),
        }
        _append_upgrade_log(self.guild.id, log_entry)

        await interaction.followup.send(
            f"Logged upgrade for `{account_label}` in {self.destination_channel.mention}.",
            ephemeral=True,
        )

        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=f"Upgrade submitted for `{account_label}` in {self.destination_channel.mention}.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.response.edit_message(
            content="Upgrade planning cancelled.",
            view=self,
        )

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Upgrade planning timed out. Run `/plan_upgrade` again to restart.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    def _format_account_display(self) -> str:
        alias = self.selected_account_alias
        tag = self.selected_account_tag
        return f"{alias} ({tag})" if alias else tag


class ScheduleSelect(discord.ui.Select):
    """Select existing schedules or start a new one."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="➕ Create new schedule",
                value="__new__",
                description="Start a brand new schedule",
                default=parent_view.selected_schedule_id is None,
            )
        ]
        for entry in parent_view.schedule_summaries():
            options.append(entry)
        super().__init__(
            placeholder="Select an existing schedule or create a new one",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        choice = self.values[0]
        if choice == "__new__":
            self.parent_view.start_new_schedule()
        else:
            self.parent_view.load_schedule(choice)
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleClanSelect(discord.ui.Select):
    """Select menu for choosing the clan the schedule belongs to."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name == parent_view.selected_clan,
            )
            for name in sorted(parent_view.clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Choose the clan to report on",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        clan_name = self.values[0]
        self.parent_view.set_clan(clan_name)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleReportTypeSelect(discord.ui.Select):
    """Select menu for choosing the report type."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label="Dashboard",
                value="dashboard",
                description="Post the configured dashboard summary",
                default=parent_view.report_type == "dashboard",
            ),
            discord.SelectOption(
                label="Donation summary",
                value="donation_summary",
                description="Share donation stats",
                default=parent_view.report_type == "donation_summary",
            ),
            discord.SelectOption(
                label="Season summary",
                value="season_summary",
                description="Send season recap with optional sections",
                default=parent_view.report_type == "season_summary",
            ),
        ]
        super().__init__(
            placeholder="Select report type",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_report_type(self.values[0])
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleFrequencySelect(discord.ui.Select):
    """Select menu for choosing schedule cadence."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label="Daily",
                value="daily",
                default=parent_view.frequency == "daily",
            ),
            discord.SelectOption(
                label="Weekly",
                value="weekly",
                default=parent_view.frequency == "weekly",
            ),
        ]
        super().__init__(
            placeholder="Choose how often to run",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_frequency(self.values[0])
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleWeekdaySelect(discord.ui.Select):
    """Select menu for weekly schedule weekday."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=day.title(),
                value=day,
                default=day == parent_view.weekday,
            )
            for day in WEEKDAY_CHOICES
        ]
        super().__init__(
            placeholder="Pick the weekday",
            min_values=1,
            max_values=1,
            options=options,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_weekday(self.values[0])
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleTimeButton(discord.ui.Button):
    """Button that opens a modal to set the UTC time."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(
            label=f"Time (UTC): {parent_view.time_utc}",
            style=discord.ButtonStyle.primary,
            row=5,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.send_modal(ScheduleTimeModal(self.parent_view))


class ScheduleChannelSelect(discord.ui.ChannelSelect):
    """Channel selector for optional override."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        default_values = []
        if parent_view.channel_id is not None:
            channel = parent_view.guild.get_channel(parent_view.channel_id)
            if isinstance(channel, discord.TextChannel):
                default_values = [channel]
        super().__init__(
            placeholder="Override destination channel (optional)",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            default_values=default_values,
            row=6,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel = self.values[0] if self.values else None
        self.parent_view.set_channel(channel)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleDashboardModuleSelect(discord.ui.Select):
    """Multi-select for dashboard modules."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=DASHBOARD_MODULES[module],
                value=module,
                default=module in parent_view.dashboard_modules,
            )
            for module in DASHBOARD_MODULES
        ]
        super().__init__(
            placeholder="Choose dashboard modules",
            min_values=1,
            max_values=len(options),
            options=options,
            row=7,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_dashboard_modules(list(self.values))
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleDashboardFormatSelect(discord.ui.Select):
    """Select menu for dashboard format."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=fmt.upper(),
                value=fmt,
                default=fmt == parent_view.dashboard_format,
            )
            for fmt in sorted(DASHBOARD_FORMATS)
        ]
        super().__init__(
            placeholder="Select dashboard format",
            min_values=1,
            max_values=1,
            options=options,
            row=8,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_dashboard_format(self.values[0])
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class ScheduleToggleButton(discord.ui.Button):
    """Button that toggles a boolean option."""

    def __init__(self, parent_view: "ScheduleConfigView", attr: str, label: str):
        self.parent_view = parent_view
        self.attr = attr
        enabled = getattr(parent_view, attr)
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        text = f"{label}: {'On' if enabled else 'Off'}"
        super().__init__(label=text, style=style, row=9)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        current = getattr(self.parent_view, self.attr)
        setattr(self.parent_view, self.attr, not current)
        self.parent_view.unsaved_changes = True
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SchedulePreviewButton(discord.ui.Button):
    """Button that displays a summary preview."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(label="Preview Summary", style=discord.ButtonStyle.secondary, row=10)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(self.parent_view.preview_text(), ephemeral=True)


class ScheduleSaveButton(discord.ui.Button):
    """Button that saves the schedule configuration."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(label="Save Schedule", style=discord.ButtonStyle.success, row=10)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_save(interaction)


class ScheduleDeleteButton(discord.ui.Button):
    """Button that removes the currently selected schedule."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(label="Delete Schedule", style=discord.ButtonStyle.danger, row=10)
        self.parent_view = parent_view
        if not parent_view.can_delete_current_schedule:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_delete(interaction)


class ScheduleCancelButton(discord.ui.Button):
    """Button that closes the scheduler view."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=11)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="Scheduler closed.",
            view=self.parent_view,
        )


class ScheduleTimeModal(discord.ui.Modal):
    """Modal used to collect a schedule time."""

    def __init__(self, parent_view: "ScheduleConfigView"):
        super().__init__(title="Set Report Time (UTC)", timeout=None)
        self.parent_view = parent_view
        self.time_input = discord.ui.TextInput(
            label="Time (HH:MM UTC)",
            placeholder="e.g. 13:30",
            default=parent_view.time_utc,
            max_length=8,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.time_input.value.strip()
        try:
            _parse_time_utc(value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.parent_view.set_time(value)
        await interaction.response.send_message(f"Time set to {value} UTC.", ephemeral=True)
        self.parent_view.refresh_components()
        await self.parent_view.refresh_view_message()


class ScheduleConfigView(discord.ui.View):
    """Interactive view for configuring scheduled reports."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        clan_map: Dict[str, str],
        schedules: List[Dict[str, Any]],
        selected_schedule_id: Optional[str],
        selected_clan: Optional[str],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_map = clan_map
        self.message: Optional[discord.Message] = None

        self.schedule_map: Dict[str, Dict[str, Any]] = {
            entry.get("id"): entry for entry in schedules if isinstance(entry, dict) and entry.get("id")
        }
        self.selected_schedule_id = selected_schedule_id if selected_schedule_id in self.schedule_map else None

        self.selected_clan = selected_clan if selected_clan in clan_map else next(iter(clan_map))
        self.report_type: str = "dashboard"
        self.frequency: str = "daily"
        self.weekday: Optional[str] = None
        self.time_utc: str = "00:00"
        self.channel_id: Optional[int] = None
        self.dashboard_modules: List[str] = ["war_overview"]
        self.dashboard_format: str = "embed"
        self.include_donations: bool = True
        self.include_wars: bool = True
        self.include_members: bool = False
        self.unsaved_changes: bool = False

        if self.selected_schedule_id:
            self.load_schedule(self.selected_schedule_id)
        else:
            self.start_new_schedule()

        self.refresh_components()

    def schedule_summaries(self) -> List[discord.SelectOption]:
        summaries: List[discord.SelectOption] = []
        for schedule_id, entry in self.schedule_map.items():
            clan = entry.get("clan_name", "unknown")
            report_type = entry.get("type", "unknown")
            frequency = entry.get("frequency", "daily")
            label = f"{clan} - {report_type}"
            description = f"{frequency.title()} at {entry.get('time_utc', '??:??')} (ID {schedule_id})"
            summaries.append(
                discord.SelectOption(
                    label=label[:100],
                    value=schedule_id,
                    description=description[:100],
                    default=schedule_id == self.selected_schedule_id,
                )
            )
        return summaries

    def set_clan(self, clan_name: str) -> None:
        if clan_name not in self.clan_map or clan_name == self.selected_clan:
            return
        self.selected_clan = clan_name
        if self.report_type == "dashboard":
            self.apply_dashboard_defaults()
        self.unsaved_changes = True
        self.refresh_components()

    def set_report_type(self, report_type: str) -> None:
        if report_type not in REPORT_TYPES or report_type == self.report_type:
            return
        self.report_type = report_type
        if report_type == "dashboard":
            self.apply_dashboard_defaults()
        if report_type != "season_summary":
            self.include_donations = True
            self.include_wars = True
            self.include_members = False
        self.unsaved_changes = True
        self.refresh_components()

    def set_frequency(self, frequency: str) -> None:
        if frequency not in SCHEDULE_FREQUENCIES or frequency == self.frequency:
            return
        self.frequency = frequency
        if frequency == "weekly" and self.weekday is None:
            self.weekday = "monday"
        if frequency == "daily":
            self.weekday = None
        self.unsaved_changes = True
        self.refresh_components()

    def set_weekday(self, weekday: str) -> None:
        if weekday not in WEEKDAY_CHOICES:
            return
        self.weekday = weekday
        self.unsaved_changes = True

    def set_time(self, time_utc: str) -> None:
        self.time_utc = time_utc
        self.unsaved_changes = True

    def set_channel(self, channel: Optional[discord.abc.GuildChannel]) -> None:
        if isinstance(channel, discord.TextChannel):
            self.channel_id = channel.id
        else:
            self.channel_id = None
        self.unsaved_changes = True

    def set_dashboard_modules(self, modules: Iterable[str]) -> None:
        cleaned = _sanitise_modules(modules)
        if cleaned:
            self.dashboard_modules = cleaned
            self.unsaved_changes = True

    def set_dashboard_format(self, fmt: str) -> None:
        if fmt in DASHBOARD_FORMATS:
            self.dashboard_format = fmt
            self.unsaved_changes = True

    def apply_dashboard_defaults(self) -> None:
        clan_entry = _get_clan_entry(self.guild.id, self.selected_clan)
        modules, fmt, default_channel_id = _dashboard_defaults(clan_entry if isinstance(clan_entry, dict) else {})
        self.dashboard_modules = modules
        self.dashboard_format = fmt
        if self.channel_id is None and isinstance(default_channel_id, int):
            self.channel_id = default_channel_id

    def load_schedule(self, schedule_id: str) -> None:
        entry = self.schedule_map.get(schedule_id)
        if entry is None:
            return
        self.selected_schedule_id = schedule_id
        self.selected_clan = entry.get("clan_name", self.selected_clan)
        self.report_type = entry.get("type", "dashboard")
        self.frequency = entry.get("frequency", "daily")
        self.time_utc = entry.get("time_utc", "00:00")
        self.weekday = entry.get("weekday")
        self.channel_id = entry.get("channel_id")
        options = entry.get("options", {})
        if self.report_type == "dashboard":
            modules = options.get("modules")
            if isinstance(modules, list):
                self.dashboard_modules = _sanitise_modules(modules)
            self.dashboard_format = options.get("format", "embed")
        else:
            self.dashboard_modules = ["war_overview"]
            self.dashboard_format = "embed"
        if self.report_type == "season_summary":
            self.include_donations = options.get("include_donations", True)
            self.include_wars = options.get("include_wars", True)
            self.include_members = options.get("include_members", False)
        else:
            self.include_donations = True
            self.include_wars = True
            self.include_members = False
        self.unsaved_changes = False

    def start_new_schedule(self) -> None:
        self.selected_schedule_id = None
        self.report_type = "dashboard"
        self.frequency = "daily"
        self.weekday = None
        self.time_utc = "00:00"
        self.channel_id = None
        self.apply_dashboard_defaults()
        self.include_donations = True
        self.include_wars = True
        self.include_members = False
        self.unsaved_changes = True

    @property
    def can_delete_current_schedule(self) -> bool:
        return self.selected_schedule_id in self.schedule_map

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(ScheduleSelect(self))
        self.add_item(ScheduleClanSelect(self))
        self.add_item(ScheduleReportTypeSelect(self))
        self.add_item(ScheduleFrequencySelect(self))
        if self.frequency == "weekly":
            self.add_item(ScheduleWeekdaySelect(self))
        self.add_item(ScheduleTimeButton(self))
        self.add_item(ScheduleChannelSelect(self))
        if self.report_type == "dashboard":
            self.add_item(ScheduleDashboardModuleSelect(self))
            self.add_item(ScheduleDashboardFormatSelect(self))
        elif self.report_type == "season_summary":
            self.add_item(ScheduleToggleButton(self, "include_donations", "Include donations"))
            self.add_item(ScheduleToggleButton(self, "include_wars", "Include wars"))
            self.add_item(ScheduleToggleButton(self, "include_members", "Include members"))
        self.add_item(SchedulePreviewButton(self))
        self.add_item(ScheduleSaveButton(self))
        self.add_item(ScheduleDeleteButton(self))
        self.add_item(ScheduleCancelButton(self))

    def render_message(self) -> str:
        channel_display = "Default channel"
        if self.channel_id is not None:
            channel_obj = self.guild.get_channel(self.channel_id)
            if isinstance(channel_obj, discord.TextChannel):
                channel_display = channel_obj.mention
        weekday_text = self.weekday.title() if self.weekday else "N/A"
        status = "Unsaved changes" if self.unsaved_changes else "All changes saved"
        try:
            next_run_preview = _calculate_next_run(
                self.frequency,
                self.time_utc,
                weekday=self.weekday if self.frequency == "weekly" else None,
            )
        except Exception:
            next_run_preview = "⚠️ invalid time"

        lines = [
            "**Schedule Report Editor**",
            f"Schedule ID: `{self.selected_schedule_id or '(new)'}`",
            f"Clan: `{self.selected_clan}`",
            f"Report type: {self.report_type}",
            f"Frequency: {self.frequency}",
            f"Weekday: {weekday_text}",
            f"Time (UTC): {self.time_utc}",
            f"Channel: {channel_display}",
            f"Next run preview: {next_run_preview}",
            f"Status: {status}",
        ]
        if self.report_type == "dashboard":
            lines.append(f"Dashboard modules: {', '.join(self.dashboard_modules)}")
            lines.append(f"Dashboard format: {self.dashboard_format.upper()}")
        if self.report_type == "season_summary":
            lines.append(
                "Sections: "
                f"{'Donations' if self.include_donations else ''} "
                f"{'Wars' if self.include_wars else ''} "
                f"{'Members' if self.include_members else ''}".strip() or "None"
            )
        lines.append("")
        lines.append("Use the controls below to adjust settings, then press **Save Schedule**.")
        return "\n".join(lines)

    def preview_text(self) -> str:
        return self.render_message()

    async def refresh_view_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh schedule view message: %s", exc)

    async def handle_save(self, interaction: discord.Interaction) -> None:
        if self.frequency == "weekly" and not self.weekday:
            await interaction.response.send_message(
                "Select a weekday for weekly schedules.",
                ephemeral=True,
            )
            return
        try:
            next_run = _calculate_next_run(
                self.frequency,
                self.time_utc,
                weekday=self.weekday if self.frequency == "weekly" else None,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        guild_config = _ensure_guild_config(self.guild.id)
        schedules = guild_config.setdefault("schedules", [])

        options: Dict[str, Any] = {}
        if self.report_type == "dashboard":
            options["modules"] = self.dashboard_modules
            options["format"] = self.dashboard_format
        elif self.report_type == "season_summary":
            options["include_donations"] = self.include_donations
            options["include_wars"] = self.include_wars
            options["include_members"] = self.include_members

        if self.selected_schedule_id and self.selected_schedule_id in self.schedule_map:
            entry = self.schedule_map[self.selected_schedule_id]
            entry.update(
                {
                    "type": self.report_type,
                    "clan_name": self.selected_clan,
                    "frequency": self.frequency,
                    "time_utc": self.time_utc,
                    "weekday": self.weekday if self.frequency == "weekly" else None,
                    "channel_id": self.channel_id,
                    "next_run": next_run,
                    "options": options,
                }
            )
            for idx, schedule in enumerate(schedules):
                if schedule.get("id") == self.selected_schedule_id:
                    schedules[idx] = entry
                    break
        else:
            new_id = str(uuid4())
            entry = {
                "id": new_id,
                "type": self.report_type,
                "clan_name": self.selected_clan,
                "frequency": self.frequency,
                "time_utc": self.time_utc,
                "weekday": self.weekday if self.frequency == "weekly" else None,
                "channel_id": self.channel_id,
                "next_run": next_run,
                "options": options,
            }
            schedules.append(entry)
            self.schedule_map[new_id] = entry
            self.selected_schedule_id = new_id

        save_server_config()
        self.unsaved_changes = False
        self.refresh_components()
        await interaction.response.send_message(
            f"Schedule saved. Next run at {next_run}.",
            ephemeral=True,
        )
        await self.refresh_view_message()

    async def handle_delete(self, interaction: discord.Interaction) -> None:
        if not self.selected_schedule_id or self.selected_schedule_id not in self.schedule_map:
            await interaction.response.send_message(
                "Select an existing schedule before deleting.",
                ephemeral=True,
            )
            return

        schedule_id = self.selected_schedule_id
        guild_config = _ensure_guild_config(self.guild.id)
        schedules = guild_config.get("schedules", [])
        guild_config["schedules"] = [
            entry for entry in schedules if entry.get("id") != schedule_id
        ]
        self.schedule_map.pop(schedule_id, None)
        save_server_config()

        if self.schedule_map:
            self.selected_schedule_id = next(iter(self.schedule_map.keys()))
            self.load_schedule(self.selected_schedule_id)
        else:
            self.start_new_schedule()

        self.refresh_components()
        await interaction.response.send_message(
            f"Deleted schedule `{schedule_id}`.",
            ephemeral=True,
        )
        await self.refresh_view_message()

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Scheduler timed out. Run `/schedule_report` again to continue.",
                    view=self,
                )
            except discord.HTTPException:
                pass

class WarPlanNameModal(discord.ui.Modal):
    """Modal used to create or rename a war plan."""

    def __init__(self, parent_view: "WarPlanView", *, initial_name: Optional[str], mode: str):
        title = "Rename War Plan" if mode == "rename" else "Create War Plan"
        super().__init__(title=title, timeout=None)
        self.parent_view = parent_view
        self.mode = mode
        self.plan_name = discord.ui.TextInput(
            label="Plan name",
            placeholder="e.g. TH15 Mass E-Drags",
            default=initial_name or "",
            max_length=80,
        )
        self.add_item(self.plan_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = self.plan_name.value.strip()
        if not new_name:
            await interaction.response.send_message(
                "Plan name cannot be empty.",
                ephemeral=True,
            )
            return

        if self.parent_view.plan_name_conflicts(new_name):
            await interaction.response.send_message(
                f"A plan named `{new_name}` already exists. Choose another name or select it from the dropdown.",
                ephemeral=True,
            )
            return

        self.parent_view.set_plan_name(new_name)
        await interaction.response.send_message(
            f"Plan name set to `{new_name}`.",
            ephemeral=True,
        )
        await self.parent_view.refresh_view_message()


class WarPlanContentModal(discord.ui.Modal):
    """Modal used to edit the war plan content."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(title="Edit War Plan Content", timeout=None)
        self.parent_view = parent_view
        current = parent_view.plan_content or ""
        self.content = discord.ui.TextInput(
            label="Strategy content",
            style=discord.TextStyle.paragraph,
            default=current,
            placeholder="Describe the strategy, assignments, spell composition, etc.",
            max_length=3800,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_content = self.content.value.strip()
        if not new_content:
            await interaction.response.send_message(
                "Plan content cannot be empty.",
                ephemeral=True,
            )
            return
        self.parent_view.set_plan_content(new_content)
        await interaction.response.send_message("Plan content updated.", ephemeral=True)
        await self.parent_view.refresh_view_message()


class WarPlanClanSelect(discord.ui.Select):
    """Select menu for choosing which clan's plans to manage."""

    def __init__(self, parent_view: "WarPlanView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name == parent_view.selected_clan,
            )
            for name in sorted(parent_view.clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Choose a clan",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        selected = self.values[0]
        self.parent_view.set_clan(selected)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarPlanPlanSelect(discord.ui.Select):
    """Select menu for choosing an existing plan or creating a new one."""

    def __init__(self, parent_view: "WarPlanView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="➕ Create new plan",
                value="__new__",
                description="Start a brand new war plan",
                default=parent_view.selected_plan_name is None,
            )
        ]
        for name in sorted(parent_view.available_plan_names, key=str.casefold):
            options.append(
                discord.SelectOption(
                    label=name,
                    value=name,
                    default=name == parent_view.selected_plan_name,
                )
            )
        super().__init__(
            placeholder="Select an existing plan or create a new one",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        selection = self.values[0]
        if selection == "__new__":
            self.parent_view.start_new_plan()
            if interaction.message is not None:
                self.parent_view.message = interaction.message
            await interaction.response.send_modal(
                WarPlanNameModal(self.parent_view, initial_name=None, mode="create")
            )
            return

        self.parent_view.load_plan(selection)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarPlanSetNameButton(discord.ui.Button):
    """Button that opens the modal to set or rename the plan."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Set Plan Name", style=discord.ButtonStyle.secondary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        initial = self.parent_view.selected_plan_name
        mode = "rename" if initial else "create"
        await interaction.response.send_modal(
            WarPlanNameModal(self.parent_view, initial_name=initial, mode=mode)
        )


class WarPlanEditContentButton(discord.ui.Button):
    """Button that opens the modal to edit plan content."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Edit Content", style=discord.ButtonStyle.primary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.send_modal(WarPlanContentModal(self.parent_view))


class WarPlanPreviewButton(discord.ui.Button):
    """Button that shows a preview of the current plan."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.plan_content:
            await interaction.response.send_message(
                "Add plan content before requesting a preview.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            self.parent_view.plan_preview_text(),
            ephemeral=True,
        )


class WarPlanSaveButton(discord.ui.Button):
    """Button that persists the current plan."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Save Plan", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_save(interaction)


class WarPlanDeleteButton(discord.ui.Button):
    """Button that deletes the currently selected plan."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Delete Plan", style=discord.ButtonStyle.danger, row=3)
        self.parent_view = parent_view
        if not parent_view.can_delete_current_plan:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_delete(interaction)


class WarPlanCancelButton(discord.ui.Button):
    """Button that closes the editor."""

    def __init__(self, parent_view: "WarPlanView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="War plan editor closed.",
            view=self.parent_view,
        )


class WarPlanView(discord.ui.View):
    """Interactive editor for creating or updating war plan templates."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        clan_map: Dict[str, str],
        selected_clan: str,
        preselected_plan: Optional[str],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_map = clan_map
        self.message: Optional[discord.Message] = None

        self.selected_clan = selected_clan
        self.plan_store: Dict[str, Dict[str, Any]] = {}
        self.available_plan_names: List[str] = []

        self.selected_plan_name: Optional[str] = None
        self.original_plan_name: Optional[str] = None
        self.plan_content: Optional[str] = None
        self.last_updated_at: Optional[str] = None
        self.last_updated_by: Optional[int] = None
        self.unsaved_changes = False

        self.load_clan(self.selected_clan)
        if preselected_plan and preselected_plan in self.plan_store:
            self.load_plan(preselected_plan)
        elif self.plan_store:
            default_plan = preselected_plan if preselected_plan in self.plan_store else next(iter(self.available_plan_names))
            self.load_plan(default_plan)
        else:
            if preselected_plan:
                self.set_plan_name(preselected_plan)
            else:
                self.start_new_plan()

        self.refresh_components()

    def load_clan(self, clan_name: str) -> None:
        clan_entry = _get_clan_entry(self.guild.id, clan_name)
        if clan_entry is None:
            clan_entry = _ensure_guild_config(self.guild.id)["clans"].setdefault(clan_name, {})
        self.plan_store = clan_entry.setdefault("war_plans", {})
        self.available_plan_names = list(self.plan_store.keys())

    def set_clan(self, clan_name: str) -> None:
        if clan_name not in self.clan_map or clan_name == self.selected_clan:
            return
        self.selected_clan = clan_name
        self.load_clan(clan_name)
        if self.available_plan_names:
            self.load_plan(self.available_plan_names[0])
        else:
            self.start_new_plan()
        self.refresh_components()

    def load_plan(self, plan_name: str) -> None:
        plan = self.plan_store.get(plan_name)
        if plan is None:
            return
        self.selected_plan_name = plan_name
        self.original_plan_name = plan_name
        self.plan_content = plan.get("content", "")
        self.last_updated_at = plan.get("updated_at")
        self.last_updated_by = plan.get("updated_by")
        self.unsaved_changes = False
        self.refresh_components()

    def start_new_plan(self) -> None:
        self.selected_plan_name = None
        self.original_plan_name = None
        self.plan_content = ""
        self.last_updated_at = None
        self.last_updated_by = None
        self.unsaved_changes = True
        self.refresh_components()

    def set_plan_name(self, name: str) -> None:
        self.selected_plan_name = name
        self.unsaved_changes = True
        self.refresh_components()

    def plan_name_conflicts(self, candidate: str) -> bool:
        candidate_lower = candidate.casefold()
        for existing in self.available_plan_names:
            if existing.casefold() == candidate_lower and existing != self.original_plan_name:
                return True
        return False

    def set_plan_content(self, content: str) -> None:
        self.plan_content = content
        self.unsaved_changes = True

    @property
    def can_delete_current_plan(self) -> bool:
        return self.selected_plan_name in self.plan_store

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(WarPlanClanSelect(self))
        self.add_item(WarPlanPlanSelect(self))
        self.add_item(WarPlanSetNameButton(self))
        self.add_item(WarPlanEditContentButton(self))
        self.add_item(WarPlanPreviewButton(self))
        self.add_item(WarPlanSaveButton(self))
        self.add_item(WarPlanDeleteButton(self))
        self.add_item(WarPlanCancelButton(self))

    def render_message(self) -> str:
        plan_name = self.selected_plan_name or "(unsaved plan)"
        content_status = (
            f"{len(self.plan_content or ''):,} characters"
            if self.plan_content
            else "No content yet"
        )
        updated_line = "Not saved yet"
        if self.original_plan_name and self.original_plan_name in self.plan_store:
            timestamps = self.plan_store[self.original_plan_name].get("updated_at")
            if timestamps:
                updated_line = timestamps

        status = "Unsaved changes" if self.unsaved_changes else "All changes saved"
        return "\n".join(
            [
                "**War Plan Editor**",
                f"Clan: `{self.selected_clan}`",
                f"Plan: `{plan_name}`",
                f"Content: {content_status}",
                f"Last saved: {updated_line}",
                f"Status: {status}",
                "",
                "Use the dropdowns and buttons below to manage war plans. Remember to press **Save Plan** after editing.",
            ]
        )

    def plan_preview_text(self) -> str:
        plan_name = self.selected_plan_name or "(unsaved plan)"
        lines = [
            f"**{plan_name}**",
            "",
            self.plan_content or "(no content provided)",
        ]
        return "\n".join(lines)

    async def refresh_view_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh war plan view message: %s", exc)

    async def handle_save(self, interaction: discord.Interaction) -> None:
        if not self.selected_plan_name:
            await interaction.response.send_message(
                "Set a plan name before saving.",
                ephemeral=True,
            )
            return
        if not self.plan_content:
            await interaction.response.send_message(
                "Add plan content before saving.",
                ephemeral=True,
            )
            return

        plan_name = self.selected_plan_name
        if (
            self.original_plan_name
            and self.original_plan_name != plan_name
            and self.original_plan_name in self.plan_store
        ):
            del self.plan_store[self.original_plan_name]

        self.plan_store[plan_name] = {
            "content": self.plan_content,
            "updated_at": datetime.utcnow().isoformat(),
            "updated_by": self.actor.id,
        }
        save_server_config()

        self.available_plan_names = list(self.plan_store.keys())
        self.original_plan_name = plan_name
        self.unsaved_changes = False

        self.refresh_components()
        await interaction.response.send_message(
            f"Saved war plan `{plan_name}` for `{self.selected_clan}`.",
            ephemeral=True,
        )
        await self.refresh_view_message()

    async def handle_delete(self, interaction: discord.Interaction) -> None:
        if not self.selected_plan_name or self.selected_plan_name not in self.plan_store:
            await interaction.response.send_message(
                "Select an existing plan before attempting to delete.",
                ephemeral=True,
            )
            return

        deleted_name = self.selected_plan_name
        del self.plan_store[deleted_name]
        save_server_config()
        self.available_plan_names = list(self.plan_store.keys())

        if self.available_plan_names:
            self.load_plan(self.available_plan_names[0])
        else:
            self.start_new_plan()

        await interaction.response.send_message(
            f"Deleted war plan `{deleted_name}`.",
            ephemeral=True,
        )
        await self.refresh_view_message()

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="War plan editor timed out. Run `/save_war_plan` again to continue.",
                    view=self,
                )
            except discord.HTTPException:
                pass





class WarPlanPostClanSelect(discord.ui.Select):
    """Select menu for choosing which clan's plans to post."""

    def __init__(self, parent_view: "WarPlanPostView"):
        options = [
            discord.SelectOption(
                label=name[:100],
                value=name,
                default=name == parent_view.selected_clan,
            )
            for name in sorted(parent_view.clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Choose a clan",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        new_clan = self.values[0]
        self.parent_view.set_clan(new_clan)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarPlanPostPlanSelect(discord.ui.Select):
    """Select menu for picking a saved war plan."""

    def __init__(self, parent_view: "WarPlanPostView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = []
        for name in parent_view.available_plan_names:
            plan = parent_view.plan_store.get(name, {})
            updated_at = plan.get("updated_at") if isinstance(plan, dict) else None
            description = None
            if isinstance(updated_at, str) and updated_at:
                description = f"Updated {updated_at}"[:100]
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name,
                    description=description,
                    default=name == parent_view.selected_plan,
                )
            )
            if len(options) >= 25:
                break
        if not options:
            options = [
                discord.SelectOption(
                    label="No war plans found",
                    value="__none__",
                    description="Use /save_war_plan to create one.",
                    default=True,
                )
            ]
        super().__init__(
            placeholder="Choose a war plan",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )
        if not parent_view.available_plan_names:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        choice = self.values[0]
        self.parent_view.set_plan(choice)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarPlanPostChannelSelect(discord.ui.ChannelSelect):
    """Channel selector for delivering the war plan."""

    def __init__(self, parent_view: "WarPlanPostView"):
        self.parent_view = parent_view
        default_channel = parent_view.explicit_channel or parent_view.default_channel
        default_values = [default_channel] if isinstance(default_channel, discord.TextChannel) else []
        super().__init__(
            placeholder="Pick a channel (leave blank to use the current one)",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            default_values=default_values,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel = self.values[0] if self.values else None
        self.parent_view.set_channel(channel)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarPlanPostPreviewButton(discord.ui.Button):
    """Button that produces an ephemeral preview of the plan."""

    def __init__(self, parent_view: "WarPlanPostView"):
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view
        if not parent_view.can_preview:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_preview(interaction)


class WarPlanPostSendButton(discord.ui.Button):
    """Button that posts the plan to the chosen channel."""

    def __init__(self, parent_view: "WarPlanPostView"):
        super().__init__(label="Post Plan", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view
        if not parent_view.can_post:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_post(interaction)


class WarPlanPostCloseButton(discord.ui.Button):
    """Button that closes the posting view."""

    def __init__(self, parent_view: "WarPlanPostView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="War plan poster closed.",
            view=self.parent_view,
        )


class WarPlanPostView(discord.ui.View):
    """Interactive view used to select and post stored war plans."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        clan_map: Dict[str, str],
        selected_clan: str,
        preselected_plan: Optional[str],
        explicit_channel: Optional[discord.TextChannel],
        fallback_channel: Optional[discord.TextChannel],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_map = clan_map
        self.message: Optional[discord.Message] = None

        self.selected_clan = selected_clan
        self.plan_store: Dict[str, Dict[str, Any]] = {}
        self.available_plan_names: List[str] = []
        self.selected_plan: Optional[str] = None
        self.explicit_channel = explicit_channel
        self.default_channel = fallback_channel
        self.last_post_summary: Optional[str] = None

        self.load_clan(selected_clan)
        if preselected_plan and preselected_plan in self.available_plan_names:
            self.selected_plan = preselected_plan
        elif self.available_plan_names:
            self.selected_plan = self.available_plan_names[0]

        self.refresh_components()

    def load_clan(self, clan_name: str) -> None:
        clan_entry = _get_clan_entry(self.guild.id, clan_name)
        war_plans: Dict[str, Dict[str, Any]] = {}
        if isinstance(clan_entry, dict):
            stored = clan_entry.get("war_plans")
            if isinstance(stored, dict):
                war_plans = stored
        self.plan_store = war_plans
        self.available_plan_names = sorted(self.plan_store.keys(), key=str.casefold)

    def set_clan(self, clan_name: str) -> None:
        if clan_name == self.selected_clan or clan_name not in self.clan_map:
            return
        self.selected_clan = clan_name
        self.load_clan(clan_name)
        self.selected_plan = self.available_plan_names[0] if self.available_plan_names else None
        self.last_post_summary = None
        self.refresh_components()

    def set_plan(self, plan_name: str) -> None:
        if plan_name not in self.plan_store:
            return
        self.selected_plan = plan_name
        self.refresh_components()

    def set_channel(self, channel: Optional[discord.abc.GuildChannel]) -> None:
        self.explicit_channel = channel if isinstance(channel, discord.TextChannel) else None
        self.refresh_components()

    def get_plan_payload(self) -> Optional[Dict[str, Any]]:
        if self.selected_plan is None:
            return None
        payload = self.plan_store.get(self.selected_plan)
        return payload if isinstance(payload, dict) else None

    def get_destination(self) -> Optional[discord.TextChannel]:
        candidate = self.explicit_channel or self.default_channel
        if not isinstance(candidate, discord.TextChannel):
            return None
        me = self.guild.me
        if me is None:
            return None
        if not candidate.permissions_for(me).send_messages:
            return None
        return candidate

    def get_destination_label(self) -> str:
        candidate = self.explicit_channel or self.default_channel
        if isinstance(candidate, discord.TextChannel):
            me = self.guild.me
            if me is not None and candidate.permissions_for(me).send_messages:
                return candidate.mention
            return f"{candidate.mention} (missing permission)"
        if self.default_channel is not None:
            return "Current channel"
        return "Not selected"

    @property
    def can_preview(self) -> bool:
        return self.get_plan_payload() is not None

    @property
    def can_post(self) -> bool:
        return self.get_plan_payload() is not None and self.get_destination() is not None

    def compose_plan_message(self) -> Optional[str]:
        plan = self.get_plan_payload()
        if plan is None:
            return None
        content = plan.get("content", "") if isinstance(plan, dict) else ""
        header = f"📋 **War Plan — {self.selected_plan}** (Clan: `{self.selected_clan}`)"
        return f"{header}\n\n{content}".rstrip()

    def render_message(self) -> str:
        lines = [
            "**War Plan Poster**",
            f"Clan: `{self.selected_clan}`",
        ]
        if self.selected_plan:
            lines.append(f"Plan: `{self.selected_plan}`")
            plan = self.get_plan_payload() or {}
            updated = plan.get("updated_at") if isinstance(plan, dict) else None
            if isinstance(updated, str) and updated:
                lines.append(f"Last updated: {updated}")
        else:
            lines.append("Plan: None selected")
            lines.append("Use `/save_war_plan` to create templates before posting.")
        lines.append(f"Destination: {self.get_destination_label()}")
        if self.last_post_summary:
            lines.append(f"Last post: {self.last_post_summary}")
        lines.append("")
        lines.append("Adjust the menus below, then choose **Preview** or **Post Plan**.")
        return "\n".join(lines)

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(WarPlanPostClanSelect(self))
        self.add_item(WarPlanPostPlanSelect(self))
        self.add_item(WarPlanPostChannelSelect(self))
        self.add_item(WarPlanPostPreviewButton(self))
        self.add_item(WarPlanPostSendButton(self))
        self.add_item(WarPlanPostCloseButton(self))

    async def refresh_view_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh war plan view message: %s", exc)

    async def handle_preview(self, interaction: discord.Interaction) -> None:
        plan_message = self.compose_plan_message()
        if plan_message is None:
            await interaction.response.send_message(
                "⚠️ Select a saved plan to preview first.",
                ephemeral=True,
            )
            return
        chunks = _chunk_content(plan_message)
        if not chunks:
            await interaction.response.send_message(
                "⚠️ The selected plan is empty.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    async def handle_post(self, interaction: discord.Interaction) -> None:
        destination = self.get_destination()
        if destination is None:
            await interaction.response.send_message(
                "⚠️ Choose a channel where I can post war plans.",
                ephemeral=True,
            )
            return
        plan_message = self.compose_plan_message()
        if plan_message is None:
            await interaction.response.send_message(
                "⚠️ Select a saved plan before posting.",
                ephemeral=True,
            )
            return
        chunks = _chunk_content(plan_message)
        if not chunks:
            await interaction.response.send_message(
                "⚠️ The selected plan is empty.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            for chunk in chunks:
                await destination.send(chunk)
        except discord.HTTPException as exc:
            log.warning(
                "Failed to post war plan: guild=%s channel=%s plan=%s",
                self.guild.id,
                getattr(destination, "id", "?"),
                self.selected_plan,
                exc,
            )
            await interaction.followup.send(
                f"⚠️ I couldn't post the plan: {exc}",
                ephemeral=True,
            )
            return
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        self.last_post_summary = f"{destination.mention} at {timestamp}"
        await self.refresh_view_message()
        await interaction.followup.send(
            f"✅ Posted war plan `{self.selected_plan}` to {destination.mention}.",
            ephemeral=True,
        )

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="War plan poster timed out. Run `/war_plan` again to continue.",
                    view=self,
                )
            except discord.HTTPException:
                pass
class SetClanSelect(discord.ui.Select):
    """Select menu for choosing an existing clan or creating a new one."""

    def __init__(self, parent_view: "SetClanView"):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="Create new clan",
                value="__new__",
                description="Add a brand new clan configuration",
                default=parent_view.selected_name is None,
            )
        ]
        for name in sorted(parent_view.clan_map.keys(), key=str.casefold):
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name,
                    description=f"Tag {parent_view.clan_map[name]}",
                    default=name == parent_view.selected_name,
                )
            )
        super().__init__(
            placeholder="Choose a clan to edit",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        choice = self.values[0]
        if choice == "__new__":
            self.parent_view.start_new_clan()
        else:
            self.parent_view.load_clan(choice)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SetClanNameModal(discord.ui.Modal):
    """Modal used to create or rename a clan entry."""

    def __init__(self, parent_view: "SetClanView"):
        super().__init__(title="Set Clan Name", timeout=None)
        self.parent_view = parent_view
        self.name_input = discord.ui.TextInput(
            label="Clan name",
            placeholder="e.g. Phoenix Reborn",
            default=parent_view.selected_name or "",
            max_length=80,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = self.name_input.value.strip()
        if not new_name:
            await interaction.response.send_message("Clan name cannot be empty.", ephemeral=True)
            return
        if (
            new_name in self.parent_view.clan_map
            and new_name != self.parent_view.original_name
        ):
            await interaction.response.send_message(
                f"`{new_name}` is already configured. Choose another name or select it from the dropdown.",
                ephemeral=True,
            )
            return
        self.parent_view.set_name(new_name)
        await interaction.response.send_message(
            f"Clan name set to `{new_name}`.",
            ephemeral=True,
        )
        await self.parent_view.refresh_view_message()


class SetClanTagModal(discord.ui.Modal):
    """Modal used to validate and store the clan tag."""

    def __init__(self, parent_view: "SetClanView"):
        super().__init__(title="Set Clan Tag", timeout=None)
        self.parent_view = parent_view
        self.tag_input = discord.ui.TextInput(
            label="Clan tag",
            placeholder="#ABC123",
            default=parent_view.tag or "",
            max_length=20,
        )
        self.add_item(self.tag_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_tag = self.tag_input.value
        normalized = _normalise_clan_tag(raw_tag)
        if normalized is None:
            await interaction.response.send_message(
                "Please provide a valid clan tag like `#ABC123`.",
                ephemeral=True,
            )
            return
        try:
            clan_payload = await client.get_clan(normalized)
        except coc.errors.NotFound:
            await interaction.response.send_message(
                f"I couldn't find a clan with tag `{normalized}`.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Unable to verify that tag right now: {exc}",
                ephemeral=True,
            )
            return

        self.parent_view.set_tag(normalized, api_name=clan_payload.get("name"))
        await interaction.response.send_message(
            f"Clan tag set to `{normalized}`.",
            ephemeral=True,
        )
        await self.parent_view.refresh_view_message()


class SetClanToggleAlertsButton(discord.ui.Button):
    """Toggle war alert automation for the clan."""

    def __init__(self, parent_view: "SetClanView"):
        self.parent_view = parent_view
        label = "Alerts: On" if parent_view.enable_alerts else "Alerts: Off"
        style = discord.ButtonStyle.success if parent_view.enable_alerts else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.enable_alerts = not self.parent_view.enable_alerts
        self.parent_view.unsaved_changes = True
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SetClanSaveButton(discord.ui.Button):
    """Persist the current clan settings."""

    def __init__(self, parent_view: "SetClanView"):
        super().__init__(label="Save Clan", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_save(interaction)


class SetClanDeleteButton(discord.ui.Button):
    """Delete the currently selected clan configuration."""

    def __init__(self, parent_view: "SetClanView"):
        super().__init__(label="Delete Clan", style=discord.ButtonStyle.danger, row=3)
        self.parent_view = parent_view
        if not parent_view.can_delete_current_clan:
            self.disabled = True

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_delete(interaction)


class SetClanCancelButton(discord.ui.Button):
    """Close the clan editor without making further changes."""

    def __init__(self, parent_view: "SetClanView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="Clan management closed.",
            view=self.parent_view,
        )


class SetClanView(discord.ui.View):
    """Interactive interface for creating or updating clan mappings."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        selected_clan: Optional[str],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.message: Optional[discord.Message] = None
        self.guild_config = _ensure_guild_config(guild.id)
        self.clans: Dict[str, Any] = self.guild_config.setdefault("clans", {})
        self.clan_map = _clan_names_for_guild(guild.id)

        self.original_name: Optional[str] = None
        self.selected_name: Optional[str] = None
        self.tag: Optional[str] = None
        self.enable_alerts: bool = True
        self.alert_channel_id: Optional[int] = None
        self.unsaved_changes: bool = False

        if selected_clan and selected_clan in self.clans:
            self.load_clan(selected_clan)
        else:
            self.start_new_clan()

        self.refresh_components()

    @property
    def can_delete_current_clan(self) -> bool:
        return self.original_name is not None

    def load_clan(self, clan_name: str) -> None:
        entry = self.clans.get(clan_name)
        if not isinstance(entry, dict):
            self.start_new_clan()
            return
        alerts = entry.get("alerts", {}) if isinstance(entry.get("alerts"), dict) else {}
        self.original_name = clan_name
        self.selected_name = clan_name
        tag = entry.get("tag")
        self.tag = _normalise_clan_tag(tag) if isinstance(tag, str) else None
        self.enable_alerts = bool(alerts.get("enabled", False))
        channel_id = alerts.get("channel_id")
        self.alert_channel_id = channel_id if isinstance(channel_id, int) else None
        self.unsaved_changes = False
        self.clan_map = _clan_names_for_guild(self.guild.id)
        self.refresh_components()

    def start_new_clan(self) -> None:
        self.original_name = None
        self.selected_name = None
        self.tag = None
        self.enable_alerts = True
        self.alert_channel_id = None
        self.unsaved_changes = False
        self.refresh_components()

    def set_name(self, name: str) -> None:
        self.selected_name = name
        if self.original_name is None:
            self.original_name = None
        self.unsaved_changes = True
        self.refresh_components()

    def set_tag(self, tag: str, *, api_name: Optional[str]) -> None:
        self.tag = tag
        if api_name and (self.original_name is None or self.selected_name is None):
            inferred = api_name.strip()
            if inferred and inferred not in self.clan_map:
                self.selected_name = inferred
        self.unsaved_changes = True
        self.refresh_components()

    def refresh_components(self) -> None:
        self.clear_items()
        self.clan_map = _clan_names_for_guild(self.guild.id)
        self.add_item(SetClanSelect(self))
        self.add_item(SetClanToggleAlertsButton(self))
        self.add_item(SetClanSaveButton(self))
        self.add_item(SetClanDeleteButton(self))
        self.add_item(SetClanCancelButton(self))

    def render_message(self) -> str:
        name_display = self.selected_name or "(unsaved clan)"
        tag_display = self.tag or "(not set)"
        alerts_text = "Enabled" if self.enable_alerts else "Disabled"
        channel_text = "None"
        if isinstance(self.alert_channel_id, int):
            channel_obj = self.guild.get_channel(self.alert_channel_id)
            if isinstance(channel_obj, discord.TextChannel):
                channel_text = channel_obj.mention
            else:
                channel_text = f"<#{self.alert_channel_id}>"
        return "\n".join(
            [
                "**Clan Manager**",
                f"Clan name: `{name_display}`",
                f"Clan tag: {tag_display}",
                f"War alerts: {alerts_text}",
                f"Alert channel: {channel_text}",
                "",
                "Use the buttons below to set the clan name, verify the tag, toggle alerts, and save your changes.",
            ]
        )

    def disable_all_items(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Clan management timed out. Run `/set_clan` again to continue.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def handle_save(self, interaction: discord.Interaction) -> None:
        if not self.selected_name:
            await interaction.response.send_message(
                "Set a clan name before saving.",
                ephemeral=True,
            )
            return
        if not self.tag:
            await interaction.response.send_message(
                "Set a clan tag before saving.",
                ephemeral=True,
            )
            return

        normalized_tag = _normalise_clan_tag(self.tag)
        if normalized_tag is None:
            await interaction.response.send_message(
                "The stored tag is invalid. Please set it again.",
                ephemeral=True,
            )
            return

        clans = self.guild_config.setdefault("clans", {})
        conflicting = next(
            (
                name
                for name, data in clans.items()
                if name not in {self.original_name, self.selected_name}
                and isinstance(data, dict)
                and _normalise_clan_tag(str(data.get("tag", ""))) == normalized_tag
            ),
            None,
        )
        if conflicting:
            await interaction.response.send_message(
                f"That tag is already linked to `{conflicting}`. Remove it first or choose another tag.",
                ephemeral=True,
            )
            return

        response, followup = _apply_clan_update(
            self.guild,
            self.selected_name,
            normalized_tag,
            self.enable_alerts,
            preserve_channel=self.alert_channel_id,
        )

        if self.original_name and self.original_name != self.selected_name:
            clans.pop(self.original_name, None)
            save_server_config()

        self.clan_map = _clan_names_for_guild(self.guild.id)
        self.load_clan(self.selected_name)

        if not interaction.response.is_done():
            await interaction.response.send_message(response, ephemeral=True)
            if followup:
                await interaction.followup.send(followup, ephemeral=True)
        else:
            await interaction.followup.send(response, ephemeral=True)
            if followup:
                await interaction.followup.send(followup, ephemeral=True)
        await self.refresh_view_message()

    async def handle_delete(self, interaction: discord.Interaction) -> None:
        if not self.original_name:
            await interaction.response.send_message(
                "Select an existing clan before deleting.",
                ephemeral=True,
            )
            return
        clans = self.guild_config.setdefault("clans", {})
        if self.original_name in clans:
            clans.pop(self.original_name, None)
            save_server_config()
        self.clan_map = _clan_names_for_guild(self.guild.id)
        if self.clan_map:
            first = next(iter(self.clan_map.keys()))
            self.load_clan(first)
        else:
            self.start_new_clan()
        await interaction.response.send_message(
            f"Deleted clan `{self.original_name}`.",
            ephemeral=True,
        )
        await self.refresh_view_message()

    def set_tag_value(self, tag: Optional[str]) -> None:
        self.tag = tag

    def set_name_value(self, name: Optional[str]) -> None:
        self.selected_name = name

class SeasonSummaryClanSelect(discord.ui.Select):
    """Select menu for choosing which clan to summarise."""

    def __init__(self, parent_view: "SeasonSummaryView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name == parent_view.selected_clan,
            )
            for name in sorted(parent_view.clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Choose a clan",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_clan(self.values[0])
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SeasonSummaryChannelSelect(discord.ui.ChannelSelect):
    """Channel selector to override the posting destination."""

    def __init__(self, parent_view: "SeasonSummaryView"):
        self.parent_view = parent_view
        default_values = []
        if parent_view.channel_id is not None:
            channel = parent_view.guild.get_channel(parent_view.channel_id)
            if isinstance(channel, discord.TextChannel):
                default_values = [channel]
        super().__init__(
            placeholder="Override destination channel (optional)",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            default_values=default_values,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        channel = self.values[0] if self.values else None
        self.parent_view.set_channel(channel)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SeasonSummaryToggleButton(discord.ui.Button):
    """Toggle button for enabling sections."""

    def __init__(self, parent_view: "SeasonSummaryView", attr: str, label: str, *, row: int):
        self.parent_view = parent_view
        self.attr = attr
        enabled = getattr(parent_view, attr)
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        text = f"{label}: {'On' if enabled else 'Off'}"
        super().__init__(label=text, style=style, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        current = getattr(self.parent_view, self.attr)
        setattr(self.parent_view, self.attr, not current)
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SeasonSummaryPreviewButton(discord.ui.Button):
    """Button that provides an ephemeral preview."""

    def __init__(self, parent_view: "SeasonSummaryView"):
        super().__init__(label="Preview Summary", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        try:
            payload, _ = await self.parent_view.compose_summary()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(payload, ephemeral=True)


class SeasonSummaryPostButton(discord.ui.Button):
    """Button that posts the summary to the selected channel."""

    def __init__(self, parent_view: "SeasonSummaryView"):
        super().__init__(label="Post Summary", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_post(interaction)


class SeasonSummaryCancelButton(discord.ui.Button):
    """Button that closes the summary view."""

    def __init__(self, parent_view: "SeasonSummaryView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="Season summary cancelled.",
            view=self.parent_view,
        )


class SeasonSummaryView(discord.ui.View):
    """Interactive view that guides administrators through posting a season summary."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        clan_map: Dict[str, str],
        selected_clan: str,
        include_donations: bool,
        include_wars: bool,
        include_members: bool,
        channel_id: Optional[int],
        fallback_channel_id: Optional[int],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_map = clan_map
        self.message: Optional[discord.Message] = None

        self.selected_clan = selected_clan
        self.include_donations = include_donations
        self.include_wars = include_wars
        self.include_members = include_members
        self.channel_id = channel_id
        self.fallback_channel_id = fallback_channel_id

        self.refresh_components()

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(SeasonSummaryClanSelect(self))
        self.add_item(SeasonSummaryChannelSelect(self))
        self.add_item(SeasonSummaryToggleButton(self, "include_donations", "Donations", row=2))
        self.add_item(SeasonSummaryToggleButton(self, "include_wars", "Wars", row=2))
        self.add_item(SeasonSummaryToggleButton(self, "include_members", "Members", row=2))
        self.add_item(SeasonSummaryPreviewButton(self))
        self.add_item(SeasonSummaryPostButton(self))
        self.add_item(SeasonSummaryCancelButton(self))

    def render_message(self) -> str:
        sections = []
        if self.include_donations:
            sections.append("Donations")
        if self.include_wars:
            sections.append("Wars")
        if self.include_members:
            sections.append("Members")
        sections_text = ", ".join(sections) if sections else "None selected"

        channel_display = "Default channel"
        destination = self.resolve_destination()
        if destination is not None:
            channel_display = destination.mention
        elif self.channel_id is not None:
            channel_display = f"<#{self.channel_id}>"

        return "\n".join(
            [
                "**Season Summary Composer**",
                f"Clan: `{self.selected_clan}`",
                f"Sections: {sections_text}",
                f"Destination: {channel_display}",
                "",
                "Adjust the options below, preview if needed, then press **Post Summary**.",
            ]
        )

    def set_clan(self, clan_name: str) -> None:
        if clan_name not in self.clan_map or clan_name == self.selected_clan:
            return
        self.selected_clan = clan_name
        if self.channel_id is None:
            default_id = self.default_channel_id(clan_name)
            if default_id is not None:
                self.channel_id = default_id
        self.refresh_components()

    def set_channel(self, channel: Optional[discord.abc.GuildChannel]) -> None:
        if isinstance(channel, discord.TextChannel):
            self.channel_id = channel.id
        else:
            self.channel_id = None

    def default_channel_id(self, clan_name: str) -> Optional[int]:
        clan_entry = _get_clan_entry(self.guild.id, clan_name)
        if isinstance(clan_entry, dict):
            channel_id = clan_entry.get("season_summary", {}).get("channel_id")
            if isinstance(channel_id, int):
                return channel_id
        return None

    def resolve_destination(self) -> Optional[discord.TextChannel]:
        if self.channel_id is not None:
            channel = self.guild.get_channel(self.channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        default_id = self.default_channel_id(self.selected_clan)
        if isinstance(default_id, int):
            channel = self.guild.get_channel(default_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        if isinstance(self.fallback_channel_id, int):
            channel = self.guild.get_channel(self.fallback_channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    async def compose_summary(self) -> Tuple[str, Optional[int]]:
        clan_entry = _get_clan_entry(self.guild.id, self.selected_clan)
        if clan_entry is None:
            raise ValueError(f"`{self.selected_clan}` is not configured.")
        return await _compose_season_summary(
            self.guild,
            self.selected_clan,
            clan_entry,
            include_donations=self.include_donations,
            include_wars=self.include_wars,
            include_members=self.include_members,
        )

    async def handle_post(self, interaction: discord.Interaction) -> None:
        try:
            payload, default_channel_id = await self.compose_summary()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        destination = self.resolve_destination()
        if destination is None:
            if isinstance(default_channel_id, int):
                destination = self.guild.get_channel(default_channel_id)  # type: ignore[assignment]
        if destination is None and isinstance(self.fallback_channel_id, int):
            destination = self.guild.get_channel(self.fallback_channel_id)  # type: ignore[assignment]
        if destination is None and isinstance(interaction.channel, discord.TextChannel):
            destination = interaction.channel

        if destination is None:
            await interaction.response.send_message(
                "I couldn't find a suitable channel to post the summary.",
                ephemeral=True,
            )
            return
        if not destination.permissions_for(destination.guild.me).send_messages:
            await interaction.response.send_message(
                "I don't have permission to post in the selected channel.",
                ephemeral=True,
            )
            return

        for chunk in _chunk_content(payload):
            await destination.send(chunk)

        await interaction.response.send_message(
            f"Season summary posted to {destination.mention}.",
            ephemeral=True,
        )
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=f"Season summary posted to {destination.mention}.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Season summary session timed out. Run `/season_summary` again to restart.",
                    view=self,
                )
            except discord.HTTPException:
                pass


class LinkPlayerDetailsModal(discord.ui.Modal):
    """Modal used to capture the player tag and optional alias."""

    def __init__(self, parent_view: "LinkPlayerView"):
        title = "Link Clash Account" if parent_view.selected_action == "link" else "Unlink Clash Account"
        super().__init__(title=title, timeout=None)
        self.parent_view = parent_view
        self.player_tag = discord.ui.TextInput(
            label="Player tag",
            placeholder="#ABC123",
            default=parent_view.selected_tag or "",
            max_length=20,
        )
        self.add_item(self.player_tag)
        self.alias_input: Optional[discord.ui.TextInput]
        if parent_view.selected_action == "link":
            alias_input = discord.ui.TextInput(
                label="Alias (optional)",
                placeholder="In-game name or nickname",
                default=parent_view.selected_alias or "",
                required=False,
                max_length=50,
            )
            self.alias_input = alias_input
            self.add_item(alias_input)
        else:
            self.alias_input = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tag_value = self.player_tag.value.strip()
        alias_value: Optional[str] = None
        if self.alias_input is not None:
            alias_raw = self.alias_input.value.strip()
            alias_value = alias_raw or None
        self.parent_view.set_details(tag_value, alias_value)
        await interaction.response.send_message("Player details updated.", ephemeral=True)
        await self.parent_view.refresh_view_message()


class LinkPlayerActionSelect(discord.ui.Select):
    """Dropdown for toggling between link and unlink actions."""

    def __init__(self, parent_view: "LinkPlayerView"):
        options = [
            discord.SelectOption(label="Link", value="link", default=parent_view.selected_action == "link"),
            discord.SelectOption(label="Unlink", value="unlink", default=parent_view.selected_action == "unlink"),
        ]
        super().__init__(
            placeholder="Choose an action",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        choice = self.values[0]
        self.parent_view.selected_action = choice
        if choice == "unlink":
            self.parent_view.selected_alias = None
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        self.parent_view.refresh_components()
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class LinkPlayerTargetSelect(discord.ui.UserSelect):
    """User selector that lets administrators manage other members."""

    def __init__(self, parent_view: "LinkPlayerView"):
        self.parent_view = parent_view
        default_values = [parent_view.selected_target] if parent_view.selected_target is not None else []
        super().__init__(
            placeholder="Choose the member to manage",
            min_values=1,
            max_values=1,
            default_values=default_values,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "⚠️ I couldn't resolve your guild membership. Please try again.",
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only administrators can change the target member.",
                ephemeral=True,
            )
            return
        selected = self.values[0]
        if not isinstance(selected, discord.Member):
            await interaction.response.send_message(
                "⚠️ Please choose a member from this server.",
                ephemeral=True,
            )
            return
        self.parent_view.set_target(selected)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class LinkPlayerDetailsButton(discord.ui.Button):
    """Button to open the modal for editing tag and alias."""

    def __init__(self, parent_view: "LinkPlayerView"):
        super().__init__(label="Set player details", style=discord.ButtonStyle.primary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.send_modal(LinkPlayerDetailsModal(self.parent_view))


class LinkPlayerConfirmPrivateButton(discord.ui.Button):
    """Button that applies the change and keeps the confirmation private."""

    def __init__(self, parent_view: "LinkPlayerView"):
        super().__init__(label="Confirm (private)", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_submit(interaction, broadcast=False)


class LinkPlayerConfirmBroadcastButton(discord.ui.Button):
    """Button that applies the change and broadcasts it to the channel."""

    def __init__(self, parent_view: "LinkPlayerView"):
        super().__init__(label="Confirm & broadcast", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.handle_submit(interaction, broadcast=True)


class LinkPlayerCancelButton(discord.ui.Button):
    """Button that closes the link player view."""

    def __init__(self, parent_view: "LinkPlayerView"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.disable_all_items()
        await interaction.response.edit_message(
            content="Link player view closed.",
            view=self.parent_view,
        )


class LinkPlayerView(discord.ui.View):
    """Interactive view for linking or unlinking Clash of Clans accounts."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        selected_action: str,
        initial_tag: str,
        initial_alias: Optional[str],
        initial_target: discord.Member,
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.actor = actor
        self.selected_action = selected_action if selected_action in {"link", "unlink"} else "link"
        self.selected_tag = initial_tag or ""
        self.selected_alias = initial_alias if self.selected_action == "link" else None
        self.selected_target = initial_target or actor
        self.message: Optional[discord.Message] = None
        self.last_result: Optional[str] = None
        self._linked_summary = ""

        self.refresh_state()
        self.refresh_components()

    def refresh_state(self) -> None:
        target_id = self.selected_target.id if isinstance(self.selected_target, discord.Member) else self.actor.id
        self._linked_summary = _summarise_linked_accounts(self.guild, target_id)

    def set_target(self, member: discord.Member) -> None:
        self.selected_target = member
        self.refresh_state()
        self.refresh_components()

    def set_details(self, tag: str, alias: Optional[str]) -> None:
        self.selected_tag = tag.strip()
        if self.selected_action == "link":
            self.selected_alias = alias
        else:
            self.selected_alias = None
        self.refresh_components()

    @property
    def can_submit(self) -> bool:
        return bool(self.selected_tag)

    def render_message(self) -> str:
        target = self.selected_target if isinstance(self.selected_target, discord.Member) else self.actor
        tag_display = self.selected_tag or "(not set)"
        lines = [
            "**Link Clash Accounts**" if self.selected_action == "link" else "**Unlink Clash Accounts**",
            f"Action: {self.selected_action.title()}",
            f"Target member: {target.mention}",
            f"Player tag: `{tag_display}`" if self.selected_tag else "Player tag: (use *Set player details*)",
        ]
        if self.selected_action == "link":
            alias_display = self.selected_alias or "(auto-detect from profile)"
            lines.append(f"Alias: {alias_display}")
        lines.append(f"Currently linked: {self._linked_summary}")
        if self.last_result:
            lines.append("")
            lines.append(f"Last result: {self.last_result}")
        lines.append("")
        lines.append("Use the controls below to set the account details, then choose whether to broadcast the update or keep it private.")
        return "\n".join(lines)

    def refresh_components(self) -> None:
        self.clear_items()
        self.add_item(LinkPlayerActionSelect(self))
        if self.actor.guild_permissions.administrator:
            self.add_item(LinkPlayerTargetSelect(self))
        self.add_item(LinkPlayerDetailsButton(self))
        private_button = LinkPlayerConfirmPrivateButton(self)
        broadcast_button = LinkPlayerConfirmBroadcastButton(self)
        if not self.can_submit:
            private_button.disabled = True
            broadcast_button.disabled = True
        self.add_item(private_button)
        self.add_item(broadcast_button)
        self.add_item(LinkPlayerCancelButton(self))

    async def refresh_view_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.render_message(), view=self)
        except discord.HTTPException as exc:
            log.warning("Failed to refresh link_player view message: %s", exc)

    async def handle_submit(self, interaction: discord.Interaction, *, broadcast: bool) -> None:
        target = self.selected_target if isinstance(self.selected_target, discord.Member) else self.actor
        if not self.can_submit:
            await interaction.response.send_message("⚠️ Set a player tag before confirming.", ephemeral=True)
            return
        try:
            message = await _link_player_account(
                guild=self.guild,
                actor=self.actor,
                target=target,
                action=self.selected_action,
                player_tag=self.selected_tag,
                alias=self.selected_alias if self.selected_action == "link" else None,
            )
        except PlayerLinkError as exc:
            await interaction.response.send_message(exc.message, ephemeral=True)
            return

        if self.selected_action == "link":
            message += " You can now reference it quickly with `/player_info`."
        else:
            message += " You can relink it anytime with `/link_player action:link`."

        self.last_result = message
        self.selected_tag = ""
        if self.selected_action == "link":
            self.selected_alias = None
        self.refresh_state()
        self.refresh_components()
        await self.refresh_view_message()

        if broadcast:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channel = interaction.channel
            destination = None
            if isinstance(channel, discord.TextChannel):
                me = channel.guild.me
                if me and channel.permissions_for(me).send_messages:
                    destination = channel
            if destination is None:
                await interaction.followup.send(
                    "⚠️ I couldn't broadcast the update because I don't have permission to post here.",
                    ephemeral=True,
                )
                return
            try:
                await destination.send(message)
            except discord.HTTPException as exc:
                log.warning(
                    "Failed to broadcast link_player result: guild=%s channel=%s",
                    self.guild.id,
                    getattr(destination, "id", "?"),
                )
                await interaction.followup.send(
                    f"⚠️ I couldn't broadcast the update: {exc}",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"✅ Broadcast posted in {destination.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Link player view timed out. Run `/link_player` to start again.",
                    view=self,
                )
            except discord.HTTPException:
                pass

class LinkPlayerModal(discord.ui.Modal):
    """Modal dialog to link or unlink player tags during onboarding."""

    def __init__(self, parent_view: "RegisterMeView", *, action: Literal["link", "unlink"]):
        title = "Link Clash Account" if action == "link" else "Unlink Clash Account"
        super().__init__(title=title, timeout=None)
        self.parent_view = parent_view
        self.action = action
        self.player_tag = discord.ui.TextInput(
            label="Player tag",
            placeholder="#ABC123",
            max_length=20,
        )
        self.add_item(self.player_tag)
        self.alias: Optional[discord.ui.TextInput]
        if action == "link":
            alias_input = discord.ui.TextInput(
                label="Alias (optional)",
                placeholder="In-game name or nickname",
                required=False,
                max_length=50,
            )
            self.add_item(alias_input)
            self.alias = alias_input
        else:
            self.alias = None


    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = self.parent_view.member
        guild = self.parent_view.guild
        actor = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)  # type: ignore[arg-type]
        )

        if actor is None:
            await interaction.response.send_message(
                "⚠️ I couldn't resolve your guild membership. Please try again.",
                ephemeral=True,
            )
            return

        if actor.id != member.id and not actor.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only the member themselves or an administrator can manage linked tags from this view.",
                ephemeral=True,
            )
            return

        alias_value: Optional[str] = None
        if self.alias is not None:
            raw_alias = self.alias.value.strip()
            alias_value = raw_alias or None

        try:
            message = await _link_player_account(
                guild=guild,
                actor=actor,
                target=member,
                action=self.action,
                player_tag=self.player_tag.value,
                alias=alias_value,
            )
        except PlayerLinkError as exc:
            await interaction.response.send_message(exc.message, ephemeral=True)
            return

        self.parent_view.refresh_components()
        updated_intro = self.parent_view.build_intro_message()
        parent_message = getattr(self.parent_view, "message", None)
        if parent_message is not None:
            try:
                await parent_message.edit(content=updated_intro, view=self.parent_view)
            except discord.HTTPException as exc:
                log.warning("Failed to refresh register_me message after linking: %s", exc)
        else:
            log.debug("RegisterMeView message handle missing; skipping intro refresh")

        await interaction.response.send_message(message, ephemeral=True)

class LinkPlayerSelect(discord.ui.Select):
    """Select menu that manages link/unlink actions for Clash accounts."""

    def __init__(self, parent_view: "RegisterMeView", existing_accounts: List[Dict[str, Optional[str]]]):
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="Link new Clash account",
                description="Add a new player tag",
                value="link-new",
                emoji="➕",
            )
        ]
        for record in existing_accounts:
            tag = record.get("tag")
            if not isinstance(tag, str) or not tag.strip():
                continue
            alias = record.get("alias")
            display = f"{alias} ({tag})" if isinstance(alias, str) and alias else tag
            options.append(
                discord.SelectOption(
                    label=f"Remove {display}",
                    description="Unlink this account",
                    value=f"unlink|{tag}",
                    emoji="🗑️",
                )
            )
        super().__init__(
            placeholder="Manage linked Clash accounts",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "⚠️ I couldn't resolve your guild membership. Please try again.",
                ephemeral=True,
            )
            return

        member = self.parent_view.member
        actor = interaction.user
        if actor.id != member.id and not actor.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Only the member themselves or an administrator can manage linked tags from this view.",
                ephemeral=True,
            )
            return

        selection = self.values[0]
        if selection == "link-new":
            if interaction.message is not None:
                self.parent_view.message = interaction.message
            await interaction.response.send_modal(LinkPlayerModal(self.parent_view, action="link"))
            return

        if not selection.startswith("unlink|"):
            await interaction.response.send_message(
                "⚠️ Unknown selection received. Please try again.",
                ephemeral=True,
            )
            return

        tag = selection.split("|", 1)[1]
        try:
            message = await _link_player_account(
                guild=self.parent_view.guild,
                actor=actor,
                target=member,
                action="unlink",
                player_tag=tag,
                alias=None,
            )
        except PlayerLinkError as exc:
            await interaction.response.send_message(exc.message, ephemeral=True)
            return

        self.parent_view.refresh_components()
        updated_intro = self.parent_view.build_intro_message()
        parent_message = getattr(self.parent_view, "message", None)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
            parent_message = interaction.message
        if parent_message is not None:
            try:
                await parent_message.edit(content=updated_intro, view=self.parent_view)
            except discord.HTTPException as exc:
                log.warning("Failed to refresh register_me message after unlinking: %s", exc)
        else:
            log.debug("RegisterMeView message handle missing; skipping intro refresh")

        await interaction.response.send_message(
            message + " You can relink it anytime with `/link_player action:link`.",
            ephemeral=True,
        )

class WarNudgeReasonModal(discord.ui.Modal):
    """Modal used to capture reason metadata when saving."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        super().__init__(title="Save War Nudge Reason", timeout=None)
        self.parent_view = parent_view

        self.reason_name = discord.ui.TextInput(
            label="Reason name",
            placeholder="e.g. Missed First Attack",
            max_length=80,
            default=parent_view.pending_reason_name or "",
        )
        self.add_item(self.reason_name)

        self.description = discord.ui.TextInput(
            label="Description (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            default=parent_view.pending_reason_description or "",
        )
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_reason_modal_submit(
            interaction,
            name=self.reason_name.value.strip(),
            description=self.description.value.strip(),
        )


class WarNudgeClanSelect(discord.ui.Select):
    """Select menu for choosing which clan to configure."""

    def __init__(self, parent_view: "WarNudgeConfigView", clan_map: Dict[str, str]):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name == parent_view.clan_name,
            )
            for name in sorted(clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Select a clan to configure",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        new_clan = self.values[0]
        self.parent_view.set_clan(new_clan)
        self.parent_view.refresh_components()
        self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarNudgeReasonSelect(discord.ui.Select):
    """Select menu for picking an existing reason or creating a new one."""

    def __init__(self, parent_view: "WarNudgeConfigView", reasons: List[Dict[str, Any]]):
        self.parent_view = parent_view
        options: List[discord.SelectOption] = [
            discord.SelectOption(
                label="➕ Create new reason",
                value="__new__",
                description="Define a brand new war nudge reason",
                default=parent_view.selected_reason_name == "__new__",
            )
        ]
        for reason in reasons:
            name = reason.get("name", "Unnamed")
            options.append(
                discord.SelectOption(
                    label=name,
                    value=name,
                    description=f"Type: {reason.get('type', 'unknown')}",
                    default=name == parent_view.selected_reason_name,
                )
            )
        super().__init__(
            placeholder="Select a reason to edit or create a new one",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        choice = self.values[0]
        self.parent_view.set_reason(choice)
        self.parent_view.refresh_components()
        self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarNudgeTypeSelect(discord.ui.Select):
    """Select menu for adjusting the reason type."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=reason_type.replace("_", " ").title(),
                value=reason_type,
                default=reason_type == parent_view.selected_reason_type,
            )
            for reason_type in WAR_NUDGE_REASONS
        ]
        super().__init__(
            placeholder="Select the reason type",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.selected_reason_type = self.values[0]
        self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarNudgeRoleSelect(discord.ui.RoleSelect):
    """Role selector for specifying who to mention."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        self.parent_view = parent_view
        default_role = None
        if parent_view.selected_role_id is not None:
            default_role = parent_view.guild.get_role(parent_view.selected_role_id)
        default_values = [default_role] if default_role is not None else []
        super().__init__(
            placeholder="Mention role (optional)",
            min_values=0,
            max_values=1,
            default_values=default_values,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        role = self.values[0] if self.values else None
        self.parent_view.selected_role_id = role.id if role else None
        self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class WarNudgeMemberSelect(discord.ui.UserSelect):
    """Member selector for specifying a direct mention."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        self.parent_view = parent_view
        default_member = None
        if parent_view.selected_user_id is not None:
            default_member = parent_view.guild.get_member(parent_view.selected_user_id)
        default_values = [default_member] if default_member is not None else []
        super().__init__(
            placeholder="Mention member (optional)",
            min_values=0,
            max_values=1,
            default_values=default_values,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = self.values[0] if self.values else None
        self.parent_view.selected_user_id = member.id if member else None
        self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SaveWarNudgeButton(discord.ui.Button):
    """Button that triggers saving (adding/updating) a reason."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        super().__init__(label="Save Reason", style=discord.ButtonStyle.success, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not self.parent_view.selected_reason_type:
            await interaction.response.send_message(
                "⚠️ Choose a reason type before saving.",
                ephemeral=True,
            )
            return
        if not (self.parent_view.selected_role_id or self.parent_view.selected_user_id):
            await interaction.response.send_message(
                "⚠️ Please choose at least one role or member to mention.",
                ephemeral=True,
            )
            return
        self.parent_view.message = interaction.message
        self.parent_view.pending_reason_name = (
            None if self.parent_view.selected_reason_name == "__new__" else self.parent_view.selected_reason_name
        )
        self.parent_view.pending_reason_description = self.parent_view.selected_description or ""
        await interaction.response.send_modal(WarNudgeReasonModal(self.parent_view))


class RemoveWarNudgeButton(discord.ui.Button):
    """Button for removing the currently selected reason."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        super().__init__(label="Remove Reason", style=discord.ButtonStyle.danger, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if self.parent_view.selected_reason_name == "__new__":
            await interaction.response.send_message(
                "⚠️ Select an existing reason before removing.",
                ephemeral=True,
            )
            return
        self.parent_view.message = interaction.message
        await self.parent_view.remove_selected_reason(interaction)


class ListWarNudgeButton(discord.ui.Button):
    """Button that displays all configured reasons for the current clan."""

    def __init__(self, parent_view: "WarNudgeConfigView"):
        super().__init__(label="List Reasons", style=discord.ButtonStyle.primary, row=4)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.send_reason_list(interaction)


class WarNudgeConfigView(discord.ui.View):
    """Interactive interface for managing war nudge reasons."""

    def __init__(self, guild: discord.Guild, clan_name: str, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.message: Optional[discord.Message] = None
        self.clan_map = _clan_names_for_guild(guild.id)
        self.clan_name = clan_name if clan_name in self.clan_map else next(iter(self.clan_map), None)
        self.selected_reason_name = "__new__"
        self.selected_reason_type = WAR_NUDGE_REASONS[0]
        self.selected_role_id: Optional[int] = None
        self.selected_user_id: Optional[int] = None
        self.selected_description: str = ""
        self.pending_reason_name: Optional[str] = None
        self.pending_reason_description: Optional[str] = None
        self.refresh_state()
        self.refresh_components()

    def refresh_state(self) -> None:
        clan_entry = _get_clan_entry(self.guild.id, self.clan_name) if self.clan_name else None
        war_nudge = clan_entry.get("war_nudge", {}) if isinstance(clan_entry, dict) else {}
        self.reasons: List[Dict[str, Any]] = war_nudge.get("reasons", [])

        if self.selected_reason_name != "__new__":
            matched = next(
                (reason for reason in self.reasons if reason.get("name", "").lower() == self.selected_reason_name.lower()),
                None,
            )
            if matched is None:
                self.selected_reason_name = "__new__"
                self.selected_reason_type = WAR_NUDGE_REASONS[0]
                self.selected_role_id = None
                self.selected_user_id = None
                self.selected_description = ""
            else:
                self.selected_reason_type = matched.get("type", WAR_NUDGE_REASONS[0])
                self.selected_role_id = matched.get("mention_role_id")
                self.selected_user_id = matched.get("mention_user_id")
                self.selected_description = matched.get("description", "")

    def refresh_components(self) -> None:
        self.clear_items()
        clan_map = _clan_names_for_guild(self.guild.id)
        if not clan_map:
            return
        clan_entry = _get_clan_entry(self.guild.id, self.clan_name) if self.clan_name else None
        if isinstance(clan_entry, dict):
            reasons = list(clan_entry.get("war_nudge", {}).get("reasons", []))
        else:
            reasons = []

        self.add_item(WarNudgeClanSelect(self, clan_map))
        self.add_item(WarNudgeReasonSelect(self, reasons))
        self.add_item(WarNudgeTypeSelect(self))
        self.add_item(WarNudgeRoleSelect(self))
        self.add_item(WarNudgeMemberSelect(self))
        self.add_item(SaveWarNudgeButton(self))
        self.add_item(RemoveWarNudgeButton(self))
        self.add_item(ListWarNudgeButton(self))

    def set_clan(self, clan_name: str) -> None:
        self.clan_name = clan_name
        self.selected_reason_name = "__new__"
        self.selected_reason_type = WAR_NUDGE_REASONS[0]
        self.selected_role_id = None
        self.selected_user_id = None
        self.selected_description = ""
        self.refresh_state()

    def set_reason(self, value: str) -> None:
        self.selected_reason_name = value
        if value == "__new__":
            self.selected_reason_type = WAR_NUDGE_REASONS[0]
            self.selected_role_id = None
            self.selected_user_id = None
            self.selected_description = ""
        self.refresh_state()

    def render_message(self) -> str:
        if not self.clan_name:
            return (
                "⚠️ No clans are configured yet. Use `/set_clan` to add one before managing war nudges."
            )
        description_line = self.selected_description or "No description set."
        target_summary = []
        if self.selected_role_id:
            role = self.guild.get_role(self.selected_role_id)
            if role:
                target_summary.append(role.mention)
        if self.selected_user_id:
            member = self.guild.get_member(self.selected_user_id)
            if member:
                target_summary.append(member.mention)
        if not target_summary:
            target_summary.append("_No mention target selected_")
        reason_label = "New reason" if self.selected_reason_name == "__new__" else f"`{self.selected_reason_name}`"
        return (
            f"**Clan:** `{self.clan_name}`\n"
            f"**Reason:** {reason_label}\n"
            f"**Type:** `{self.selected_reason_type}`\n"
            f"**Targets:** {' '.join(target_summary)}\n"
            f"**Description:** {description_line}\n\n"
            "Use the controls below to add, update, or remove war nudge reasons."
        )

    async def handle_reason_modal_submit(self, interaction: discord.Interaction, *, name: str, description: str) -> None:
        if not name:
            await interaction.response.send_message(
                "⚠️ Reason name cannot be empty.",
                ephemeral=True,
            )
            return
        if not (self.selected_role_id or self.selected_user_id):
            await interaction.response.send_message(
                "⚠️ Please choose at least one role or member to mention.",
                ephemeral=True,
            )
            return

        clan_entry = _get_clan_entry(self.guild.id, self.clan_name)
        war_nudge = clan_entry.setdefault("war_nudge", {})
        reasons: List[Dict[str, Any]] = war_nudge.setdefault("reasons", [])

        payload = {
            "name": name,
            "type": self.selected_reason_type,
            "mention_role_id": self.selected_role_id,
            "mention_user_id": self.selected_user_id,
            "description": description,
        }

        updated = False
        for idx, reason in enumerate(reasons):
            if reason.get("name", "").lower() == name.lower():
                reasons[idx] = payload
                updated = True
                break
        if not updated:
            reasons.append(payload)

        save_server_config()
        self.selected_reason_name = name
        self.selected_description = description
        self.pending_reason_name = None
        self.pending_reason_description = None
        self.refresh_state()
        self.refresh_components()

        if self.message is not None:
            try:
                await self.message.edit(content=self.render_message(), view=self)
            except discord.HTTPException as exc:
                log.warning("Failed to refresh configure_war_nudge message: %s", exc)

        await interaction.response.send_message(
            f"{'✅ Updated' if updated else '✅ Added'} war nudge reason `{name}` for `{self.clan_name}`.",
            ephemeral=True,
        )

    async def remove_selected_reason(self, interaction: discord.Interaction) -> None:
        if interaction.message is not None:
            self.message = interaction.message
        clan_entry = _get_clan_entry(self.guild.id, self.clan_name)
        war_nudge = clan_entry.setdefault("war_nudge", {})
        reasons: List[Dict[str, Any]] = war_nudge.setdefault("reasons", [])
        original_len = len(reasons)
        reasons[:] = [
            reason
            for reason in reasons
            if reason.get("name", "").lower() != self.selected_reason_name.lower()
        ]
        if len(reasons) == original_len:
            await interaction.response.send_message(
                f"⚠️ No reason named `{self.selected_reason_name}` found for `{self.clan_name}`.",
                ephemeral=True,
            )
            return

        save_server_config()
        self.selected_reason_name = "__new__"
        self.selected_reason_type = WAR_NUDGE_REASONS[0]
        self.selected_role_id = None
        self.selected_user_id = None
        self.selected_description = ""
        self.refresh_state()
        self.refresh_components()

        if self.message is not None:
            try:
                await self.message.edit(content=self.render_message(), view=self)
            except discord.HTTPException as exc:
                log.warning("Failed to refresh configure_war_nudge message after removal: %s", exc)

        await interaction.response.send_message(
            f"✅ Removed war nudge reason for `{self.clan_name}`.",
            ephemeral=True,
        )

    async def send_reason_list(self, interaction: discord.Interaction) -> None:
        reasons = self.reasons
        if not reasons:
            await interaction.response.send_message(
                f"ℹ️ No war nudge reasons are configured for `{self.clan_name}`.",
                ephemeral=True,
            )
            return
        lines = [
            f"- **{reason.get('name', 'Unnamed')}** — type: `{reason.get('type', 'unknown')}`"
            for reason in reasons
        ]
        await interaction.response.send_message(
            f"Configured reasons for `{self.clan_name}`:\n" + "\n".join(lines),
            ephemeral=True,
        )

    async def handle_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content="Session expired. Re-run the command to continue.", view=None)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await self.handle_timeout()


class ToggleRoleButton(discord.ui.Button):
    """Reusable button that toggles a specific role for the onboarding workflow."""

    def __init__(
        self,
        *,
        label: str,
        role_id: int,
        role_name: str,
        parent_view: "RegisterMeView",
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        row: Optional[int] = None,
    ):
        super().__init__(label=label, style=style, row=row)
        self.role_id = role_id
        self.role_name = role_name
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.toggle_role(interaction, self.role_id, self.role_name)


class RegisterMeView(discord.ui.View):
    """Interactive helper for new members to opt into alert roles."""

    def __init__(
        self,
        *,
        member: discord.Member,
        war_alert_role: Optional[discord.Role],
        event_roles: List[Dict[str, Any]],
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self.member = member
        self.guild = member.guild
        self.message: Optional[discord.Message] = None
        self.war_alert_role = war_alert_role
        self.event_roles_seed = [
            {
                "key": entry.get("key"),
                "label": entry.get("label"),
                "role": entry.get("role"),
            }
            for entry in event_roles
            if isinstance(entry, dict)
        ]
        self.event_roles: List[Dict[str, Any]] = []
        self.linked_account_records: List[Dict[str, Optional[str]]] = []
        self.refresh_components()

    def refresh_components(self) -> None:
        """Rebuild the interactive controls with up-to-date account data."""
        self.clear_items()
        guild_config = _ensure_guild_config(self.guild.id)
        raw_accounts = guild_config.get("player_accounts", {}).get(str(self.member.id), [])
        self.linked_account_records = raw_accounts if isinstance(raw_accounts, list) else []

        ordered_keys: List[str] = []
        for entry in self.event_roles_seed:
            key = entry.get("key")
            if isinstance(key, str) and key not in ordered_keys:
                ordered_keys.append(key)
        event_map = _get_event_roles_for_guild(self.guild.id)
        for key in event_map.keys():
            if key not in ordered_keys:
                ordered_keys.append(key)
        self.event_roles = []
        for key in ordered_keys:
            entry = event_map.get(key)
            if not isinstance(entry, dict):
                continue
            label = entry.get("label", _default_event_label(key))
            role = _get_event_role(self.guild, key)
            self.event_roles.append({"key": key, "label": label, "role": role})

        self.add_item(LinkPlayerSelect(self, self.linked_account_records))

        button_row = 1
        if self.war_alert_role is not None:
            self.add_item(
                ToggleRoleButton(
                    label="Sign Up for War Alerts",
                    role_id=self.war_alert_role.id,
                    role_name=self.war_alert_role.name,
                    parent_view=self,
                    style=discord.ButtonStyle.green,
                    row=button_row,
                )
            )
            button_row = 2 if button_row < 4 else 1
        for entry in self.event_roles:
            role = entry.get("role")
            label = entry.get("label")
            if not isinstance(role, discord.Role):
                continue
            button_label = f"Sign Up for {label} Alerts"
            self.add_item(
                ToggleRoleButton(
                    label=button_label,
                    role_id=role.id,
                    role_name=role.name,
                    parent_view=self,
                    row=button_row,
                )
            )
            button_row = button_row + 1 if button_row < 4 else 1

        self.add_item(
            discord.ui.Button(
                label="For further help click here",
                style=discord.ButtonStyle.link,
                url=README_URL,
                row=2,
            )
        )

    def build_intro_message(self) -> str:
        linked_accounts = _summarise_linked_accounts(self.guild, self.member.id)
        event_labels = [
            entry.get("label")
            for entry in self.event_roles
            if isinstance(entry.get("label"), str) and isinstance(entry.get("role"), discord.Role)
        ]
        if event_labels:
            events_line = f"Available event alerts: {', '.join(event_labels)}."
        else:
            events_line = "Event alerts will appear here once configured by an administrator."
        return "\n".join(
            [
                "Welcome! Here's how to get set up:",
                "1️⃣ Use the controls below to opt into the alert roles you want.",
                f"2️⃣ Link your Clash accounts right here (current links: {linked_accounts}).",
                f"3️⃣ {events_line}",
                "4️⃣ Explore `/plan_upgrade` and the other slash commands to stay organised.",
            ]
        )

    async def toggle_role(
        self,
        interaction: discord.Interaction,
        role_id: int,
        role_name: str,
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I can only manage roles for members inside this server.",
                ephemeral=True,
            )
            return

        is_owner = interaction.user.id == self.member.id
        if not is_owner and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only the member themselves or an administrator can manage these roles here.",
                ephemeral=True,
            )
            return

        role = self.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                "That role no longer exists. Ask an admin to reconfigure it.",
                ephemeral=True,
            )
            return

        target_member: Optional[discord.Member]
        if isinstance(self.member, discord.Member):
            target_member = self.member
        else:
            target_member = self.guild.get_member(self.member.id)

        if target_member is None:
            await interaction.response.send_message(
                "I couldn't resolve your member details right now. Please try again shortly.",
                ephemeral=True,
            )
            return

        try:
            if role in getattr(target_member, "roles", []):
                await interaction.response.send_message(
                    f"You have already signed up for {role_name} alert(s).",
                    ephemeral=True,
                )
                return

            await target_member.add_roles(role, reason="RegisterMe subscription")
            message = f"{target_member.mention} is now subscribed to {role_name} alert(s)."
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to modify that role.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Failed to update roles: {exc}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(message, ephemeral=True)


class DonationClanSelect(discord.ui.Select):
    """Select menu for choosing which clan's donation metrics to manage."""

    def __init__(self, parent_view: "DonationConfigView", clan_map: Dict[str, str]):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name == parent_view.clan_name,
            )
            for name in sorted(clan_map.keys(), key=str.casefold)
        ]
        super().__init__(
            placeholder="Select a clan",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.set_clan(self.values[0])
        self.parent_view.refresh_components()
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class DonationMetricSelect(discord.ui.Select):
    """Multi-select for toggling donation metrics."""

    def __init__(self, parent_view: "DonationConfigView", selected: Set[str]):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=metric.replace("_", " ").title(),
                value=metric,
                description=DONATION_METRIC_INFO.get(metric, ""),
                default=metric in selected,
            )
            for metric in DONATION_METRICS
        ]
        super().__init__(
            placeholder="Choose which metrics to highlight",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self.parent_view.selected_metrics = set(self.values)
        if interaction.message is not None:
            self.parent_view.message = interaction.message
        await interaction.response.edit_message(
            content=self.parent_view.render_message(),
            view=self.parent_view,
        )


class SaveDonationMetricsButton(discord.ui.Button):
    """Persist the currently selected donation metrics."""

    def __init__(self, parent_view: "DonationConfigView"):
        super().__init__(label="Save Metrics", style=discord.ButtonStyle.success, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self.parent_view.save_metrics(interaction)


class DonationConfigView(discord.ui.View):
    """Interactive configuration for donation metrics."""

    def __init__(self, guild: discord.Guild, clan_name: str, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.message: Optional[discord.Message] = None
        self.clan_map = _clan_names_for_guild(guild.id)
        self.clan_name = clan_name if clan_name in self.clan_map else next(iter(self.clan_map), None)
        self.selected_metrics: Set[str] = set()
        self.refresh_state()
        self.refresh_components()

    def refresh_state(self) -> None:
        clan_entry = _get_clan_entry(self.guild.id, self.clan_name) if self.clan_name else None
        donation_tracking = clan_entry.get("donation_tracking", {}) if isinstance(clan_entry, dict) else {}
        metrics = donation_tracking.get("metrics", {}) if isinstance(donation_tracking, dict) else {}
        self.selected_metrics = {metric for metric in DONATION_METRICS if metrics.get(metric, False)}

    def refresh_components(self) -> None:
        self.clear_items()
        if not self.clan_map:
            return
        self.add_item(DonationClanSelect(self, self.clan_map))
        self.add_item(DonationMetricSelect(self, self.selected_metrics))
        self.add_item(SaveDonationMetricsButton(self))

    def set_clan(self, clan_name: str) -> None:
        self.clan_name = clan_name
        self.refresh_state()

    def render_message(self) -> str:
        if not self.clan_name:
            return "⚠️ No clans are configured yet. Use `/set_clan` first."
        clan_entry = _get_clan_entry(self.guild.id, self.clan_name)
        donation_tracking = clan_entry.get("donation_tracking", {}) if isinstance(clan_entry, dict) else {}
        channel_id = donation_tracking.get("channel_id")
        channel_ref = f"<#{channel_id}>" if isinstance(channel_id, int) else "_Not configured_"
        if self.selected_metrics:
            metrics_lines = "\n".join(
                f"- {metric.replace('_', ' ').title()}" for metric in sorted(self.selected_metrics)
            )
        else:
            metrics_lines = "_None selected_"
        return (
            f"**Clan:** `{self.clan_name}`\n"
            f"**Donation channel:** {channel_ref}\n"
            f"**Highlighted metrics:**\n{metrics_lines}\n\n"
            "Use the controls below to toggle donation metrics for this clan."
        )

    async def save_metrics(self, interaction: discord.Interaction) -> None:
        if not self.clan_name:
            await interaction.response.send_message(
                "⚠️ No clan selected. Choose a clan first.",
                ephemeral=True,
            )
            return

        clan_entry = _get_clan_entry(self.guild.id, self.clan_name)
        donation_tracking = clan_entry.setdefault("donation_tracking", {})
        metrics = donation_tracking.setdefault("metrics", {})
        for metric in DONATION_METRICS:
            metrics[metric] = metric in self.selected_metrics
        save_server_config()

        self.refresh_state()
        self.refresh_components()

        if self.message is not None:
            try:
                await self.message.edit(content=self.render_message(), view=self)
            except discord.HTTPException as exc:
                log.warning("Failed to refresh configure_donation_metrics message: %s", exc)

        await interaction.response.send_message(
            f"✅ Donation metrics updated for `{self.clan_name}`.",
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Session expired. Re-run the command to continue.",
                    view=None,
                )
            except discord.HTTPException:
                pass

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


# ---------------------------------------------------------------------------
# Slash command: /toggle_war_alerts
# ---------------------------------------------------------------------------
@bot.tree.command(name="toggle_war_alerts", description="Opt in or out of war alert pings.")
@app_commands.describe(enable="Choose True to receive alerts or False to opt out")
async def toggle_war_alerts(interaction: discord.Interaction, enable: bool):
    """Toggle the role used for mention-based war alerts."""
    _record_command_usage(interaction, "toggle_war_alerts")
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
    _record_command_usage(interaction, "assign_bases")
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
        war = await client.get_active_war_raw(tag)
        log.debug(
            "assign_bases war fetched: is_cwl=%s state=%s clan=%s enemy=%s",
            getattr(war, "is_cwl", None) if war else None,
            getattr(war, "state", None) if war else None,
            getattr(getattr(war, "clan", None), "name", None) if war else None,
            getattr(getattr(war, "opponent", None), "name", None) if war else None,
        )
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

    if war is None:
        await send_text_response(
            interaction,
            "⚠️ That clan isn't in an active war right now—per-player assignments aren't available.",
            ephemeral=True,
        )
        return

    home_roster, enemy_positions, alert_role = await build_war_roster(member, war, interaction)
    log.debug(
        "assign_bases roster sizes: home=%d enemy=%d",
        len(home_roster),
        len(enemy_positions),
    )

    if not home_roster or not enemy_positions:
        await send_text_response(
            interaction,
            "⚠️ I couldn't find a populated war roster for that clan yet. Try again once the war line-up is available.",
            ephemeral=True,
        )
        return
    
    view = AssignBasesModeView(
        interaction=interaction,
        clan_name=clan_name,
        home_roster=home_roster,
        enemy_positions=enemy_positions,
        alert_role=alert_role,
    )
    log.debug("assign_bases view components=%s", [
        (child.__class__.__name__, getattr(child, 'custom_id', None), getattr(child, 'row', None))
        for child in view.children
    ])
    intro_lines = [
        "After submitting the command with the clan name, choose how you want to share assignments.",
        "• Use **Per Player Assignments** to build the familiar per-base list without memorising the syntax.",
        "• Use **General Assignment Rule** for a quick broadcast such as \"everyone attack your mirror.\"",
    ]
    intro = "\n".join(intro_lines)
    await send_text_response(interaction, intro, ephemeral=True, view=view)

async def build_war_roster(member, war, interaction):
    def _normalise_roster(members: List[Any], label: str) -> Dict[int, str]:
        """Return a sequential mapping of base numbers to member names."""
        roster_by_position: Dict[int, str] = {}
        overflow: List[str] = []
        raw_positions: List[Tuple[Optional[int], str]] = []
        for entry in members:
            position = getattr(entry, "map_position", None)
            name = getattr(entry, "name", None) or "Unknown"
            raw_positions.append((position, name))
            if isinstance(position, int) and position > 0:
                roster_by_position[position] = name
            else:
                overflow.append(name)

        if overflow:
            next_slot = max(roster_by_position.keys(), default=0) + 1
            for name in overflow:
                while next_slot in roster_by_position:
                    next_slot += 1
                roster_by_position[next_slot] = name
                next_slot += 1

        ordered = dict(sorted(roster_by_position.items()))
        expected_positions = list(range(1, len(ordered) + 1))
        if list(ordered.keys()) != expected_positions:
            remapped = {
                index: name for index, (_, name) in enumerate(ordered.items(), start=1)
            }
            log.debug(
                "build_war_roster %s remapping positions raw=%s original_keys=%s remapped_keys=%s",
                label,
                raw_positions,
                list(ordered.keys()),
                list(remapped.keys()),
            )
            return remapped

        log.debug(
            "build_war_roster %s positions raw=%s normalised=%s",
            label,
            raw_positions,
            list(ordered.keys()),
        )
        return ordered

    home_roster = _normalise_roster(list(getattr(war.clan, "members", [])), "home")
    enemy_roster = _normalise_roster(list(getattr(war.opponent, "members", [])), "enemy")
    enemy_positions = sorted(enemy_roster.keys())

    alert_role = discord.utils.get(interaction.guild.roles, name=ALERT_ROLE_NAME)
    log.debug(
        "build_war_roster totals home=%d enemy=%d",
        len(home_roster),
        len(enemy_positions),
    )
    return home_roster, enemy_positions, alert_role
# ---------------------------------------------------------------------------
# Autocomplete

# ---------------------------------------------------------------------------
# Slash command: /assign_clan_role
# ---------------------------------------------------------------------------
@bot.tree.command(name="assign_clan_role", description="Self-assign your clan role via select menu.")
async def assign_clan_role(interaction: discord.Interaction):
    """Allow members to pick a clan role matching configured clans."""
    _record_command_usage(interaction, "assign_clan_role")
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
@configure_dashboard.autocomplete("clan_name")
@configure_donation_metrics.autocomplete("clan_name")
@dashboard.autocomplete("clan_name")
@save_war_plan.autocomplete("clan_name")
@set_clan.autocomplete("clan_name")
@war_plan.autocomplete("clan_name")
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
