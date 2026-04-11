"""Tests for the reference miner service — demonstrates the full batch lifecycle."""

import asyncio
import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from miner_reference import service
from miner_reference.service import MinerState, PodStatus, app


@pytest.fixture(autouse=True)
def fresh_state():
    """Reset miner state before each test, skipping the lifespan warmup delay."""
    state = MinerState()
    state.status = PodStatus.READY
    state.vram_ok = True
    state.vram_total_gb = 564.0
    app.state.miner = state


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_status_ready(client: AsyncClient):
    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


async def test_full_batch_lifecycle(client: AsyncClient):
    """Test the complete flow: ready -> generate -> complete -> results (retryable) -> next generate."""
    import asyncio

    # 1. Check status — should be ready
    resp = await client.get("/status")
    assert resp.json()["status"] == "ready"

    # 2. Submit a small batch (stems are lowercase alphanumeric per the spec)
    prompts = [
        {"stem": f"prompt{i:03d}", "image_url": f"https://example.com/img_{i}.jpg"}
        for i in range(3)
    ]
    resp = await client.post("/generate", json={"prompts": prompts, "seed": 42})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 3

    # 3. Status should be generating
    resp = await client.get("/status")
    data = resp.json()
    assert data["status"] == "generating"
    assert data["total"] == 3

    # 4. Wait for generation to complete
    for _ in range(50):
        resp = await client.get("/status")
        if resp.json()["status"] == "complete":
            break
        await asyncio.sleep(0.05)
    assert resp.json()["status"] == "complete"

    # 5. Download results (ZIP archive)
    resp = await client.get("/results")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "prompt000.js" in names
    assert "prompt001.js" in names
    assert "prompt002.js" in names
    assert "_failed.json" not in names

    # 6. Stays in complete — results are retryable
    resp = await client.get("/status")
    assert resp.json()["status"] == "complete"

    resp = await client.get("/results")
    assert resp.status_code == 200
    assert set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist()) == names

    # 7. Next /generate transitions from complete to generating
    next_prompts = [
        {"stem": f"batch2{i:03d}", "image_url": f"https://example.com/b2_{i}.jpg"}
        for i in range(2)
    ]
    resp = await client.post("/generate", json={"prompts": next_prompts, "seed": 99})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2
    assert app.state.miner.status == PodStatus.GENERATING


async def test_generate_idempotent_retry(client: AsyncClient):
    """Retrying /generate with the same stems while generating returns 200."""
    prompts = [{"stem": "aaa", "image_url": "https://example.com/a.jpg"}]

    resp = await client.post("/generate", json={"prompts": prompts, "seed": 42})
    assert resp.status_code == 200
    assert app.state.miner.status == PodStatus.GENERATING

    resp = await client.post("/generate", json={"prompts": prompts, "seed": 42})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


async def test_generate_rejected_when_not_ready(client: AsyncClient):
    """Cannot submit a new batch while already generating a different one.

    Also locks in the EXACT 409 body shape required by the API spec:

        {"detail": "Cannot accept batch", "current_status": "generating"}

    NOT the nested
    `{"detail": {"detail": "...", "current_status": "..."}}` that FastAPI's
    HTTPException produces by default. The handler bypasses HTTPException
    and returns a JSONResponse directly to get this exact shape.
    """
    app.state.miner.status = PodStatus.GENERATING
    app.state.miner.batch_stems = frozenset(["existingprompt"])
    resp = await client.post(
        "/generate",
        json={"prompts": [{"stem": "differentprompt", "image_url": "https://example.com/img.jpg"}], "seed": 1},
    )
    assert resp.status_code == 409

    body = resp.json()
    # Exact shape match — no extra keys, no nesting under `detail`.
    assert body == {
        "detail": "Cannot accept batch",
        "current_status": "generating",
    }, f"409 body drift from spec: got {body}"


async def test_generate_409_current_status_values(client: AsyncClient):
    """The /generate 409 body should report each valid `current_status`
    value exactly as the state machine reports it. This checks warming_up
    and replace — the remaining non-accepting states beyond `generating`
    (which is covered by test_generate_rejected_when_not_ready above).
    """
    for blocking_status in (PodStatus.WARMING_UP, PodStatus.REPLACE):
        app.state.miner.status = blocking_status
        app.state.miner.batch_stems = frozenset()  # no idempotency match
        resp = await client.post(
            "/generate",
            json={
                "prompts": [{"stem": "aaa", "image_url": "https://example.com/a.jpg"}],
                "seed": 1,
            },
        )
        assert resp.status_code == 409, f"expected 409 in {blocking_status}"
        assert resp.json() == {
            "detail": "Cannot accept batch",
            "current_status": blocking_status.value,
        }, f"body drift for status={blocking_status}"


async def test_results_rejected_when_not_complete(client: AsyncClient):
    """Cannot download results before generation is complete."""
    resp = await client.get("/results")
    assert resp.status_code == 409


async def test_replacement_request_on_bad_vram(client: AsyncClient):
    """When VRAM is insufficient and replacements are available, request replacement."""
    app.state.miner.vram_ok = False
    app.state.miner.vram_total_gb = 200.0

    resp = await client.get("/status", params={"replacements_remaining": 2})
    data = resp.json()
    assert data["status"] == "replace"
    assert data["payload"]["reason"] == "insufficient_vram"
    assert data["payload"]["detected_vram_gb"] == 200.0


async def test_no_replacement_when_budget_exhausted(client: AsyncClient):
    """When VRAM is bad but no replacements left, continue anyway."""
    app.state.miner.vram_ok = False
    app.state.miner.vram_total_gb = 200.0

    resp = await client.get("/status", params={"replacements_remaining": 0})
    data = resp.json()
    assert data["status"] == "ready"


async def test_replacement_request_during_warmup(client: AsyncClient):
    """VRAM check should trigger replacement during warming_up too, not just ready."""
    app.state.miner.status = PodStatus.WARMING_UP
    app.state.miner.vram_ok = False
    app.state.miner.vram_total_gb = 100.0

    resp = await client.get("/status", params={"replacements_remaining": 3})
    assert resp.json()["status"] == "replace"


async def test_generating_status_shows_progress(client: AsyncClient):
    """Progress count is reported correctly during generation."""
    app.state.miner.status = PodStatus.GENERATING
    app.state.miner.progress = 15
    app.state.miner.total = 32

    resp = await client.get("/status")
    data = resp.json()
    assert data["status"] == "generating"
    assert data["progress"] == 15
    assert data["total"] == 32


async def test_generation_crash_transitions_to_complete(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
):
    """If the background task crashes outside the per-prompt try/except, the
    pod must still transition to COMPLETE so it isn't stranded in GENERATING.

    The spec is explicit about this: "If your generation task crashes,
    transition to `complete` with whatever partial results you have. A pod
    stuck in `generating` forever will be detected as unresponsive and
    replaced, costing a pod from your budget."

    This test injects a crash by patching `asyncio.sleep` (called once per
    prompt outside the inner try/except) to raise on the third invocation.
    The first two prompts should land in `state.results`, the rest should
    end up in `state.failed` via the catch-all, and the pod must be
    `complete` and serving a valid ZIP.
    """
    crash_after_calls = [3]
    real_sleep = asyncio.sleep

    async def crashing_sleep(seconds: float) -> None:
        crash_after_calls[0] -= 1
        if crash_after_calls[0] <= 0:
            raise RuntimeError("simulated background-task crash")
        await real_sleep(seconds)

    # Patch asyncio.sleep AS USED INSIDE service.py only — leaves the test's
    # own asyncio.sleep calls intact.
    monkeypatch.setattr(service.asyncio, "sleep", crashing_sleep)

    prompts = [
        {"stem": f"crash{i}", "image_url": f"https://example.com/img_{i}.jpg"}
        for i in range(5)
    ]
    resp = await client.post("/generate", json={"prompts": prompts, "seed": 1})
    assert resp.status_code == 200

    # Wait for the task to crash and the catch-all to fire.
    for _ in range(50):
        resp = await client.get("/status")
        if resp.json()["status"] == "complete":
            break
        await real_sleep(0.05)

    assert resp.json()["status"] == "complete", (
        "pod must transition to complete even when the generation task crashes"
    )

    # /results must serve a valid ZIP, not 409 or empty.
    resp = await client.get("/results")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())

    # The catch-all should have populated _failed.json with reasons for
    # every prompt that didn't make it into results.
    assert "_failed.json" in names, (
        "crashed prompts must be reported in _failed.json so the orchestrator "
        "knows they didn't generate"
    )

    import json
    failed_manifest = json.loads(zf.read("_failed.json"))
    state = app.state.miner
    succeeded = {n[:-3] for n in names if n.endswith(".js")}
    failed = set(failed_manifest.keys())

    # Every prompt must appear in exactly one place — never both, never
    # neither. This mirrors the API spec edge-case rule for /results.
    all_stems = {p["stem"] for p in prompts}
    assert succeeded.isdisjoint(failed)
    assert succeeded | failed == all_stems
    assert len(state.results) + len(state.failed) == len(prompts)


async def test_generate_rejects_invalid_stem_format(client: AsyncClient):
    """Stems must be lowercase alphanumeric per the spec — non-conforming
    input is rejected at request validation time with 422, before any state
    mutation."""
    resp = await client.post(
        "/generate",
        json={
            "prompts": [{"stem": "Invalid-Stem!", "image_url": "https://example.com/x.jpg"}],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    # State should be unchanged — no batch should have started.
    assert app.state.miner.status == PodStatus.READY


async def test_status_warming_up_payload(client: AsyncClient):
    """During warming_up the /status payload should report the current
    stage, demonstrating the diagnostic-payload pattern from the spec."""
    app.state.miner.status = PodStatus.WARMING_UP
    app.state.miner.warming_up_stage = "loading_unet"

    resp = await client.get("/status")
    data = resp.json()
    assert data["status"] == "warming_up"
    assert data["payload"] == {"stage": "loading_unet"}
    # progress/total should NOT be in the response (excluded as None).
    assert "progress" not in data
    assert "total" not in data


async def test_status_excludes_null_fields(client: AsyncClient):
    """The /status response should omit null fields entirely, not serialize
    them as `null`. Cleaner output for the orchestrator's logs."""
    resp = await client.get("/status")
    data = resp.json()
    assert "status" in data
    # In a vanilla ready state, none of the optional fields should be present.
    assert "progress" not in data
    assert "total" not in data
    assert "payload" not in data


async def test_results_byte_identical_across_retries(client: AsyncClient):
    """Two consecutive /results calls in the same complete state must
    return byte-identical responses. The ZIP is built once and cached so
    retries don't re-zip and don't drift on metadata (timestamps etc.).
    """
    prompts = [
        {"stem": f"same{i}", "image_url": f"https://example.com/img_{i}.jpg"}
        for i in range(3)
    ]
    resp = await client.post("/generate", json={"prompts": prompts, "seed": 1})
    assert resp.status_code == 200

    # Wait for completion.
    for _ in range(50):
        resp = await client.get("/status")
        if resp.json()["status"] == "complete":
            break
        await asyncio.sleep(0.05)
    assert resp.json()["status"] == "complete"

    # Two consecutive downloads must yield byte-identical bytes.
    first = (await client.get("/results")).content
    second = (await client.get("/results")).content
    assert first == second
    assert len(first) > 0
