"""Prepare and save the canonical judging bundle for an accepted final source."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
from renderer_client import RenderOptions
from subnet_common.embeddings import build_embeddings_npz
from subnet_common.render import GRAY_BG, GRAY_VIEWS, WHITE_BG, WHITE_VIEWS, render_grid, render_views
from witness_client.calls import canonical

from .artifacts import (
    MAX_MANIFEST_BYTES,
    MAX_PNG_BYTES,
    ArtifactPending,
    ArtifactRepository,
    FinalChoice,
    JudgingBundle,
    ObjectRef,
)
from .state import StateConflict

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE_NAMES = {f"white/{v.name}.png" for v in WHITE_VIEWS} | {f"gray/{v.name}.png" for v in GRAY_VIEWS} | {"grid.png"}


class Judging:
    def __init__(self, store: ArtifactRepository, endpoint: str, revision: str, hf_token: str | None) -> None:
        if not revision:
            raise ValueError("Judging requires a frozen embedding model revision")
        self.store, self.endpoint, self.revision, self.hf_token = store, endpoint, revision, hf_token

    def root(self, choice: FinalChoice) -> str:
        assert choice.source is not None
        binding = hashlib.sha256(
            canonical(
                {
                    "profile": self.store.profile.id,
                    "embedding_revision": self.revision,
                }
            )
        ).hexdigest()
        return f"{self.store.prefix(choice.generation.prompt_sha256)}/judging/{choice.source.sha256}/{binding}"

    def _load_bundle(self, choice: FinalChoice, root: str) -> JudgingBundle | None:
        raw = self.store.objects.get(f"{root}/bundle.json", MAX_MANIFEST_BYTES)
        if raw is None:
            return None
        bundle = JudgingBundle.model_validate_json(raw)
        if (
            bundle.scope_key != choice.scope_key
            or bundle.generation != choice.generation
            or bundle.submission_commit != choice.submission_commit
            or choice.source is None
            or bundle.source_sha256 != choice.source.sha256
            or bundle.profile != self.store.profile
            or bundle.embedding_revision != self.revision
        ):
            raise ArtifactPending("Saved judging bundle belongs to another input")
        if bundle.failure_reason == "render_rejected" and not bundle.files:
            return bundle
        if bundle.failure_reason is not None or bundle.files.keys() != IMAGE_NAMES | {"embeddings.npz"}:
            raise ArtifactPending("Saved judging bundle is incomplete")
        for name, ref in bundle.files.items():
            if ref.key != f"{root}/{name}" or ref.size > MAX_PNG_BYTES:
                raise ArtifactPending("Saved judging file is outside its scope")
            raw = self.store.read_object(ref, MAX_PNG_BYTES)
            if name.endswith(".png") and not raw.startswith(PNG_SIGNATURE):
                raise ArtifactPending("Saved judging view is not a PNG")
        return bundle

    def _load_images(self, root: str) -> dict[str, bytes] | None:
        raw = self.store.objects.get(f"{root}/images.json", MAX_MANIFEST_BYTES)
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.keys() != IMAGE_NAMES:
            raise ArtifactPending("Saved judging images are incomplete")
        images = {}
        for name, data in value.items():
            ref = ObjectRef.model_validate(data)
            if ref.key != f"{root}/objects/{ref.sha256}.png" or ref.size > MAX_PNG_BYTES:
                raise ArtifactPending("Saved judging image is outside its scope")
            images[name] = self.store.read_object(ref, MAX_PNG_BYTES)
            if not images[name].startswith(PNG_SIGNATURE):
                raise ArtifactPending("Saved judging image is not a PNG")
        return images

    def _save_images(self, root: str, images: dict[str, bytes]) -> dict[str, bytes]:
        if images.keys() != IMAGE_NAMES:
            raise ArtifactPending("Judging renderer did not produce every view")
        refs = {}
        for name, data in images.items():
            if len(data) > MAX_PNG_BYTES or not data.startswith(PNG_SIGNATURE):
                raise ArtifactPending("Judging renderer returned an invalid PNG")
            digest = hashlib.sha256(data).hexdigest()
            refs[name] = self.store._put(f"{root}/objects/{digest}.png", data).model_dump()
        try:
            self.store._save(f"{root}/images.json", canonical(refs), None)
        except StateConflict:
            pass
        saved = self._load_images(root)
        if saved is None:
            raise ArtifactPending("Judging image publication is unresolved")
        return saved

    def _load_embeddings(self, root: str) -> bytes | None:
        raw = self.store.objects.get(f"{root}/embedding.json", MAX_MANIFEST_BYTES)
        if raw is None:
            return None
        ref = ObjectRef.model_validate_json(raw)
        if ref.key != f"{root}/objects/{ref.sha256}.npz" or ref.size > MAX_PNG_BYTES:
            raise ArtifactPending("Saved embeddings are outside their scope")
        return self.store.read_object(ref, MAX_PNG_BYTES)

    def _save_embeddings(self, root: str, data: bytes) -> bytes:
        digest = hashlib.sha256(data).hexdigest()
        ref = self.store._put(f"{root}/objects/{digest}.npz", data)
        try:
            self.store._save(f"{root}/embedding.json", ref.model_dump_json().encode(), None)
        except StateConflict:
            pass
        saved = self._load_embeddings(root)
        if saved is None:
            raise ArtifactPending("Embedding publication is unresolved")
        return saved

    def _publish(self, choice: FinalChoice, root: str, files: dict[str, ObjectRef], rejected: bool = False) -> None:
        assert choice.source is not None
        bundle = JudgingBundle(
            scope_key=choice.scope_key,
            generation=choice.generation,
            submission_commit=choice.submission_commit,
            source_sha256=choice.source.sha256,
            profile=self.store.profile,
            embedding_revision=self.revision,
            files=files,
            failure_reason="render_rejected" if rejected else None,
        )
        try:
            self.store._save(f"{root}/bundle.json", bundle.model_dump_json().encode(), None)
        except StateConflict:
            pass
        if self._load_bundle(choice, root) is None:
            raise ArtifactPending("Judging bundle publication is unresolved")

    def ready(self, choice: FinalChoice) -> bool:
        return self._load_bundle(choice, self.root(choice)) is not None

    async def prepare(self, choice: FinalChoice) -> None:
        if choice.source is None:
            return
        root = self.root(choice)
        if await asyncio.to_thread(self._load_bundle, choice, root) is not None:
            return
        _, source, artifact = await asyncio.to_thread(self.store.collect_final, choice.generation.prompt_sha256)
        if source is None or artifact is None or artifact.source != choice.source:
            raise ArtifactPending("Selected final source changed")
        images = await asyncio.to_thread(self._load_images, root)
        if images is None:
            try:
                async with httpx.AsyncClient(timeout=150, trust_env=False) as client:
                    white = await render_views(
                        client,
                        self.endpoint,
                        source,
                        WHITE_VIEWS,
                        WHITE_BG,
                        choice.generation.id,
                        raise_errors=True,
                    )
                    gray = await render_views(
                        client,
                        self.endpoint,
                        source,
                        GRAY_VIEWS,
                        GRAY_BG,
                        choice.generation.id,
                        raise_errors=True,
                    )
                    grid: bytes | None
                    if artifact.options == RenderOptions() and artifact.png is not None:
                        grid = await asyncio.to_thread(self.store.read_object, artifact.png, MAX_PNG_BYTES)
                    else:
                        grid = await render_grid(client, self.endpoint, source, choice.generation.id, raise_errors=True)
                if white is None or gray is None or grid is None:
                    raise ArtifactPending("Judging renderer did not produce a complete bundle")
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 422:
                    raise
                await asyncio.to_thread(self._publish, choice, root, {}, True)
                return
            images = await asyncio.to_thread(
                self._save_images,
                root,
                {f"white/{name}.png": data for name, data in white.items()}
                | {f"gray/{name}.png": data for name, data in gray.items()}
                | {"grid.png": grid},
            )
        prompt = await asyncio.to_thread(
            self.store.objects.get,
            f"{self.store.prefix(choice.generation.prompt_sha256)}/prompt",
            8 * 1024 * 1024,
        )
        if prompt is None or hashlib.sha256(prompt).hexdigest() != choice.generation.prompt_sha256:
            raise ArtifactPending("Saved prompt image is missing or changed")
        embeddings = await asyncio.to_thread(self._load_embeddings, root)
        if embeddings is None:
            embeddings = await build_embeddings_npz(
                prompt,
                {v.name: images[f"white/{v.name}.png"] for v in WHITE_VIEWS},
                choice.generation.id,
                revision=self.revision,
                hf_token=self.hf_token,
            )
            if not embeddings or len(embeddings) > MAX_PNG_BYTES:
                raise ArtifactPending("Judging embeddings exceed their size limit")
            embeddings = await asyncio.to_thread(self._save_embeddings, root, embeddings)
        files = {}
        for name, data in (images | {"embeddings.npz": embeddings}).items():
            key = f"{root}/{name}"
            await asyncio.to_thread(
                self.store.objects.put,
                key,
                data,
                "image/png" if name.endswith(".png") else "application/octet-stream",
            )
            files[name] = ObjectRef(key=key, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        await asyncio.to_thread(self._publish, choice, root, files)
