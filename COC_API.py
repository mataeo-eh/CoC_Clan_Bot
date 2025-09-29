import coc

server_config = {
    1412958253733646388:
    {
        "Clan_tag":
        {
            "Jesus_Saves": "#2GG82OG2U",
            "Christ_is_King_Clan_tag": "#2JU8CQCPJ"
        }
    }
}


class CoCAPI:
    def __init__(self, token):
        self.client = coc.Client()
        self.token = token

    async def login(self):
        await self.client.login(self.token) # pyright: ignore[reportCallIssue]

    async def get_player(self, tag):
        player = await self.client.get_player(tag)
        return {
            "name": player.name,
            "trophies": player.trophies,
            "town_hall": player.town_hall
        }

    def set_server_clan(self, guild_id: int, clan_name: str, tag: str):
        """Set or update a clan tag for a given server."""
        if guild_id not in server_config:
            server_config[guild_id] = {"Clan_tag": {}}
        server_config[guild_id]["Clan_tag"][clan_name] = tag