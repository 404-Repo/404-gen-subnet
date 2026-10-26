"""The persisted request and isolated credential must match the HTTP wire bytes."""

import json
from typing import Literal, cast

import pytest
from openai.types.responses import (
    ResponseInputParam,
    ResponseReasoningItem,
    ToolParam,
)
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming

from witness_client.request import load_credentials, prepare_request, serialize_body
from witness_client.types import WitnessConfig

TOKEN = "sk-proj-" + "a" * 80


@pytest.fixture
def config() -> WitnessConfig:
    return {
        "witness_url": "https://witness.example",
        "proof_parameters": "/unused",
    }


@pytest.mark.parametrize("prompt", ["", "Make a 🚚"])
@pytest.mark.parametrize(
    "mode,session_id",
    [
        ("api", None),
        ("codex", None),
        ("codex", "fixture-session"),
        ("codex", "s" * 1024),
    ],
)
def test_request_body_matches_wire_and_only_credential_is_hidden(
    config: WitnessConfig,
    mode: Literal["api", "codex"],
    prompt: str,
    session_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: (TOKEN, "abc123"),
    )
    params: ResponseCreateParamsStreaming = {
        "model": "fixture",
        "input": prompt,
        "stream": True,
        "reasoning": {"effort": "high"},
        "prompt_cache_key": "body-cache-key",
    }
    body = serialize_body(params, mode)
    assert params["input"] == prompt
    request = prepare_request(
        config, mode, TOKEN if mode == "api" else None, body=body, session_id=session_id
    )
    prefix, credential, suffix = request.chunks
    assert (prefix[0], credential[0], suffix[0]) == (False, True, False)
    assert credential[1] == TOKEN.encode()
    assert TOKEN.encode() not in prefix[1] + suffix[1]
    assert TOKEN not in repr(request)
    wire = b"".join(data for _, data in request.chunks)
    headers, body = wire.split(b"\r\n\r\n", 1)
    assert body == request.body
    assert f"Content-Length: {len(body)}".encode() in headers.split(b"\r\n")
    assert b"Authorization: Bearer " + TOKEN.encode() in headers.split(b"\r\n")
    assert (b"ChatGPT-Account-Id: abc123" in headers.split(b"\r\n")) == (
        mode == "codex"
    )
    assert [
        line for line in headers.split(b"\r\n") if line.startswith(b"session-id:")
    ] == ([] if session_id is None else [f"session-id: {session_id}".encode()])
    expected: dict[str, object] = {
        "model": "fixture",
        "input": prompt,
        "tools": [],
        "reasoning": {"effort": "high"},
        "stream": True,
        "prompt_cache_key": "body-cache-key",
    }
    if mode == "codex":
        expected.update(
            store=False,
            instructions="",
            input=[
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ],
        )
    assert json.loads(body) == expected
    assert request.url == (
        "https://api.openai.com/v1/responses"
        if mode == "api"
        else "https://chatgpt.com/backend-api/codex/responses"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "has space",
        "a\r\nX: injected",
        "a\n",
        "a\r",
        "a\t",
        "a\x00",
        "a\x7f",
        "é",
        "a" * 1025,
        42,
    ],
)
def test_invalid_session_id_fails_before_loading_credentials(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    def unexpected_login(*args: object) -> tuple[str, str]:
        pytest.fail("Invalid session_id must fail before reading credentials")

    monkeypatch.setattr(
        "witness_client.request.subscription_credentials", unexpected_login
    )
    with pytest.raises(ValueError, match="visible ASCII") as failure:
        prepare_request(config, "codex", None, body=b"{}", session_id=cast(str, value))
    assert (
        str(failure.value) == "session_id must contain 1-1024 visible ASCII characters"
    )


def test_session_id_does_not_enable_api_key_headers(config: WitnessConfig) -> None:
    with pytest.raises(ValueError, match="requires subscription"):
        prepare_request(config, "api", TOKEN, body=b"{}", session_id="fixture-session")


def test_maximum_session_id_and_credentials_fit_transport_headers(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: ("a" * 4096, "a" * 80),
    )
    request = prepare_request(config, "codex", None, body=b"{}", session_id="s" * 1024)
    headers, body = b"".join(data for _, data in request.chunks).split(b"\r\n\r\n", 1)
    assert len(headers) <= 8192 and body == b"{}"


@pytest.mark.parametrize("mode", ["api", "codex"])
def test_structured_input_is_preserved(mode: Literal["api", "codex"]) -> None:
    items: ResponseInputParam = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe the image"},
                {
                    "type": "input_image",
                    "image_url": "https://example.org/image.png",
                    "detail": "auto",
                },
            ],
        }
    ]
    body = serialize_body({"model": "fixture", "input": items, "stream": True}, mode)
    assert json.loads(body)["input"] == items


def test_subscription_preserves_settings_except_required_store(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: (TOKEN, "abc123"),
    )
    body = serialize_body(
        {
            "model": "fixture",
            "input": [],
            "instructions": "Return JavaScript",
            "tools": [
                {
                    "type": "function",
                    "name": "render",
                    "parameters": None,
                    "strict": False,
                }
            ],
            "store": True,
            "stream": True,
            "temperature": 0.5,
        },
        "codex",
    )
    request = prepare_request(config, "codex", None, body=body)
    body = json.loads(request.body)
    assert body["instructions"] == "Return JavaScript"
    assert body["tools"] == [
        {
            "type": "function",
            "name": "render",
            "parameters": None,
            "strict": False,
        }
    ]
    assert body["temperature"] == 0.5
    assert body["store"] is False and body["stream"] is True


@pytest.mark.parametrize(
    "credential", [None, "a" * 63, "a" * 4097, "a" * 80 + "\r\nX: injected", "é" * 80]
)
def test_invalid_credential_cannot_enter_wire_request(
    config: WitnessConfig, credential: str | None
) -> None:
    body = serialize_body({"model": "fixture", "input": "test", "stream": True}, "api")
    with pytest.raises(ValueError, match="Unsupported OpenAI credential format"):
        prepare_request(config, "api", credential, body=body)


def test_request_settings_and_size_limits(
    config: WitnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="requires streaming"):
        serialize_body(
            cast(
                ResponseCreateParamsStreaming,
                {
                    "model": "fixture",
                    "input": "test",
                    "stream": False,
                },
            ),
            "api",
        )

    def unexpected_login(*args: object) -> tuple[str, str]:
        pytest.fail("Oversized bodies must fail before reading credentials")

    monkeypatch.setattr(
        "witness_client.request.subscription_credentials", unexpected_login
    )
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        serialize_body(
            {"model": "fixture", "input": "x" * (8 * 1024 * 1024), "stream": True},
            "codex",
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"background": True}, "background generation"),
        ({"stream": False}, "requires streaming"),
        ({"stream": "true"}, "requires streaming"),
    ],
)
def test_unsupported_options_fail_before_subscription_login(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, object],
    message: str,
) -> None:
    def unexpected_login(*args: object) -> tuple[str, str]:
        pytest.fail("Invalid requests must fail before reading credentials")

    monkeypatch.setattr(
        "witness_client.request.subscription_credentials", unexpected_login
    )
    with pytest.raises(ValueError, match=message):
        body = serialize_body(
            cast(
                ResponseCreateParamsStreaming,
                {
                    "model": "fixture",
                    "input": "test",
                    "stream": True,
                    **params,
                },
            ),
            "codex",
        )
        prepare_request(config, "codex", None, body=body)


def test_subscription_refresh_reuses_materialized_iterables_and_request_body(
    config: WitnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = iter([(TOKEN, "abc123"), ("sk-proj-" + "b" * 80, "abc123")])
    monkeypatch.setattr(
        "witness_client.request.subscription_credentials",
        lambda *args: next(credentials),
    )
    tools: list[ToolParam] = [
        {
            "type": "function",
            "name": "render",
            "parameters": None,
            "strict": False,
        }
    ]
    params: ResponseCreateParamsStreaming = {
        "model": "fixture",
        "input": "test",
        "stream": True,
        "tools": iter(tools),
    }
    body = serialize_body(params, "codex")
    load_credentials(config, "codex", None)
    refreshed = prepare_request(config, "codex", None, body=body)
    assert body == refreshed.body
    assert json.loads(refreshed.body)["tools"] == tools
    assert refreshed.chunks[1][1] == ("sk-proj-" + "b" * 80).encode()


def test_sdk_response_models_can_be_serialized_for_followup(
    config: WitnessConfig,
) -> None:
    reasoning = ResponseReasoningItem(
        id="rs_test",
        type="reasoning",
        summary=[],
        encrypted_content="encrypted",
    )
    body = serialize_body(
        {
            "model": "fixture",
            "input": cast(ResponseInputParam, [reasoning]),
            "stream": True,
        },
        "api",
    )
    request = prepare_request(config, "api", TOKEN, body=body)
    assert json.loads(request.body)["input"] == [
        {
            "id": "rs_test",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "encrypted",
        }
    ]


@pytest.mark.parametrize("value", [True, 42, None])
def test_input_rejects_scalar_json_values(config: WitnessConfig, value: object) -> None:
    with pytest.raises(ValueError, match="prompt string or a list"):
        body = serialize_body(
            {
                "model": "fixture",
                "input": cast(ResponseInputParam, value),
                "stream": True,
            },
            "api",
        )
        prepare_request(config, "api", TOKEN, body=body)


def test_unknown_provider_settings_are_forwarded(config: WitnessConfig) -> None:
    body = serialize_body(
        cast(
            ResponseCreateParamsStreaming,
            {
                "model": "fixture",
                "input": "test",
                "stream": True,
                "new_provider_setting": {"enabled": True},
            },
        ),
        "api",
    )
    request = prepare_request(config, "api", TOKEN, body=body)
    assert json.loads(request.body)["new_provider_setting"] == {"enabled": True}


def test_prompt_template_can_omit_model_and_input(config: WitnessConfig) -> None:
    params: ResponseCreateParamsStreaming = {
        "prompt": {"id": "pmpt_fixture", "variables": {"subject": "lorry"}},
        "stream": True,
    }
    body = serialize_body(params, "api")
    request = prepare_request(config, "api", TOKEN, body=body)
    body = json.loads(request.body)
    assert body["prompt"] == params["prompt"]
    assert "model" not in body
    assert "input" not in body
    assert body["stream"] is True
