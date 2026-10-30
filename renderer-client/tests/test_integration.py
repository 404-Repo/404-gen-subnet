"""Opt-in checks against the pinned local JS renderer; no model calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from renderer_client import RecordingRenderer, RendererClient, RenderOptions, RenderRejected, ReplayRenderer

URL = os.environ.get("RENDERER_TEST_URL")
IMAGE = os.environ.get("RENDERER_TEST_ID")
pytestmark = pytest.mark.skipif(not URL or not IMAGE, reason="Set RENDERER_TEST_URL and RENDERER_TEST_ID")
FIXTURES = Path(__file__).resolve().parents[2] / "miner-reference" / "examples"


def test_real_validation_refinement_and_replay_after_client_closes(monkeypatch):
    source = (FIXTURES / "car.js").read_text()
    options = RenderOptions(img_size=128, lighting="neutral")
    with RendererClient(URL, renderer_id=IMAGE) as live:
        recording = RecordingRenderer(live)
        with pytest.raises(RenderRejected):
            recording.render("invalid JavaScript", options)
        png = recording.render(source, options)
        assert png[12:16] == b"IHDR"
        assert int.from_bytes(png[16:20], "big") == 261  # 2 x 128-pixel views + gap.

    def no_http(*args, **kwargs):
        pytest.fail("Offline replay attempted HTTP")

    monkeypatch.setattr(httpx.Client, "__init__", no_http)
    replay = ReplayRenderer(recording.records, renderer_id=IMAGE)
    with pytest.raises(RenderRejected):
        replay.render("invalid JavaScript", options)
    assert replay.render(source, options) == png
    replay.assert_consumed()


def test_same_pinned_runtime_repeats_single_and_grid_under_concurrent_requests():
    source = (FIXTURES / "material_variants.js").read_text()
    for options in (
        RenderOptions(thetas=(30,), phis=(0,), img_size=128, lighting="neutral"),
        RenderOptions(img_size=128, lighting="neutral"),
    ):
        with RendererClient(URL, renderer_id=IMAGE) as live:
            expected = live.render(source, options)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(live.render, source, options) for _ in range(2)]
                images = [future.result() for future in futures]
            assert images == [expected, expected]
