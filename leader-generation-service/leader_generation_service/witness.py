import httpx
from subnet_common.competition.generations import GenerationsMap, read_submitted, submitted_url


class SavedResults:
    def __init__(self, http: httpx.AsyncClient, cdn_url: str) -> None:
        self.http, self.cdn_url = http, cdn_url

    async def read(self, round_name: str, storage_prefix: str, hotkey: str) -> GenerationsMap:
        return await read_submitted(self.http, submitted_url(self.cdn_url, round_name, storage_prefix, hotkey))
