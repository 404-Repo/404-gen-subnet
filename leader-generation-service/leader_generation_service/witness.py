from pathlib import Path

import httpx
from bittensor_wallet import Keypair
from pydantic import BaseModel
from subnet_common.competition.generations import GenerationsAdapter, GenerationsMap
from subnet_common.witness.rounds import RoundInputs
from witness_client import AsyncWitnessClient

from .models import Snapshot


class Context(BaseModel):
    round_inputs: RoundInputs
    saving_enabled: bool


class WitnessInputs:
    def __init__(self, url: str, hotkey: Keypair, parameters: Path) -> None:
        self.url, self.hotkey, self.parameters = url, hotkey, parameters

    async def read(self) -> RoundInputs:
        witness = AsyncWitnessClient(
            {"witness_url": self.url, "proof_parameters": str(self.parameters)},
            hotkey=self.hotkey,
            subscription="codex",
        )
        try:
            async with httpx.AsyncClient(transport=witness.renderer_transport(), trust_env=False) as client:
                response = await client.get("https://witness.pod/v1/artifacts/context")
                response.raise_for_status()
                context = Context.model_validate_json(response.content)
                if not context.saving_enabled or not context.round_inputs.save_submissions:
                    raise ValueError("Witness final-result saving is disabled")
                return context.round_inputs
        finally:
            await witness.aclose()


class SavedResults:
    def __init__(self, http: httpx.AsyncClient, cdn_url: str) -> None:
        self.http, self.cdn_url = http, cdn_url

    async def read(self, snapshot: Snapshot) -> GenerationsMap:
        inputs = snapshot.inputs
        key = f"{inputs.storage_prefix}rounds/{inputs.round}/{snapshot.hotkey}/submitted.json"
        response = await self.http.get(f"{self.cdn_url}/{key}", headers={"Cache-Control": "no-cache"})
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return GenerationsAdapter.validate_json(response.content)
