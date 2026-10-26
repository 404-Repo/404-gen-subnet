"""Save JavaScript, judging views and embeddings for a final result."""

from __future__ import annotations

import asyncio

import httpx
from renderer_client import RenderRejected
from subnet_common.competition.generations import GenerationResult
from subnet_common.embeddings import build_embeddings_npz
from subnet_common.render import GRAY_BG, GRAY_VIEWS, WHITE_BG, WHITE_VIEWS, render_grid, render_views

from .artifacts import ArtifactRepository


class Judging:
    def __init__(self, store: ArtifactRepository, endpoint: str, revision: str, hf_token: str | None) -> None:
        if not revision:
            raise ValueError("Judging requires a frozen embedding model revision")
        self.store, self.endpoint, self.revision, self.hf_token = store, endpoint, revision, hf_token

    async def save(self, prompt: str, source: str | None, image: bytes | None) -> GenerationResult:
        root = self.store.prefix(prompt)
        objects = self.store.objects
        data = source.encode() if source is not None else None
        if data is not None and len(data) > 1024 * 1024:
            raise ValueError("Source exceeds 1 MiB")
        result = GenerationResult(failure_reason="generation_failed")
        if data is not None:
            await asyncio.to_thread(objects.put, f"{root}.js", data, "text/javascript")
            result = GenerationResult(js=f"{root}.js", size=len(data))
            try:
                async with httpx.AsyncClient(timeout=150, trust_env=False) as client:
                    white = await render_views(
                        client, self.endpoint, data, WHITE_VIEWS, WHITE_BG, root, raise_errors=True
                    )
                    gray = await render_views(client, self.endpoint, data, GRAY_VIEWS, GRAY_BG, root, raise_errors=True)
                    grid = await render_grid(client, self.endpoint, data, root, raise_errors=True)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 422:
                    raise
                rejection = RenderRejected(error.response.text)
                result.failure_reason = "render_rejected"
                await asyncio.to_thread(self.store.update_result, prompt, result)
                raise rejection from error
            if white is None or gray is None or grid is None or image is None:
                raise RuntimeError("Renderer did not produce every judging view")
            for name, content in (
                {f"white/{name}.png": content for name, content in white.items()}
                | {f"gray/{name}.png": content for name, content in gray.items()}
                | {"grid.png": grid}
            ).items():
                await asyncio.to_thread(objects.put, f"{root}/{name}", content, "image/png")
            embeddings = await build_embeddings_npz(image, white, root, revision=self.revision, hf_token=self.hf_token)
            if not embeddings:
                raise RuntimeError("Embedding calculation returned no data")
            await asyncio.to_thread(objects.put, f"{root}/embeddings.npz", embeddings)
            result.views = root
        await asyncio.to_thread(self.store.update_result, prompt, result)
        return result
