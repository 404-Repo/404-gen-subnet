"""Public-bucket witness artifacts through the complete collection pipeline."""

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
    ArtifactReadError,
    RoundArtifacts,
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
        self.lose_final_ack = False
        self.before_final_seal = None

    async def download(self, key: str, limit: int | None = None) -> bytes:
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        raw = self.objects[key]
        if limit is not None and len(raw) > limit:
            raise ValueError("Object exceeds the read limit")
        return raw

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



class Collection:
    def __init__(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        self.round = RoundArtifacts(
            deployment="competition",
            round="test-1",
            round_end=int(time.time()) - 600,
            profile=PROFILE,
            embedding_revision="fixture-revision",
            seed=42,
            prompt_hashes_sha256=digest(canonical([digest(IMAGE)])),
        )
        self.settings = settings
        self.r2 = MemoryR2()
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
        self.git.files["config.json"] = json.dumps({
            "name": "test", "description": "fixture", "first_evaluation_date": "2025-01-01",
            "last_competition_date": "2025-12-31", "generation_stage_minutes": 180,
            "win_margin": 0.05, "prompts_per_round": 1, "carryover_prompts": 0,
        })

        async def http(request: httpx.Request) -> httpx.Response:
            if request.url == "https://prompts.test/chair.png":
                return httpx.Response(200, content=IMAGE)
            return httpx.Response(404)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(http))

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
                        "options": options or {},
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
            "options": options or {},
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
            "accepted_at": float(self.round.round_end - 1),
            "output_is_none": output_none,
            "artifact_id": None if output_none else artifact_id,
            "source": None if output_none else source,
        }
        self.r2.objects[source["key"]] = SOURCE
        self.r2.objects[png["key"]] = PNG
        self.r2.objects[f"{self.root}/renders/{artifact_id}.json"] = canonical(artifact)
        self.r2.objects[f"{self.root}/final.json"] = canonical(choice)
        binding = digest(canonical({
            "profile": digest(canonical(PROFILE)), "embedding_revision": self.round.embedding_revision,
        }))
        self.bundle_root = f"{self.root}/judging/{source['sha256']}/{binding}"
        names = {f"white/{name}.png" for name in (
            "front_left", "front_right", "front_below", "front_above", "right", "back", "left", "top_down",
        )} | {f"gray/{name}.png" for name in ("front_left", "front_right", "front_below", "front_above")} | {
            "grid.png", "embeddings.npz",
        }
        self.bundle_files = {}
        for name in names:
            data = b"fixture embeddings" if name == "embeddings.npz" else PNG
            key = f"{self.bundle_root}/{name}"
            self.r2.objects[key] = data
            self.bundle_files[name] = {"key": key, "sha256": digest(data), "size": len(data)}
        self.bundle = {
            "version": 1, "scope_key": choice["scope_key"], "generation": generation,
            "submission_commit": COMMIT, "source_sha256": source["sha256"], "profile": PROFILE,
            "embedding_revision": self.round.embedding_revision, "files": self.bundle_files, "failure_reason": None,
        }
        self.r2.objects[f"{self.bundle_root}/bundle.json"] = canonical(self.bundle)
        return choice, artifact

    def save_choice(self) -> None:
        self.r2.objects[f"{self.root}/final.json"] = canonical(self.choice)

    async def run(self):
        self.r2.objects["artifacts/v1/rounds/test-1.json"] = self.round.model_dump_json().encode()
        pipeline = DownloadPipeline(
            GitBatcher(git=self.git, base_sha="ref", branch=self.settings.github_branch),
            self.r2,
            self.http,
            self.settings,
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


async def test_collects_pod_source_and_completed_bundle_from_the_current_round(collection: Collection) -> None:
    await collection.run()
    result = collection.result()
    assert result.js == f"{collection.settings.cdn_url}/{collection.artifact['source']['key']}"
    assert result.views == f"{collection.settings.cdn_url}/{collection.bundle_root}"
    assert result.failure_reason is None
    assert result.source_sha256 == digest(SOURCE) and result.submission_revision == "e" * 64
    assert collection.r2.objects[collection.artifact["source"]["key"]] == SOURCE
    assert collection.r2.objects[f"{collection.bundle_root}/embeddings.npz"] == b"fixture embeddings"


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
        collection.choice["accepted_at"] = float(collection.round.round_end + value)
    elif field == "key":
        collection.choice["source"]["key"] = value
    elif field == "png":
        collection.r2.objects[collection.bundle_files["grid.png"]["key"]] = value
    else:
        collection.r2.objects[collection.artifact[field]["key"]] = value
    collection.save_choice()
    with pytest.raises(ArtifactReadError):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed


async def test_waits_for_a_pending_pod_bundle_then_collects_it(collection: Collection) -> None:
    key = f"{collection.bundle_root}/bundle.json"
    saved = collection.r2.objects.pop(key)
    with pytest.raises(ArtifactReadError, match="not complete"):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed
    collection.r2.objects[key] = saved
    await collection.run()
    assert collection.result().views == f"{collection.settings.cdn_url}/{collection.bundle_root}"


async def test_collects_the_pod_canonical_rejection(collection: Collection) -> None:
    collection.bundle.update({"failure_reason": "render_rejected", "files": {}})
    collection.r2.objects[f"{collection.bundle_root}/bundle.json"] = canonical(collection.bundle)
    await collection.run()
    result = collection.result()
    assert result.js and result.views is None and result.failure_reason == "render_rejected"


@pytest.mark.parametrize("none", [True, False])
async def test_missing_and_declared_failures_do_not_select_old_renders(collection: Collection, none: bool) -> None:
    if none:
        collection.install(output_none=True)
    else:
        del collection.r2.objects[f"{collection.root}/final.json"]
    await collection.run()
    result = collection.result()
    assert result.js is None and result.views is None
    assert result.failure_reason == ("generation_failed" if none else "not_submitted")


async def test_collection_waits_for_witness_cutoff(collection: Collection) -> None:
    collection.round = collection.round.model_copy(update={"round_end": int(time.time())})
    with pytest.raises(ArtifactReadError, match="collection boundary"):
        await collection.run()


async def test_sealed_absence_prevents_a_late_inflight_submission(collection: Collection) -> None:
    key = f"{collection.root}/final.json"
    del collection.r2.objects[key]
    await collection.run()
    assert collection.result().failure_reason == "not_submitted"
    assert not await collection.r2.compare_and_swap(key, canonical(collection.choice), None)
    saved = json.loads(collection.r2.objects[key])
    assert saved["sealed"] and saved["missing"] and saved["accepted_at"] == 0


async def test_sealed_choice_prevents_a_late_inflight_replacement(collection: Collection) -> None:
    key = f"{collection.root}/final.json"
    old_version = digest(collection.r2.objects[key])
    await collection.run()
    replacement = {**collection.choice, "revision": "f" * 64}
    assert not await collection.r2.compare_and_swap(key, canonical(replacement), old_version)
    assert collection.result().source_sha256 == digest(SOURCE)
    assert json.loads(collection.r2.objects[key])["sealed"]


async def test_collection_observes_the_final_choice_that_wins_the_sealing_race(collection: Collection) -> None:
    collection.r2.before_final_seal = lambda: collection.install(output_none=True)
    await collection.run()
    assert collection.result().failure_reason == "generation_failed"
    assert collection.result().js is None


async def test_lost_seal_acknowledgement_recovers_the_same_choice(collection: Collection) -> None:
    collection.r2.lose_final_ack = True
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
    collection.round = collection.round.model_copy(
        update={"prompt_hashes_sha256": digest(canonical(sorted([digest(IMAGE), digest(second_image)])))}
    )
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
    monkeypatch.setattr(DownloadPipeline, "_collect_prompt", AsyncMock(side_effect=prompt))
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


@pytest.mark.parametrize("changed", ["prompt", "seed"])
async def test_changed_round_inputs_remain_pending(collection: Collection, changed: str) -> None:
    if changed == "prompt":
        await collection.http.aclose()
        collection.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"changed prompt bytes")
        ))
    else:
        collection.git.files["rounds/1/seed.json"] = '{"seed":43}'
    with pytest.raises(ArtifactReadError, match="frozen current round"):
        await collection.run()
    assert f"rounds/1/{HOTKEY}/submitted.json" not in collection.git.committed
