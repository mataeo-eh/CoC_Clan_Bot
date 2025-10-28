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
            "name": player.name,
            "trophies": player.trophies,
            "town_hall": player.town_hall
        }
        log.debug("CoCAPI.get_clan_war_info returning payload")
        return data

    def set_server_clan(self, guild_id: int, clan_name: str, tag: str, alerts_enabled: bool = True):
        log.debug("CoCAPI.set_server_clan invoked")
        # Set or update a clan tag for a given server and persist alert preference.
        guild_config = server_config.setdefault(
            guild_id,
            {"Clan tags": {}, "Player tags": {}, "Enable Alert Tracking": {}},
        )
        clan_tags = guild_config.setdefault("Clan tags", {})
        clan_tags[clan_name] = tag
        alert_map = guild_config.setdefault("Enable Alert Tracking", {})
        alert_map[clan_name] = alerts_enabled
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
        clan_tags = guild_config.get("Clan tags")

        if not clan_tags:
            raise ClanNotConfiguredError(f"No clan tags configured for guild {guild_id}.")

        if clan_name not in clan_tags:
            raise ClanNotConfiguredError(f"Clan '{clan_name}' not configured for guild {guild_id}.")

        tag = clan_tags[clan_name]
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
    

    
