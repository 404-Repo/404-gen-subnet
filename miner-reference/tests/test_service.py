"""Tests for the reference miner service — demonstrates the full batch lifecycle."""

import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

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

    # 2. Submit a small batch
    prompts = [
        {"stem": f"prompt_{i:03d}", "image_url": f"https://example.com/img_{i}.jpg"}
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
    assert "prompt_000.js" in names
    assert "prompt_001.js" in names
    assert "prompt_002.js" in names
    assert "_failed.json" not in names

    # 6. Stays in complete — results are retryable
    resp = await client.get("/status")
    assert resp.json()["status"] == "complete"

    resp = await client.get("/results")
    assert resp.status_code == 200
    assert set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist()) == names

    # 7. Next /generate transitions from complete to generating
    next_prompts = [
        {"stem": f"batch2_{i:03d}", "image_url": f"https://example.com/b2_{i}.jpg"}
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
    """Cannot submit a new batch while already generating a different one."""
    app.state.miner.status = PodStatus.GENERATING
    app.state.miner.batch_stems = frozenset(["existing_prompt"])
    resp = await client.post(
        "/generate",
        json={"prompts": [{"stem": "different_prompt", "image_url": "https://example.com/img.jpg"}], "seed": 1},
    )
    assert resp.status_code == 409


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
