from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest

from pod_gateway.models import MARKERS, PodSpec, Scope, object_dict
from pod_gateway.runpod import (
    CreateRejected,
    CreateUnknown,
    ProviderUnavailable,
    RunPodClient,
)

SCOPE = Scope("competition", "test-0", "5" * 48)
SPEC = PodSpec("registry/witness@sha256:" + "a" * 64, env={"PRIVATE_KEY": "hidden-operator-key"})
START = "00000000-0000-4000-8000-000000000001"


def pod_json(pod_id: str = "cpu-pod") -> dict[str, object]:
    return {**SPEC.body(SCOPE, START), "id": pod_id, "status": "RUNNING"}


def client(handler: Callable[[httpx.Request], httpx.Response]) -> RunPodClient:
    return RunPodClient("offline-runpod-key", httpx.Client(transport=httpx.MockTransport(handler)))


def test_cpu_create_uses_v2_and_returns_no_secrets() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.runpod.io/v2/pods"
        assert request.headers["Authorization"] == "Bearer offline-runpod-key"
        body = json.loads(request.content)
        assert body["cpu"] == {"id": "cpu3c", "vcpuCount": 2}
        assert body["disk"] == 10 and body["ports"] == ["8000/tcp"]
        assert "gpu" not in body and "mounts" not in body
        assert body["env"]["WITNESS_BOOT_MODE"] == "dormant"
        return httpx.Response(201, json=pod_json())

    pod = client(handler).create(SPEC.body(SCOPE, START))
    assert len(requests) == 1 and requests[0].method == "POST"
    assert pod.id == "cpu-pod" and pod.in_scope(SCOPE)
    assert set(pod.markers) == set(MARKERS)
    assert "hidden-operator-key" not in repr(pod)
    assert "offline-runpod-key" not in repr(pod)


def test_direct_cpu_endpoint_uses_public_ip_and_mapped_tcp_port() -> None:
    reply = {**pod_json(), "runtime": {"ports": [
        {"private": 8000, "public": 41000, "ip": "10.0.0.4", "type": "tcp"},
        {"private": 8000, "public": 45123, "ip": "1.1.1.1", "type": "tcp"},
        {"private": 8001, "public": 45124, "ip": "1.1.1.1", "type": "http"},
    ]}}
    pod = client(lambda _: httpx.Response(200, json=reply)).get("cpu-pod")
    assert pod is not None and pod.tcp == {8000: ("1.1.1.1", 45123)}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_explicit_create_rejection_is_not_retried_or_echoed(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="secret-provider-diagnostic", headers={"Retry-After": "20"}
        )

    with pytest.raises(CreateRejected) as error:
        client(handler).create(SPEC.body(SCOPE, START))
    assert error.value.status == status and error.value.retry_after == "20"
    assert "secret-provider-diagnostic" not in str(error.value)
    assert calls == 1


@pytest.mark.parametrize("status", [202, 307, 408, 409, 500, 503])
def test_other_create_responses_are_uncertain_and_never_followed_or_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Location": "https://elsewhere.invalid"})

    with pytest.raises(CreateUnknown):
        client(handler).create(SPEC.body(SCOPE, START))
    assert calls == 1


@pytest.mark.parametrize("reply", [{"id": "partial"}, {"pods": []}, None])
def test_success_with_malformed_create_reply_is_uncertain(reply: object) -> None:
    with pytest.raises(CreateUnknown):
        client(lambda _: httpx.Response(201, json=reply)).create(SPEC.body(SCOPE, START))


def test_post_timeout_is_uncertain_and_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private request details", request=request)

    with pytest.raises(CreateUnknown) as error:
        client(handler).create(SPEC.body(SCOPE, START))
    assert calls == 1
    assert error.value.__cause__ is None
    assert "private request details" not in str(error.value)


def test_inventory_follows_all_cursors_and_deduplicates_pod_ids() -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        second = "cursor" in request.url.params
        return httpx.Response(200, json={
            "pods": [pod_json("first"), pod_json("second")] if second else [pod_json("first")],
            "pagination": {
                "hasNextPage": not second,
                "nextCursor": None if second else "next-page",
            },
        })

    assert [pod.id for pod in client(handler).list()] == ["first", "second"]
    assert queries == [
        {"limit": "1000", "includeClusterPods": "false"},
        {"limit": "1000", "includeClusterPods": "false", "cursor": "next-page"},
    ]


@pytest.mark.parametrize("pagination", [
    {"hasNextPage": True, "nextCursor": None},
    {"hasNextPage": "false", "nextCursor": None},
    {},
])
def test_incomplete_inventory_is_not_returned_as_a_complete_empty_list(pagination: object) -> None:
    provider = client(lambda _: httpx.Response(200, json={"pods": [], "pagination": pagination}))
    with pytest.raises(ProviderUnavailable):
        provider.list()


def test_repeated_cursor_fails_instead_of_looping() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={
            "pods": [], "pagination": {"hasNextPage": True, "nextCursor": "same"},
        })

    with pytest.raises(ProviderUnavailable):
        client(handler).list()
    assert calls == 2


def test_only_get_404_means_absence() -> None:
    assert client(lambda _: httpx.Response(404)).get("missing") is None
    with pytest.raises(ProviderUnavailable) as error:
        client(lambda _: httpx.Response(429, headers={"Retry-After": "10"})).get("existing")
    assert error.value.status == 429 and error.value.retry_after == "10"
    with pytest.raises(ProviderUnavailable):
        client(lambda _: httpx.Response(200, json=pod_json("wrong-id"))).get("existing")


@pytest.mark.parametrize("status", [204, 404])
def test_delete_uses_exact_id_and_accepts_absence(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "DELETE"
        assert request.url.raw_path == b"/v2/pods/exact%2Fid"
        return httpx.Response(status)

    client(handler).delete("exact/id")
    assert len(calls) == 1


def test_nullable_environment_of_an_unrelated_pod_is_not_an_inventory_failure() -> None:
    provider = client(lambda _: httpx.Response(200, json={**pod_json(), "env": None}))
    pod = provider.get("cpu-pod")
    assert pod is not None and not pod.in_scope(SCOPE)


def test_first_pod_is_owned_when_runpod_omits_its_empty_predecessor() -> None:
    reply = pod_json()
    reply["env"] = {
        key: value for key, value in object_dict(SPEC.body(SCOPE, START)["env"]).items() if value != ""
    }
    provider = client(lambda _: httpx.Response(200, json=reply))
    pod = provider.get("cpu-pod")
    assert pod is not None and pod.scope() == SCOPE
    assert pod.session().replaces is None


def test_spec_is_pinned_and_caller_environment_cannot_mutate_it() -> None:
    env = {"PRIVATE_KEY": "original"}
    spec = replace(SPEC, env=env)
    fingerprint = spec.fingerprint
    env["PRIVATE_KEY"] = "changed"
    assert spec.fingerprint == fingerprint
    assert "original" not in repr(spec)
    for values in (
        {"image": "registry/witness:latest"},
        {"vcpu_count": 3},
        {"vcpu_count": True},
        {"disk_gb": 0},
        {"port": 65536},
        {"env": {"WITNESS_BOOT_MODE": "active"}},
        {"env": {"RUNPOD_API_KEY": "must-stay-in-gateway"}},
    ):
        with pytest.raises(ValueError):
            replace(SPEC, **values)



def test_lifecycle_patch_changes_only_the_exact_pods_name() -> None:
    name = SCOPE.name(START) + "-active"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert str(request.url) == "https://api.runpod.io/v2/pods/cpu-pod"
        assert json.loads(request.content) == {"name": name}
        return httpx.Response(200, json={**pod_json(), "name": name})

    pod = client(handler).rename("cpu-pod", name)
    assert pod.lifecycle == "active" and pod.scope() == SCOPE
