from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import tasks

import coc

from bot_core import bot, client
from logger import get_logger, log_command_call

log = get_logger()
from COC_API import ClanNotConfiguredError, GuildNotConfiguredError
from ENV.Clan_Configs import server_config


MAX_MESSAGE_LENGTH = 1900
ALERT_ROLE_NAME = "War Alerts"
# Matches the poll frequency of the background alert loop (5 minutes).
ALERT_WINDOW_SECONDS = 300

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

    # Persist the clan tag alongside the chosen alert preference.
    client.set_server_clan(interaction.guild.id, clan_name, tag, alerts_enabled=enable_alerts)
    await send_text_response(
        interaction,
        (
            f"✅ `{clan_name}` now points to {tag} for this server.\n"
            f"📣 War alerts enabled: {'Yes' if enable_alerts else 'No'}."
        ),
        ephemeral=True,
    )


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
        lines.append("Use the dropdown below to choose the details you want to view.")
        return "\n".join(lines)

    for key in selections:
        label = WAR_INFO_FIELD_MAP.get(key, key.title())
        value = _format_war_value(key, war_info.get(key))
        lines.append(f"**{label}:**\n{value}")
    return "\n\n".join(lines)


def _clan_names_for_guild(guild_id: int) -> Dict[str, str]:
    """Return a mapping of clan name -> tag for a guild."""
    log.debug("_clan_names_for_guild called")
    guild_config = server_config.get(guild_id, {})
    return guild_config.get("Clan tags", {}) or {}


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




def _parse_assignment_string(definition: str) -> Dict[int, List[int]]:
    """Parse admin assignment input into a mapping of home -> enemy bases."""
    log.debug("_parse_assignment_string invoked")
    mapping: Dict[int, List[int]] = {}
    if not definition.strip():
        raise ValueError("Assignments cannot be empty.")
    blocks = definition.replace("\n", ";").split(";")
    for block in blocks:
        chunk = block.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError("Each assignment must use the format base:target")
        left, right = chunk.split(":", 1)
        try:
            home = int(left.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid home base number '{left}'.") from exc
        if home in mapping:
            raise ValueError(f"Duplicate assignment for base {home}.")
        targets: List[int] = []
        for part in right.split(','):
            data = part.strip()
            if not data:
                continue
            try:
                targets.append(int(data))
            except ValueError as exc:
                raise ValueError(f"Invalid enemy base number '{data}'.") from exc
        if not targets:
            raise ValueError(f"Base {home} must list at least one target.")
        if len(targets) > 2:
            raise ValueError(f"Base {home} can only receive up to two targets.")
        mapping[home] = targets
    if not mapping:
        raise ValueError("No valid assignments were provided.")
    #if len(mapping) > 15:
        #raise ValueError("You can assign at most 15 bases per command.")
    return mapping

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

        channel = _find_alert_channel(guild)
        if channel is None:
            continue  # No writable channel to post alerts

        clan_tags = config.get("Clan tags", {})
        alert_map = config.get("Enable Alert Tracking", {})
        if not clan_tags:
            continue  # Nothing configured for this guild

        alert_role = discord.utils.get(guild.roles, name=ALERT_ROLE_NAME)

        for clan_name, tag in clan_tags.items():
            if not alert_map.get(clan_name, True):
                continue  # Admins disabled tracking for this clan
            try:
                war = await client.get_clan_war_raw(tag)
            except (coc.errors.PrivateWarLog, coc.errors.NotFound, coc.errors.GatewayError):
                continue  # Skip clans without accessible war data
            except Exception:
                continue  # Fail-safe for unexpected library errors

            for alert in _collect_war_alerts(guild, clan_name, tag, war, alert_role, now):
                await send_channel_message(channel, alert)


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

@bot.tree.command(name="assign_bases", description="Assign enemy bases to clan members.")
@app_commands.describe(
    clan_name="Configured clan currently in war",
    assignments="Use the format 1:1,2;2:3 to pair home bases with enemy targets",
)
async def assign_bases(interaction: discord.Interaction, clan_name: str, assignments: str):
    """Allow administrators to map home clan members to their attack targets."""
    log_command_call("assign_bases")
    log.debug("assign_bases invoked")
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

    try:
        parsed = _parse_assignment_string(assignments)
    except ValueError as exc:
        await send_text_response(interaction, f"⚠️ {exc}", ephemeral=True)
        return

    log.debug('assign_bases parsed assignments: %s', parsed)

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

    sorted_home = [m for m in sorted(war.clan.members, key=lambda m: getattr(m, "map_position", 0)) if getattr(m, "map_position", None) is not None]
    sorted_enemy = [m for m in sorted(war.opponent.members, key=lambda m: getattr(m, "map_position", 0)) if getattr(m, "map_position", None) is not None]
    home_members = {m.map_position: m for m in sorted_home}
    max_home = len(sorted_home)
    max_enemy = len(sorted_enemy)

    log.debug('assign_bases available home bases: %s', sorted(home_members.keys()))

    entries: List[tuple[int, str, List[int]]] = []
    for home_base, targets in parsed.items():
        member_obj = home_members.get(home_base)
        if member_obj is None or home_base < 1 or home_base > max_home:
            await send_text_response(
                interaction,
                f"⚠️ Home base {home_base} is not present in the current war.",
                ephemeral=True,
            )
            return
        member_name = getattr(member_obj, "name", f"Base {home_base}")

        for enemy_base in targets:
            if enemy_base < 1 or enemy_base > max_enemy:
                await send_text_response(
                    interaction,
                    f"⚠️ Enemy base {enemy_base} is not present in the current war.",
                    ephemeral=True,
                )
                return
        log.debug('assign_bases resolved home %%s -> targets %%s', home_base, targets)
        entries.append((home_base, member_name, targets))

    entries.sort(key=lambda item: item[0])
    log.debug('assign_bases sorted entries: %s', entries)
    lines: List[str] = []  # Final formatted lines for the summary output
    for display_base, member_name, targets in entries:
        target_text = " and ".join(str(num) for num in targets)
        lines.append(f"[{display_base}] {member_name}: {target_text}")

    output = "\n".join(lines)

    alert_role = discord.utils.get(interaction.guild.roles, name=ALERT_ROLE_NAME)
    mention = f"{alert_role.mention} " if alert_role else ""
    content = f"{mention}Assignments for `{clan_name}`\n{output}".strip()
    print(f'[assign_bases] final content:\n{content}', flush=True)

    if interaction.channel and interaction.channel.permissions_for(interaction.guild.me).send_messages:
        for chunk in _chunk_content(content):
            await interaction.channel.send(chunk)
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("✅ War targets broadcast to the channel.", ephemeral=True)
    else:
        await send_text_response(
            interaction,
            content,
            ephemeral=False,
        )


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
            "Select the clan whose role you want to adopt. "
            "Buttons control visibility of the confirmation."
        ),
        ephemeral=True,
        view=view,
    )


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

@clan_war_info_menu.autocomplete("clan_name")
@assign_bases.autocomplete("clan_name")
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
