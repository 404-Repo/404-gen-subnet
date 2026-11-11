import httpx
from subnet_common.competition.generations import GenerationsAdapter, GenerationsMap


class SavedResults:
    def __init__(self, http: httpx.AsyncClient, cdn_url: str) -> None:
        self.http, self.cdn_url = http, cdn_url

    async def read(self, round_name: str, storage_prefix: str, hotkey: str) -> GenerationsMap:
        key = f"{storage_prefix}rounds/{round_name}/{hotkey}/submitted.json"
        response = await self.http.get(f"{self.cdn_url}/{key}", headers={"Cache-Control": "no-cache"})
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return GenerationsAdapter.validate_json(response.content)
