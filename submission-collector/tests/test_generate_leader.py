"""Offline coverage of the manual leader's frozen inputs and publication boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import generate_leader as leader


HTTP_CLIENT = httpx.AsyncClient


def commit(directory: Path, files: dict[str, str]) -> str:
    for name, value in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    leader.git(directory, "add", ".")
    leader.git(
        directory,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Fixture",
    )
    return leader.git(directory, "rev-parse", "HEAD").decode().strip()


def repo(path: Path, name: str) -> Path:
    path.mkdir()
    leader.git(path, "init", "-q")
    leader.git(path, "remote", "add", "origin", f"https://github.com/{name}.git")
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    source = repo(tmp_path / "miner", leader.BASE_REPO)
    source_commit = commit(
        source,
        {
            "main.py": "async def generate(image, seed, witness): return 'source'\n",
            "miner/run.py": "async def run(*args, **kwargs): pass\n",
        },
    )
    monkeypatch.setattr(leader, "BASE_COMMIT", source_commit)
    competition = repo(tmp_path / "competition", leader.DEFAULT_COMPETITION)
    commit(
        competition,
        {
            "config.json": json.dumps({"name": "gpt", "prompts_per_round": 2}),
            "state.json": '{"current_round": 0}',
            "rounds/0/prompts.txt": "https://prompts.test/a.png\nhttps://prompts.test/b.png\n",
            "rounds/0/seed.json": '{"seed": 123}',
            "leader.json": json.dumps(
                [{"repo": leader.BASE_REPO, "commit": source_commit, "hotkey": "historical-leader"}]
            ),
        },
    )
    return argparse.Namespace(
        competition=competition,
        competition_repo=leader.DEFAULT_COMPETITION,
        source=source,
        runner_source=source,
        run_dir=tmp_path / "run",
        round=0,
        ref="HEAD",
        cdn_url="https://cdn.test",
    )


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(handler))
    )


async def prepared(world: argparse.Namespace, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    mock_http(monkeypatch, lambda request: httpx.Response(200, content=request.url.path.encode()))
    await leader.prepare(world)
    identity = {"hotkey": "authenticated-operator", "witness_url": "https://witness.subnet404.xyz"}
    leader.save(world.run_dir / "identity.json", identity)
    return leader.load_manifest(world.run_dir)


def saved() -> dict[str, object]:
    return {
        key: {
            "js": f"rounds/gpt-0/authenticated-operator/submitted/{key}.js",
            "views": f"rounds/gpt-0/authenticated-operator/submitted/{key}",
            "size": 6,
        }
        for key in ("a", "b")
    }


async def test_prepare_freezes_commit_and_official_inputs_ignoring_owner_edits(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (world.source / "main.py").write_text("uncommitted owner work")
    manifest = await prepared(world, monkeypatch)
    assert "uncommitted" not in (world.run_dir / "source/main.py").read_text()
    assert manifest["seed"] == 123
    assert manifest["round_name"] == "gpt-0"
    assert set(manifest["prompts"]) == {"a", "b"}
    assert (world.source / "main.py").read_text() == "uncommitted owner work"
    (world.run_dir / "prompts/a").write_bytes(b"wrong image")
    with pytest.raises(ValueError, match="Frozen image changed"):
        leader.load_manifest(world.run_dir)


async def test_prepare_selects_new_incumbent_from_history(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incumbent = repo(world.source.parent / "incumbent", "team/new-leader")
    revision = commit(incumbent, {"main.py": "async def generate(image, seed, witness): return None\n"})
    history = leader.read_json(world.competition, "HEAD", "leader.json")
    history.append({"repo": "team/new-leader", "commit": revision, "hotkey": "new-leader-hotkey"})
    commit(world.competition, {"leader.json": json.dumps(history)})
    world.source = incumbent
    manifest = await prepared(world, monkeypatch)
    assert manifest["leader"] == history[-1]
    assert manifest["runner_repo"] == leader.BASE_REPO
    assert manifest["runner_commit"] != revision


@pytest.mark.parametrize("change", ["source", "runner", "extra"])
async def test_resume_rejects_changed_frozen_code(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    await prepared(world, monkeypatch)
    path = {"source": "source/main.py", "runner": "runner.py", "extra": "source/extra.py"}[change]
    (world.run_dir / path).write_text("changed")
    with pytest.raises(ValueError, match="Frozen"):
        leader.load_manifest(world.run_dir)


@pytest.mark.parametrize("kind", ["partial", "unfinished", "foreign_round", "foreign_hotkey"])
async def test_unfinished_or_foreign_indexes_do_not_create_a_judge_bundle(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    await prepared(world, monkeypatch)
    rows = saved()
    if kind == "partial":
        del rows["b"]
    elif kind == "unfinished":
        rows["b"] = {}
    elif kind == "foreign_round":
        rows["b"]["js"] = "rounds/test-9/authenticated-operator/submitted/b.js"
    else:
        rows["b"]["views"] = "rounds/gpt-0/leader/submitted/b"
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=rows))
    with pytest.raises(ValueError):
        await leader.handoff(world)
    assert not (world.competition / "rounds/0/leader").exists()


async def test_handoff_verifies_every_artifact_and_keeps_evidence_identity(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepared(world, monkeypatch)
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        return httpx.Response(200, json=saved())

    mock_http(monkeypatch, handler)
    await leader.handoff(world)
    target = world.competition / "rounds/0/leader"
    generated = json.loads((target / "generated.json").read_bytes())
    assert generated["a"]["js"] == "https://cdn.test/rounds/gpt-0/authenticated-operator/submitted/a.js"
    assert ("HEAD", "/rounds/gpt-0/authenticated-operator/submitted/a/white/front_below.png") in requested
    assert len([method for method, _ in requested if method == "HEAD"]) == 30
    provenance = json.loads((target / "source.json").read_bytes())
    assert provenance["leader"]["hotkey"] == "historical-leader"
    assert provenance["witness"]["hotkey"] == "authenticated-operator"
    assert provenance["submitted_index"].endswith("/authenticated-operator/submitted.json")
    await leader.handoff(world)


async def test_missing_embeddings_leaves_no_judge_bundle(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepared(world, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=saved())
        return httpx.Response(404 if request.url.path.endswith("embeddings.npz") else 200)

    mock_http(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await leader.handoff(world)
    assert not (world.competition / "rounds/0/leader").exists()


async def test_handoff_rejects_official_seed_change(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepared(world, monkeypatch)
    commit(world.competition, {"rounds/0/seed.json": '{"seed": 456}'})
    with pytest.raises(ValueError, match="inputs or incumbent"):
        await leader.handoff(world)


async def test_terminal_none_result_is_preserved(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepared(world, monkeypatch)
    rows = saved()
    rows["b"] = {"failure_reason": "generation_failed"}
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=rows))
    await leader.handoff(world)
    generated = json.loads((world.competition / "rounds/0/leader/generated.json").read_bytes())
    assert generated["b"]["js"] is None
    assert generated["b"]["failure_reason"] == "generation_failed"


async def test_gateway_round_mismatch_stops_before_allocation_or_generation(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bittensor_wallet
    import witness_client.async_client
    from witness_client import WitnessError
    from witness_client.async_pods import AsyncPodGatewayClient

    await prepared(world, monkeypatch)
    (world.run_dir / "identity.json").unlink()
    world.wallet_name, world.wallet_hotkey, world.wallet_path = "miner", "default", Path("/unused-wallet")
    world.witness_config = world.run_dir / "witness.json"
    world.parallel_prompts, world.parallel_calls, world.attempts = 1, 1, 1
    leader.save(world.witness_config, {"witness_url": "https://witness.subnet404.xyz", "proof_parameters": "/unused"})
    key = bittensor_wallet.Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(bittensor_wallet, "Wallet", lambda **kwargs: SimpleNamespace(hotkey=key))
    monkeypatch.setattr(witness_client.async_client, "Prover", lambda *args: None)
    monkeypatch.setattr(AsyncPodGatewayClient, "_allocation_json", AsyncMock(return_value={"round": "test-9"}))
    runner = SimpleNamespace()

    async def invoke(generate: object, prompts: object, seed: int, **kwargs: object) -> None:
        async with runner.AsyncWitnessClient(world.witness_config, hotkey=key, subscription="codex"):
            pytest.fail("Wrong-round gateway must stop before generation")

    runner.run = invoke
    load = leader.load_module
    monkeypatch.setattr(
        leader, "load_module", lambda name, path: runner if name == "_leader_runner" else load(name, path)
    )
    with pytest.raises(WitnessError, match="Gateway round differs"):
        await leader.run(world)


@pytest.mark.parametrize("save_error", [False, True])
async def test_explicit_failure_uses_pod_saving_without_source_or_model_calls(
    world: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    save_error: bool,
) -> None:
    import bittensor_wallet
    import witness_client.async_client
    from witness_client import AsyncWitnessClient
    from witness_client.async_pods import AsyncPodGatewayClient

    await prepared(world, monkeypatch)
    (world.run_dir / "identity.json").unlink()
    world.command, world.prompt = "fail", ["b"]
    world.wallet_name, world.wallet_hotkey, world.wallet_path = "miner", "default", Path("/unused-wallet")
    world.witness_config = world.run_dir / "witness.json"
    leader.save(world.witness_config, {"witness_url": "https://witness.subnet404.xyz", "proof_parameters": "/unused"})
    key = bittensor_wallet.Keypair.create_from_uri("//Alice")
    rows = json.loads(json.dumps(saved()).replace("authenticated-operator", key.ss58_address))
    del rows["b"]
    posts = []
    monkeypatch.setattr(bittensor_wallet, "Wallet", lambda **kwargs: SimpleNamespace(hotkey=key))
    monkeypatch.setattr(witness_client.async_client, "Prover", lambda *args: None)
    monkeypatch.setattr(AsyncPodGatewayClient, "_allocation_json", AsyncMock(return_value={"round": "gpt-0"}))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Explicit failure saving must not execute Python source or make model calls")

    async def send(self: AsyncPodGatewayClient, request: httpx.Request) -> httpx.Response:
        await self._allocation_json("GET", "/v1/pods/current", deadline=99999999999)
        if request.url.path == "/v1/artifacts/context":
            return httpx.Response(200, json={"submission_commit": None}, request=request)
        assert request.url.path == "/v1/submissions"
        if request.method == "GET":
            return httpx.Response(200, json={"submissions": rows}, request=request)
        body = json.loads(request.content)
        posts.append(body)
        assert body["source"] is None and body["prompt_image"] is None
        assert body["generation"] == {"seed": 123, "prompt_sha256": leader.sha(b"/b.png")}
        if save_error:
            raise httpx.ReadError("Synthetic save failure", request=request)
        rows["b"] = {"failure_reason": "generation_failed"}
        return httpx.Response(200, json={"submission": rows["b"]}, request=request)

    monkeypatch.setattr(leader, "load_module", forbidden)
    monkeypatch.setattr(AsyncWitnessClient, "_resolve", forbidden)
    monkeypatch.setattr(AsyncPodGatewayClient, "send", send)
    if save_error:
        with pytest.raises(httpx.ReadError):
            await leader.run(world)
        assert not (world.run_dir / "operator_failures.json").exists()
        assert json.loads((world.run_dir / "progress.json").read_bytes())["pending"] == ["b"]
        return
    await leader.run(world)
    await leader.run(world)
    assert len(posts) == 1
    assert json.loads((world.run_dir / "operator_failures.json").read_bytes()) == ["b"]
    assert json.loads((world.run_dir / "progress.json").read_bytes())["pending"] == []
    world.prompt = ["a"]
    with pytest.raises(ValueError, match="Cannot replace"):
        await leader.run(world)
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=rows))
    await leader.handoff(world)
    provenance = json.loads((world.competition / "rounds/0/leader/source.json").read_bytes())
    assert provenance["operator_failed_prompts"] == ["b"]
