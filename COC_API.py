import coc

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
