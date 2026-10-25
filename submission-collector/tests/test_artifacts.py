"""Private witness source delivery through the complete collection pipeline."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiobotocore.response import StreamingBody
from botocore.exceptions import ClientError
from subnet_common.competition.generations import GenerationResult, GenerationsAdapter
from subnet_common.competition.state import CompetitionState, RoundStage
from subnet_common.git_batcher import GitBatcher
from subnet_common.r2_client import R2Client
from subnet_common.testing import MockGitHubClient

from submission_collector.artifacts import (
    GRID_OPTIONS,
    ArtifactReadError,
    ArtifactSettings,
    WitnessArtifacts,
)
from submission_collector.download import DownloadPipeline
from submission_collector.settings import Settings


HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
IMAGE = b"frozen prompt image"
SOURCE = "export default function generate(THREE) {}\r\n// café\r\n".encode()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=")
PROFILE = {
    "image_digest": "sha256:" + "a" * 64,
    "validation_sha256": "b" * 64,
    "bootstrap_sha256": None,
}
COMMIT = "c" * 40


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class MemoryR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, bytes] = {}
        self.fail_public = False
        self.lose_manifest_ack = False
        self.lose_final_ack = False
        self.before_final_seal = None

    async def download(self, key: str, limit: int | None = None) -> bytes:
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        raw = self.objects[key]
        if limit is not None and len(raw) > limit:
            raise ValueError("Object exceeds the read limit")
        return raw

    async def create(self, key: str, data: bytes, content_type: str = "application/json") -> bool:
        created = await self.compare_and_swap(key, data, None, content_type)
        if created and self.lose_manifest_ack and key.endswith("images.json"):
            self.lose_manifest_ack = False
            raise OSError("Lost image-manifest acknowledgement")
        return created

    async def download_version(self, key: str, limit: int) -> tuple[bytes, str]:
        raw = await self.download(key, limit)
        return raw, digest(raw)

    async def compare_and_swap(
        self, key: str, data: bytes, expected: str | None, content_type="application/json"
    ) -> bool:
        if key.endswith("final.json") and self.before_final_seal is not None:
            callback, self.before_final_seal = self.before_final_seal, None
            callback()
        current = self.objects.get(key)
        if (digest(current) if current is not None else None) != expected:
            return False
        self.objects[key] = data
        if key.endswith("final.json") and self.lose_final_ack:
            self.lose_final_ack = False
            raise OSError("Lost final-seal acknowledgement")
        return True

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        if self.fail_public:
            raise OSError("Public bucket is unavailable")
        self.uploads[key] = data
        return key


class Collection:
    def __init__(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = ArtifactSettings(
            deployment="competition",
            round="test-1",
            bucket="private-witness",
            round_end=int(time.time()) - 600,
            profile=PROFILE,
            judging_profile=PROFILE,
            prompt_hashes=[digest(IMAGE)],
        )
        self.settings = settings.model_copy(update={"witness_artifacts": self.config})
        self.private, self.public = MemoryR2(), MemoryR2()
        self.git = MockGitHubClient()
        self.git.files["rounds/1/seed.json"] = '{"seed":42}'
        self.git.files["rounds/1/prompts.txt"] = "https://prompts.test/chair.png"
        self.git.files["rounds/1/submissions.json"] = json.dumps(
            {
                HOTKEY: {
                    "repo": "test/model",
                    "commit": COMMIT,
                    "revealed_at_block": 150,
                    "tie_breaker": "0" * 128,
                    "round": "test-1",
                }
            }
        )
        self.root = f"artifacts/v1/competition/test-1/{HOTKEY}/{digest(IMAGE)}"
        self.rendered: list[str] = []
        self.render_status = 200

        async def http(request: httpx.Request) -> httpx.Response:
            if request.url == "https://prompts.test/chair.png":
                return httpx.Response(200, content=IMAGE)
            assert request.url.host == "renderer.test"
            assert json.loads(request.content)["source"].encode() == SOURCE
            self.rendered.append(request.url.path)
            if self.render_status != 200:
                return httpx.Response(self.render_status, json={"error": "fixture_failure"})
            if request.url.path == "/render/grid":
                return httpx.Response(200, content=PNG)
            count = len(request.url.params["thetas"].split(","))
            return httpx.Response(200, json={"images": [base64.b64encode(PNG).decode()] * count})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(http))
        self.settings = self.settings.model_copy(update={"render_service_url": "https://renderer.test"})
        self.embeddings = AsyncMock(return_value=b"embedding bytes")
        monkeypatch.setattr("submission_collector.download.build_embeddings_npz", self.embeddings)

        self.choice, self.artifact = self.install()

    def install(self, *, output_none: bool = False, options: dict | None = None) -> tuple[dict, dict]:
        generation = {
            "round": "test-1",
            "prompt_sha256": digest(IMAGE),
            "seed": 42,
        }
        generation_id = digest(
            b"404-generate/v2\n" + canonical(generation)
        )
        source = {
            "key": f"{self.root}/sources/{digest(SOURCE)}.js",
            "sha256": digest(SOURCE),
            "size": len(SOURCE),
        }
        png = {
            "key": f"{self.root}/images/{digest(PNG)}.png",
            "sha256": digest(PNG),
            "size": len(PNG),
        }
        artifact_id = digest(
            canonical(
                {
                    "generation_id": generation_id,
                    "profile": digest(canonical(PROFILE)),
                    "request": {
                        "renderer_id": PROFILE["image_digest"],
                        "source_sha256": source["sha256"],
                        "options": options or GRID_OPTIONS,
                    },
                }
            )
        )
        artifact = {
            "version": 1,
            "scope_key": f"pod-gateway/v1/competition/test-1/{HOTKEY}.json",
            "generation": generation,
            "submission_commit": COMMIT,
            "artifact_id": artifact_id,
            "source": source,
            "png": png,
            "validation": "passed",
            "rejection_json": None,
            "profile": PROFILE,
            "options": options or GRID_OPTIONS,
        }
        choice = {
            "version": 1,
            "scope_key": artifact["scope_key"],
            "generation": generation,
            "submission_commit": COMMIT,
            "revision": "e" * 64,
            "operation_id": digest(
                canonical(
                    {
                        "generation": generation_id,
                        "source_sha256": None if output_none else source["sha256"],
                    }
                )
            ),
            "accepted_at": float(self.config.round_end - 1),
            "output_is_none": output_none,
            "artifact_id": None if output_none else artifact_id,
            "source": None if output_none else source,
        }
        self.private.objects[source["key"]] = SOURCE
        self.private.objects[png["key"]] = PNG
        self.private.objects[f"{self.root}/renders/{artifact_id}.json"] = canonical(artifact)
        self.private.objects[f"{self.root}/final.json"] = canonical(choice)
        return choice, artifact

    def save_choice(self) -> None:
        self.private.objects[f"{self.root}/final.json"] = canonical(self.choice)

    async def run(self):
        pipeline = DownloadPipeline(
            GitBatcher(git=self.git, base_sha="ref", branch=self.settings.github_branch),
            self.public,
            self.http,
            self.settings,
            artifacts=WitnessArtifacts(self.private, self.config),
        )
        return await pipeline.run(CompetitionState(current_round=1, stage=RoundStage.DOWNLOADING), "ref")

    def result(self):
        return GenerationsAdapter.validate_json(self.git.committed[f"rounds/1/{HOTKEY}/submitted.json"])["chair"]


@pytest.fixture
async def collection(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    instance = Collection(settings, monkeypatch)
    try:
        yield instance
    finally:
        await instance.http.aclose()


async def test_collects_exact_saved_source_and_reuses_only_matching_grid(
    collection: Collection,
) -> None:
    await collection.run()
    result = collection.result()
    assert result.js and result.views and result.failure_reason is None
    assert result.source_sha256 == digest(SOURCE) and result.submission_revision == "e" * 64
    assert collection.public.uploads[f"rounds/1/{HOTKEY}/submitted/chair.js"] == SOURCE
    assert len(collection.public.uploads) == 15
    assert collection.rendered == ["/render", "/render"]
    assert collection.embeddings.call_args.args[0] == IMAGE


@pytest.mark.parametrize("mismatch", ["lighting", "renderer"])
async def test_incompatible_grid_is_rendered_for_judging(collection: Collection, mismatch: str) -> None:
    if mismatch == "lighting":
        collection.choice, collection.artifact = collection.install(options={**GRID_OPTIONS, "lighting": "neutral"})
    else:
        profile = collection.config.judging_profile.model_copy(update={"image_digest": "sha256:" + "f" * 64})
        collection.config = collection.config.model_copy(update={"judging_profile": profile})
    await collection.run()
    assert collection.rendered.count("/render/grid") == 1
    assert collection.result().views


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", 43),
        ("commit", "d" * 40),
        ("round", "test-2"),
        ("late", 1),
        ("source", b"changed"),
        ("key", "another-miner/source.js"),
        ("png", b"changed"),
    ],
)
async def test_unresolved_provenance_never_becomes_a_miner_failure(collection: Collection, field: str, value) -> None:
    if field in ("seed", "round"):
        collection.choice["generation"][field] = value
    elif field == "commit":
        collection.choice["submission_commit"] = value
    elif field == "late":
        collection.choice["accepted_at"] = float(collection.config.round_end + value)
    elif field == "key":
        collection.choice["source"]["key"] = value
    else:
        collection.private.objects[collection.artifact[field]["key"]] = value
    collection.save_choice()
    with pytest.raises(ArtifactReadError):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed


@pytest.mark.parametrize("failure", ["embeddings", "public_upload", "lost_private_ack", "renderer"])
async def test_infrastructure_failure_is_retryable_and_saved_views_survive(
    collection: Collection, failure: str
) -> None:
    if failure == "embeddings":
        collection.embeddings.side_effect = RuntimeError("DINO unavailable")
    elif failure == "public_upload":
        collection.public.fail_public = True
    elif failure == "lost_private_ack":
        collection.private.lose_manifest_ack = True
    else:
        collection.render_status = 401
    with pytest.raises((OSError, RuntimeError, httpx.HTTPStatusError)):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed
    first_renders = len(collection.rendered)
    collection.embeddings.side_effect = None
    collection.public.fail_public = False
    collection.render_status = 200
    await collection.run()
    assert collection.result().views and collection.result().failure_reason is None
    if failure in ("embeddings", "lost_private_ack"):
        assert len(collection.rendered) == first_renders


async def test_canonical_validation_rejection_is_a_terminal_result(
    collection: Collection,
) -> None:
    collection.render_status = 422
    await collection.run()
    result = collection.result()
    assert result.js and result.views is None and result.failure_reason == "render_rejected"


@pytest.mark.parametrize("none", [True, False])
async def test_missing_and_declared_failures_do_not_select_old_renders(collection: Collection, none: bool) -> None:
    if none:
        collection.install(output_none=True)
    else:
        del collection.private.objects[f"{collection.root}/final.json"]
    await collection.run()
    result = collection.result()
    assert result.js is None and result.views is None
    assert result.failure_reason == ("generation_failed" if none else "not_submitted")
    assert not collection.rendered and not collection.public.uploads


async def test_collection_waits_for_witness_cutoff(collection: Collection) -> None:
    collection.config = collection.config.model_copy(update={"round_end": int(time.time())})
    with pytest.raises(ArtifactReadError, match="collection boundary"):
        await collection.run()
    assert not collection.public.uploads


async def test_sealed_absence_prevents_a_late_inflight_submission(collection: Collection) -> None:
    key = f"{collection.root}/final.json"
    del collection.private.objects[key]
    await collection.run()
    assert collection.result().failure_reason == "not_submitted"
    assert not await collection.private.compare_and_swap(key, canonical(collection.choice), None)
    saved = json.loads(collection.private.objects[key])
    assert saved["sealed"] and saved["missing"] and saved["accepted_at"] == 0


async def test_sealed_choice_prevents_a_late_inflight_replacement(collection: Collection) -> None:
    key = f"{collection.root}/final.json"
    old_version = digest(collection.private.objects[key])
    await collection.run()
    replacement = {**collection.choice, "revision": "f" * 64}
    assert not await collection.private.compare_and_swap(key, canonical(replacement), old_version)
    assert collection.result().source_sha256 == digest(SOURCE)
    assert json.loads(collection.private.objects[key])["sealed"]


async def test_collection_observes_the_final_choice_that_wins_the_sealing_race(collection: Collection) -> None:
    collection.private.before_final_seal = lambda: collection.install(output_none=True)
    await collection.run()
    assert collection.result().failure_reason == "generation_failed"
    assert collection.result().js is None and not collection.public.uploads


async def test_lost_seal_acknowledgement_recovers_the_same_choice(collection: Collection) -> None:
    collection.private.lose_final_ack = True
    with pytest.raises(OSError, match="seal acknowledgement"):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed
    await collection.run()
    assert collection.result().submission_revision == collection.choice["revision"]
    assert collection.result().source_sha256 == digest(SOURCE) and collection.result().views


async def test_failed_collection_joins_other_prompt_work_before_retry(
    collection: Collection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection.git.files["rounds/1/prompts.txt"] += "\nhttps://prompts.test/table.png"
    second_image = IMAGE + b"second"
    collection.config = collection.config.model_copy(update={"prompt_hashes": [digest(IMAGE), digest(second_image)]})
    started, stopped = asyncio.Event(), asyncio.Event()

    async def prompt(**kwargs):
        if kwargs["prompt"] == "chair":
            await started.wait()
            raise OSError("Storage is unavailable")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(
        DownloadPipeline, "_fetch_prompt_images", AsyncMock(return_value={"chair": IMAGE, "table": second_image})
    )
    monkeypatch.setattr(DownloadPipeline, "_fetch_render_upload", AsyncMock(side_effect=prompt))
    with pytest.raises(OSError):
        await collection.run()
    assert stopped.is_set()


async def test_bounded_r2_reads_handle_short_chunks_and_close_on_overflow() -> None:
    class Stream:
        def __init__(self):
            self.data = bytearray(b"exact bytes")
            self.closed = False
            self.content = SimpleNamespace(read=self.read_chunk)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def read(self):
            return bytes(self.data)

        async def read_chunk(self, amount):
            result = bytes(self.data[: min(amount, 2)])
            del self.data[: len(result)]
            return result

    stream = Stream()
    storage = object.__new__(R2Client)
    storage.bucket = "test"
    storage._client = SimpleNamespace(get_object=AsyncMock(return_value={"Body": StreamingBody(stream, 11)}))
    assert await storage.download("object", limit=20) == b"exact bytes"
    assert stream.closed
    stream = Stream()
    storage._client.get_object.return_value = {"Body": StreamingBody(stream, 11)}
    with pytest.raises(ValueError, match="size limit"):
        await storage.download("object", limit=3)
    assert stream.closed
    stream = Stream()
    storage._client.get_object.return_value = {"Body": StreamingBody(stream, 11), "ETag": "saved-version"}
    assert await storage.download_version("object", 20) == (b"exact bytes", "saved-version")
    assert stream.closed


async def test_skips_already_completed_prompts(collection: Collection) -> None:
    existing = {
        "chair": GenerationResult(js="https://assets.test/chair.js", views="https://assets.test/chair", size=100)
    }
    collection.git.files[f"rounds/1/{HOTKEY}/submitted.json"] = GenerationsAdapter.dump_json(existing).decode()
    assert await collection.run() == {HOTKEY: existing}
