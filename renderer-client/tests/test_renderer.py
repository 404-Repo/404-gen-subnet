import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import httpx
import pytest

from renderer_client import (
    RecordingRenderer,
    RendererClient,
    RendererError,
    RenderOptions,
    RenderRecord,
    RenderRejected,
    RenderRequest,
    RenderUnavailable,
    ReplayMismatch,
    ReplayRenderer,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=")
SOURCE = "export default function generate(THREE) { return new THREE.Group(); }"
IMAGE = "sha256:" + "a" * 64
DETAILS = {"error": "validation_failed", "failures": [{"stage": "parse", "rule": "MISSING_DEFAULT_EXPORT"}]}


def client(handler):
    return RendererClient(
        "http://127.0.0.1:8000", renderer_id=IMAGE, api_key="fixture-key", transport=httpx.MockTransport(handler)
    )


def test_live_render_sends_source_and_every_setting_and_returns_exact_png():
    calls = []
    options = RenderOptions(
        thetas=(30, 120),
        phis=(0, -30),
        img_size=1024,
        cam_radius=3,
        cam_fov_deg=40,
        gap=0,
        bg_color="FFFFFF",
        lighting="neutral",
    )

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/render/grid"
        assert dict(request.url.params) == {
            "thetas": "30.0,120.0",
            "phis": "0.0,-30.0",
            "img_size": "1024",
            "cam_radius": "3",
            "cam_fov_deg": "40",
            "gap": "0",
            "bg_color": "ffffff",
            "lighting": "neutral",
        }
        assert json.loads(request.content) == {"source": SOURCE}
        assert request.headers["authorization"] == "Bearer fixture-key"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with client(handler) as live:
        assert live.render(SOURCE, options) == PNG
    assert len(calls) == 1


def test_single_view_uses_one_angle_pair_on_same_interface():
    def handler(request):
        assert request.url.params["thetas"] == "30.0"
        assert request.url.params["phis"] == "0.0"
        assert request.url.params["lighting"] == "studio"
        assert request.url.params["img_size"] == "518"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with client(handler) as live:
        assert live.render(SOURCE, RenderOptions(thetas=(30,), phis=(0,))) == PNG


def test_record_and_replay_rejection_then_refinement_without_network(monkeypatch):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(422, json=DETAILS)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with client(handler) as live:
        recording = RecordingRenderer(live)
        with pytest.raises(RenderRejected) as rejected:
            recording.render("invalid source")
        assert rejected.value.details == DETAILS
        assert recording.render(SOURCE) == PNG
    assert attempts == 2
    assert recording.records[1].request.source_sha256 == hashlib.sha256(SOURCE.encode()).hexdigest()

    def no_network(*args, **kwargs):
        pytest.fail("Replay attempted to create an HTTP client")

    monkeypatch.setattr(httpx.Client, "__init__", no_network)
    replay = ReplayRenderer(recording.records, renderer_id=IMAGE)
    with pytest.raises(RenderRejected) as rejected_again:
        replay.render("invalid source")
    assert rejected_again.value.detail_json == recording.records[0].rejection_json
    assert replay.render(SOURCE) == PNG
    replay.assert_consumed()


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 503])
def test_http_infrastructure_errors_do_not_become_miner_rejections_or_retry(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"location": "https://other.test"})

    with client(handler) as live:
        recording = RecordingRenderer(live)
        with pytest.raises(RenderUnavailable):
            recording.render(SOURCE)
        with pytest.raises(RendererError, match="interrupted"):
            _ = recording.records
    assert calls == 1


def test_transport_failure_remains_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("fixture", request=request)

    with client(handler) as live, pytest.raises(RenderUnavailable):
        live.render(SOURCE)


def test_failed_observation_cannot_be_hidden_by_later_success():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with client(handler) as live:
        recording = RecordingRenderer(live)
        with pytest.raises(RenderUnavailable):
            recording.render(SOURCE)
        assert recording.render(SOURCE) == PNG
        with pytest.raises(RendererError, match="interrupted"):
            _ = recording.records


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(422, content=b"not JSON"),
        httpx.Response(422, json=[]),
        httpx.Response(200, headers={"content-type": "text/html"}, content=PNG),
        httpx.Response(200, headers={"content-type": "image/png"}, content=b"not a PNG"),
    ],
)
def test_malformed_service_responses_are_unavailable(response):
    with client(lambda request: response) as live, pytest.raises(RenderUnavailable):
        live.render(SOURCE)


def test_response_reads_are_bounded_and_stream_is_closed(monkeypatch):
    from renderer_client import client as module

    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield PNG * 1000

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(PNG))
    with client(lambda request: httpx.Response(200, headers={"content-type": "image/png"}, stream=Stream())) as live:
        with pytest.raises(RenderUnavailable, match="size limit"):
            live.render(SOURCE)
    assert closed == [True]


@pytest.mark.parametrize(
    "changed",
    [
        {"thetas": (25.0, 120.0, 216.0, 312.0)},
        {"phis": (-14.0, -15.0, -15.0, -15.0)},
        {"img_size": 1024},
        {"cam_radius": 3.0},
        {"cam_fov_deg": 40.0},
        {"gap": 0},
        {"bg_color": "ffffff"},
        {"lighting": "neutral"},
    ],
)
def test_replay_checks_every_render_option_without_consuming_mismatch(changed):
    options = RenderOptions()
    record = RenderRecord(RenderRequest.from_source(IMAGE, SOURCE, options), png=PNG)
    replay = ReplayRenderer([record], renderer_id=IMAGE)
    with pytest.raises(ReplayMismatch):
        replay.render(SOURCE, replace(options, **changed))
    assert replay.render(SOURCE) == PNG
    replay.assert_consumed()


def test_replay_checks_source_and_image_identity():
    record = RenderRecord(RenderRequest.from_source(IMAGE, SOURCE, RenderOptions()), png=PNG)
    replay = ReplayRenderer([record], renderer_id=IMAGE)
    with pytest.raises(ReplayMismatch):
        replay.render(SOURCE + " ")
    with pytest.raises(ReplayMismatch):
        ReplayRenderer([record], renderer_id="sha256:" + "b" * 64).render(SOURCE)


def test_duplicate_renders_are_distinct_observations_and_tail_is_checked():
    record = RenderRecord(RenderRequest.from_source(IMAGE, SOURCE, RenderOptions()), png=PNG)
    other_options = RenderOptions(lighting="neutral")
    other = RenderRecord(RenderRequest.from_source(IMAGE, SOURCE, other_options), png=PNG + b"neutral")
    replay = ReplayRenderer([record, other, record], renderer_id=IMAGE)
    assert replay.render(SOURCE) == PNG
    with pytest.raises(ReplayMismatch, match="2 recorded render.*not consumed"):
        replay.assert_consumed()
    assert replay.render(SOURCE) == PNG
    with pytest.raises(ReplayMismatch, match="Render inputs differ"):
        replay.render(SOURCE)
    assert replay.render(SOURCE, other_options) == PNG + b"neutral"
    replay.assert_consumed()
    with pytest.raises(ReplayMismatch, match="No recorded render"):
        replay.render(SOURCE)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"thetas": ()},
        {"phis": (0,)},
        {"thetas": (float("nan"), 0, 0, 0)},
        {"img_size": 0},
        {"img_size": True},
        {"gap": -1},
        {"cam_radius": float("inf")},
        {"cam_radius": 0},
        {"cam_fov_deg": 180},
        {"bg_color": "#808080"},
        {"lighting": "typo"},
    ],
)
def test_invalid_options_are_refused(kwargs):
    with pytest.raises(ValueError):
        RenderOptions(**kwargs)


@pytest.mark.parametrize("url", ["file:///tmp/renderer", "http://user:password@localhost", "http://localhost?x=1"])
def test_invalid_endpoints_are_refused(url):
    with pytest.raises(ValueError):
        RendererClient(url, renderer_id=IMAGE)


def test_record_rejects_ambiguous_outcome_and_malformed_payload():
    request = RenderRequest.from_source(IMAGE, SOURCE, RenderOptions())
    for kwargs in ({}, {"png": PNG, "rejection_json": json.dumps(DETAILS)}, {"png": b"invalid"}):
        with pytest.raises(ValueError):
            RenderRecord(request, **kwargs)


@pytest.mark.parametrize("reject_first", [False, True])
def test_replay_matches_inputs_when_concurrent_renders_complete_out_of_order(reject_first):
    entered, release = Event(), Event()
    other_source = SOURCE + " "

    class Renderer:
        renderer_id = IMAGE

        def render(self, source, options=None):
            if source == SOURCE:
                entered.set()
                assert release.wait(3)
                if reject_first:
                    raise RenderRejected(json.dumps(DETAILS))
                return PNG + b"first"
            return PNG + b"second"

    recording = RecordingRenderer(Renderer())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recording.render, SOURCE)
        try:
            assert entered.wait(3)
            assert pool.submit(recording.render, other_source).result(timeout=3) == PNG + b"second"
        finally:
            release.set()
        if reject_first:
            with pytest.raises(RenderRejected):
                first.result(timeout=3)
        else:
            assert first.result(timeout=3) == PNG + b"first"

    replay = ReplayRenderer(recording.records, renderer_id=IMAGE)
    if reject_first:
        with pytest.raises(RenderRejected) as rejected:
            replay.render(SOURCE)
        assert rejected.value.details == DETAILS
    else:
        assert replay.render(SOURCE) == PNG + b"first"
    assert replay.render(other_source) == PNG + b"second"
    replay.assert_consumed()
