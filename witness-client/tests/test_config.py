"""Configuration must preserve secure defaults and private recovery storage."""

import json
import ssl
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import httpx
import pytest
from bittensor_wallet import Keypair
from witness_client import WitnessClient, WitnessConfig
from witness_client.request import prepare_request, serialize_body

PARAMS = Path(__file__).resolve().parents[2] / "witness-server/verifier/params"
TOKEN = "sk-proj-" + "a" * 80


@pytest.fixture
def key() -> Keypair:
    return Keypair.create_from_mnemonic(Keypair.generate_mnemonic())


@pytest.fixture
def config(tmp_path: Path) -> WitnessConfig:
    return {
        "witness_url": "https://witness.example",
        "round": "1",
        "proof_parameters": str(PARAMS),
        "state_directory": str(tmp_path / "state"),
    }


@pytest.mark.parametrize("from_file", [False, True])
def test_configuration_is_copied_or_loaded_as_utf8(
    config: WitnessConfig, tmp_path: Path, from_file: bool, key: Keypair
) -> None:
    config["codex_home"] = "учётная-запись"
    source: WitnessConfig | Path = config
    if from_file:
        source = tmp_path / "witness.json"
        source.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    with WitnessClient(source, openai_api_key=TOKEN, hotkey=key) as client:
        config["codex_home"] = "changed"
        assert client._config["codex_home"] == "учётная-запись"


@pytest.mark.parametrize("xdg", [None, "", "relative", "absolute"])
@pytest.mark.parametrize("explicit", [False, True])
def test_state_directory_selection_and_permissions(
    config: WitnessConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xdg: str | None,
    explicit: bool,
    key: Keypair,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv(
            "XDG_STATE_HOME", str(tmp_path / "xdg") if xdg == "absolute" else xdg
        )
    expected = Path(config["state_directory"])
    if not explicit:
        del config["state_directory"]
        base = tmp_path / "xdg" if xdg == "absolute" else home / ".local" / "state"
        expected = base / "404-witness-client"
    with WitnessClient(config, openai_api_key=TOKEN, hotkey=key) as client:
        assert client._state == expected
        assert expected.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
        "http://witness.example",
        "https:///path",
        "https://witness.example/path",
        "https://witness.example?query",
        "https://witness.example#fragment",
        "https://user@witness.example",
    ],
)
def test_non_https_origins_are_rejected(
    config: WitnessConfig, url: str, key: Keypair
) -> None:
    config["witness_url"] = url
    with pytest.raises(ValueError, match="HTTPS origin"):
        WitnessClient(config, hotkey=key, openai_api_key=TOKEN)
    assert not Path(config["state_directory"]).exists()


@pytest.mark.parametrize("preparations", [0, -1, 33, True, 1.5, "3"])
def test_invalid_preparation_limit_is_rejected_before_setup(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, key: Keypair, preparations: int
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid concurrency must not initialize the prover or HTTP")

    monkeypatch.setattr("witness_client.client.Prover", forbidden)
    monkeypatch.setattr("witness_client.client.httpx.Client", forbidden)
    with pytest.raises(ValueError, match="parallel_preparations"):
        WitnessClient(config, hotkey=key, openai_api_key=TOKEN, parallel_preparations=preparations)
    assert not Path(config["state_directory"]).exists()


def test_invalid_configuration_is_rejected_before_setup(
    config: WitnessConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: Keypair,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid configuration must not initialize the prover or HTTP")

    monkeypatch.setattr("witness_client.client.Prover", forbidden)
    monkeypatch.setattr("witness_client.client.httpx.Client", forbidden)
    raw = dict(config)
    del raw["proof_parameters"]
    path = tmp_path / "witness.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(KeyError, match="proof_parameters"):
        WitnessClient(path, hotkey=key, openai_api_key=TOKEN)
    assert not Path(config["state_directory"]).exists()


def test_insecure_state_directory_is_rejected_before_setup(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, key: Keypair
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Private state must be checked before initializing prover or HTTP")

    monkeypatch.setattr("witness_client.client.Prover", forbidden)
    monkeypatch.setattr("witness_client.client.httpx.Client", forbidden)
    state = Path(config["state_directory"])
    state.mkdir()
    state.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        WitnessClient(config, hotkey=key, openai_api_key=TOKEN)


@pytest.mark.parametrize("mode", ["api", "codex"])
def test_configuration_cannot_override_certificate_trust_or_provider(
    config: WitnessConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["api", "codex"],
    key: Keypair,
) -> None:
    raw = dict(config)
    raw.update(
        {
            "witness_url": "https://localhost",
            "ca_file": "/untrusted/witness.pem",
            "provider_ca_file": "/untrusted/provider.pem",
            "test_provider": {"host": "localhost", "path": "/fixture"},
            "test_token": "fixture",
        }
    )
    path = tmp_path / "witness.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: (TOKEN, "abc123"),
    )
    client_constructor = Mock(wraps=httpx.Client)
    monkeypatch.setattr("witness_client.client.httpx.Client", client_constructor)
    with WitnessClient(path, openai_api_key=TOKEN, hotkey=key) as client:
        assert client_constructor.call_args is not None
        context = client_constructor.call_args.kwargs["verify"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        body = serialize_body(
            {"model": "fixture", "input": "test", "stream": True}, mode
        )
        request = prepare_request(client._config, mode, TOKEN, body=body)
        assert request.url == (
            "https://api.openai.com/v1/responses"
            if mode == "api"
            else "https://chatgpt.com/backend-api/codex/responses"
        )
