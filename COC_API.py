import coc
from ENV.Clan_Configs import server_config, save_server_config
from logger import get_logger

log = get_logger()


class GuildNotConfiguredError(Exception):
    """Raised when a Discord guild has no stored configuration."""


class ClanNotConfiguredError(Exception):
    """Raised when a requested clan name is not configured for a guild."""


class CoCAPI:
    def __init__(self, token):
        log.debug("CoCAPI initialised")
        self.client = coc.Client()
        self.token = token

    async def login(self):
        log.debug("CoCAPI.login invoked")
        await self.client.login_with_tokens(self.token)
        log.debug("CoCAPI.login completed")

    async def get_player(self, tag):
        log.debug("CoCAPI.get_player invoked")
        player = await self.client.get_player(tag)
        log.debug("CoCAPI.get_player fetched data")
        data = {
            "profile": {
                "name": player.name,
                "tag": player.tag,
                "exp_level": player.exp_level,
                "town_hall_level": getattr(player, "town_hall", None),
                "builder_hall_level": getattr(player, "builder_hall_level", None),
            },
            "clan": {
                "name": player.clan.name if player.clan else None,
                "tag": player.clan.tag if player.clan else None,
                "role": player.role,
            },
            "league": player.league.name if player.league else None,
            "trophies": player.trophies,
            "best_trophies": getattr(player, "best_trophies", None),
            "versus_trophies": getattr(player, "versus_trophies", None),
            "war_stars": getattr(player, "war_stars", None),
            "attack_wins": getattr(player, "attack_wins", None),
            "defense_wins": getattr(player, "defense_wins", None),
            "donations": getattr(player, "donations", None),
            "donations_received": getattr(player, "donations_received", None),
            "heroes": [
                {
                    "name": hero.name,
                    "level": hero.level,
                    "max_level": hero.max_level,
                    "village": hero.village,
                }
                for hero in getattr(player, "heroes", [])
            ],
            "troops": [
                {
                    "name": troop.name,
                    "level": troop.level,
                    "max_level": troop.max_level,
                    "village": troop.village,
                }
                for troop in getattr(player, "troops", [])
            ],
            "spells": [
                {
                    "name": spell.name,
                    "level": spell.level,
                    "max_level": spell.max_level,
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
        """Fetch the live war object for a clan tag."""
        log.debug("CoCAPI.get_clan_war_raw invoked")
        result = await self.client.get_clan_war(tag)
        log.debug("CoCAPI.get_clan_war_raw fetched data")
        return result

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
        clan = await self.client.get_clan_war(tag)
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
            "all accounts in war": clan.members,
            "Clan members in war": clan.clan.members
        }
        log.debug("CoCAPI.get_clan_war_info returning payload")
        return data
    

    
