import asyncio
from typing import Any, Dict, List, Optional

import coc
from coc.enums import WarRound
from Clan_Configs import server_config, save_server_config
from logger import get_logger

log = get_logger()


class GuildNotConfiguredError(Exception):
    """Raised when a Discord guild has no stored configuration."""


class ClanNotConfiguredError(Exception):
    """Raised when a requested clan name is not configured for a guild."""

class notinWar(Exception):
    "Raised when war.state is notinWar/warEnded"

class CoCAPI:
    def __init__(self, token):
        log.debug("CoCAPI initialised")
        self.client: Optional[coc.Client] = None
        self.token = token

    def _require_client(self) -> coc.Client:
        if self.client is None:
            raise RuntimeError("Clash of Clans client not initialised. Call login() first.")
        return self.client

    async def login(self):
        log.debug("CoCAPI.login invoked")
        loop = asyncio.get_running_loop()
        if self.client is not None:
            await self.client.close()
        self.client = coc.Client(loop=loop)
        await self.client.login_with_tokens(self.token)
        log.debug("CoCAPI.login completed")

    async def get_player(self, tag):
        log.debug("CoCAPI.get_player invoked")
        player = await self._require_client().get_player(tag)
        log.debug("CoCAPI.get_player fetched data")
        data = {
            "profile": {
                "name": player.name,
                "tag": player.tag,
                "exp_level": player.exp_level,
                "town_hall_level": getattr(player, "town_hall", None),
                "town_hall_weapon_level": getattr(player, "town_hall_weapon", None),
                "builder_hall_level": getattr(player, "builder_hall", None),
                "legend_statistics": getattr(player, "legend_statistics", None),
            },
            "clan": {
                "name": player.clan.name if player.clan else None,
                "tag": player.clan.tag if player.clan else None,
                "role": getattr(player, "role", None),
            },
            "league": {
                "name": player.league.name if player.league else None,
                "id": player.league.id if player.league else None,
                "icon": player.league.icon if player.league else None,
            },
            "trophies": player.trophies,
            "best_trophies": getattr(player, "best_trophies", None),
            "versus_trophies": getattr(player, "builder_base_trophies", None),
            'best_builder_base_trophies': getattr(player, "best_builder_base_trophies", None),
            "war_stars": getattr(player, "war_stars", None),
            "attack_wins": getattr(player, "attack_wins", None),
            "defense_wins": getattr(player, "defense_wins", None),
            "donations": getattr(player, "donations", None),
            "donations_received": getattr(player, "received", None),
            "heroes": [
                {
                    "name ": hero.name,
                    "level ": hero.level,
                    "max_level ": hero.max_level,
                    "village ": hero.village,
                }
                for hero in getattr(player, "heroes", [])
            ],
            "troops": [
                {
                    "name ": troop.name,
                    "level ": troop.level,
                    "max_level ": troop.max_level,
                    "village ": troop.village,
                }
                for troop in getattr(player, "troops", [])
            ],
            "spells": [
                {
                    "name ": spell.name,
                    "level ": spell.level,
                    "max_level ": spell.max_level,
                }
                for spell in getattr(player, "spells", [])
            ],
            "achievements": [
                {
                    "name": achievement.name,
                    "stars": achievement.stars,
                    "value": achievement.value,
                    "target": achievement.target,
                    "info": achievement.info,
                }
                for achievement in getattr(player, "achievements", [])
            ],
        }
        log.debug("CoCAPI.get_player returning payload")
        return data

    async def get_clan(self, tag: str):
        """Fetch a clan profile object for a clan tag."""
        log.debug("CoCAPI.get_clan invoked")
        clan = await self._require_client().get_clan(tag)
        log.debug("CoCAPI.get_clan fetched data")
        return clan

    def set_server_clan(self, guild_id: int, clan_name: str, tag: str, alerts_enabled: bool = True):
        log.debug("CoCAPI.set_server_clan invoked")
        normalised_tag = tag.upper()
        guild_config = server_config.setdefault(
            guild_id,
            {"clans": {}, "player_tags": {}},
        )
        clans = guild_config.setdefault("clans", {})
        clan_entry = clans.setdefault(
            clan_name,
            {"tag": normalised_tag, "alerts": {"enabled": alerts_enabled, "channel_id": None}},
        )
        clan_entry["tag"] = normalised_tag
        alerts = clan_entry.setdefault("alerts", {})
        alerts["enabled"] = alerts_enabled
        alerts.setdefault("channel_id", None)
        save_server_config()
        log.debug("CoCAPI.set_server_clan persisted configuration")

    async def get_clan_war_raw(self, tag: str):
        """Fetch the regular current-war endpoint for a clan tag, excluding CWL."""
        log.debug("CoCAPI.get_clan_war_raw invoked")
        result = await self._require_client().get_clan_war(tag)
        log.debug("CoCAPI.get_clan_war_raw fetched data")
        return result

    async def get_active_war_raw(self, tag: str):
        """Fetch the current war context for a clan tag, including CWL rounds."""
        log.debug("CoCAPI.get_active_war_raw invoked")
        client = self._require_client()
        war = await client.get_current_war(tag)
        if war is None:
            log.debug("CoCAPI.get_active_war_raw: no current war, checking current preparation round")
            war = await client.get_current_war(
                tag,
                cwl_round=WarRound.current_preparation,
            )
        if war is None:
            log.debug("CoCAPI.get_active_war_raw: no preparation war, checking previous round")
            war = await client.get_current_war(
                tag,
                cwl_round=WarRound.previous_war,
            )
        log.debug(
            "CoCAPI.get_active_war_raw fetched war: is_cwl=%s state=%s",
            getattr(war, "is_cwl", None),
            getattr(war, "state", None) if war else None,
        )
        return war

    async def _build_cwl_member_totals(self, war) -> List[Dict[str, Any]]:
        """Aggregate attacks and stars for each member across CWL rounds played so far."""
        if not getattr(war, "is_cwl", False):
            return []

        league_group = getattr(war, "league_group", None)
        clan_tag = getattr(war, "clan_tag", None)
        if league_group is None or not clan_tag:
            return []

        totals: Dict[str, Dict[str, Any]] = {}
        async for league_war in league_group.get_wars_for_clan(clan_tag):
            state_value = getattr(getattr(league_war, "state", None), "value", getattr(league_war, "state", None))
            if state_value == "preparation":
                continue

            attacks_per_member = getattr(league_war, "attacks_per_member", 1) or 1
            for member in getattr(getattr(league_war, "clan", None), "members", []):
                member_tag = getattr(member, "tag", None)
                if not member_tag:
                    continue

                attacks = list(getattr(member, "attacks", []) or [])
                stars_earned = sum(getattr(attack, "stars", 0) for attack in attacks)
                entry = totals.setdefault(
                    member_tag,
                    {
                        "tag": member_tag,
                        "name": getattr(member, "name", "Unknown"),
                        "town_hall": getattr(member, "town_hall", "?"),
                        "rounds_rostered": 0,
                        "attacks_used": 0,
                        "attacks_available": 0,
                        "stars_earned": 0,
                        "max_stars": 0,
                    },
                )
                entry["name"] = getattr(member, "name", entry["name"])
                entry["town_hall"] = getattr(member, "town_hall", entry["town_hall"])
                entry["rounds_rostered"] += 1
                entry["attacks_used"] += len(attacks)
                entry["attacks_available"] += attacks_per_member
                entry["stars_earned"] += stars_earned
                entry["max_stars"] += attacks_per_member * 3

        ordered = sorted(
            totals.values(),
            key=lambda item: (
                -item["attacks_available"],
                -item["attacks_used"],
                -item["stars_earned"],
                str(item["name"]).casefold(),
                str(item["tag"]).casefold(),
            ),
        )
        log.debug("CoCAPI._build_cwl_member_totals aggregated %d members", len(ordered))
        return ordered

    async def get_clan_war_info(self, clan_name, guild_id):
        log.debug("CoCAPI.get_clan_war_info invoked")
        if guild_id not in server_config:
            raise GuildNotConfiguredError(f"Guild {guild_id} has no stored configuration.")

        guild_config = server_config[guild_id]
        clans = guild_config.get("clans", {})

        if not clans:
            raise ClanNotConfiguredError(f"No clan tags configured for guild {guild_id}.")

        if clan_name not in clans:
            raise ClanNotConfiguredError(f"Clan '{clan_name}' not configured for guild {guild_id}.")

        tag = clans[clan_name].get("tag")
        if not tag:
            raise ClanNotConfiguredError(f"Clan '{clan_name}' has no tag configured.")
        clan = await self.get_active_war_raw(tag)
        if clan is None:
            raise notinWar(f"Clan '{clan_name}' is not currently in an active war.")
        cwl_member_totals = await self._build_cwl_member_totals(clan)
        log.debug("CoCAPI.get_clan_war_info fetched war data")
        data = {
            "home clan": clan.clan,
            "opponent clan": clan.opponent,
            "clan tag": clan.clan_tag,
            "war tag": clan.war_tag,
            "war state": clan.state,
            "war status": clan.status or clan.state,
            "war type": clan.type,
            "is cwl": clan.is_cwl,
            "war size": clan.team_size,
            "attacks per member": clan.attacks_per_member,
            "all attacks done this war": clan.attacks,
            "battle modifier": clan.battle_modifier,
            "preparation start time": clan.preparation_start_time,
            "war day start time": clan.start_time,
            "war end time": clan.end_time,
            "league group": clan.league_group,
            "clan members in war": clan.clan.members,
            "all members in war": clan.members,
            "members with unused attacks this war": clan.clan.members,
            "members with no attacks this war": clan.clan.members,
            "member stars this war": clan.clan.members,
            "member attack summaries": clan.clan.members,
            "member stars this cwl": cwl_member_totals,
        }
        log.debug("CoCAPI.get_clan_war_info returning payload")
        return data
    

    
