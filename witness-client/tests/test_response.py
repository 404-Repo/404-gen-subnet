import json

import pytest
from witness_client import WitnessError
from witness_client.response import completed_response, http_body, provider_error
from witness_client.types import CompletedResponse, OutputItem, ResponseEvent


@pytest.mark.parametrize(
    "raw",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nabc",
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nabc",
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\nabc",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nextra",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nab",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabcXX0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: y\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3;ext=1\r\nabc\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nG\r\nabc\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n000000003\r\nabc\r\n0\r\n\r\n",
    ],
)
def test_incomplete_or_ambiguous_http_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        http_body(raw)


@pytest.mark.parametrize("chunk_count", [0, 1, 65536])
def test_chunked_body_preserves_binary_data(chunk_count: int) -> None:
    chunk = b"x" * 125 + b"\r\n\x00"
    frame = b"80\r\n" + chunk + b"\r\n"
    raw = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + frame * chunk_count
        + b"0\r\n\r\n"
    )
    assert http_body(raw) == chunk * chunk_count


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"4\r\na\r\nb\r\n3\r\nxyz\r\n0\r\n\r\n", b"a\r\nbxyz"),
        (b"0000000A\r\n0123456789\r\n00\r\n\r\n", b"0123456789"),
    ],
)
def test_chunked_lengths_and_boundaries(body: bytes, expected: bytes) -> None:
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body
    assert http_body(raw) == expected


def sse(*events: ResponseEvent) -> bytes:
    return b"".join(
        b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
    )


def test_final_text_and_stream_must_agree() -> None:
    done: ResponseEvent = {"type": "response.output_text.done", "text": "original"}
    completed: ResponseEvent = {
        "type": "response.completed",
        "response": {"status": "completed", "output": []},
    }
    assert completed_response(sse(done, completed))[0] == "original"
    with pytest.raises(ValueError, match="inconsistent"):
        completed_response(
            sse(
                {"type": "response.output_text.delta", "delta": "edited"},
                done,
                completed,
            )
        )
    with pytest.raises(ValueError, match="Missing completed"):
        completed_response(sse(done))


def test_public_api_completed_output() -> None:
    value: CompletedResponse = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "scene"}]}
        ],
    }
    assert (
        completed_response(sse({"type": "response.completed", "response": value}))[0]
        == "scene"
    )


@pytest.mark.parametrize("terminal_output", [False, True])
def test_completed_output_items_keep_their_indices_without_duplication(
    terminal_output: bool,
) -> None:
    items: list[OutputItem] = [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}
        for text in ("first", "second")
    ]
    text, response = completed_response(
        sse(
            {"type": "response.output_item.done", "output_index": 1, "item": items[1]},
            {"type": "response.output_item.done", "output_index": 0, "item": items[0]},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": items if terminal_output else [],
                },
            },
        )
    )
    assert text == response.output_text == "firstsecond"
    assert [item.model_dump(exclude_unset=True) for item in response.output] == items


@pytest.mark.parametrize("indices", [[1], [0, 2], [2, 5]])
def test_completed_output_items_allow_index_gaps(indices: list[int]) -> None:
    events: list[ResponseEvent] = []
    for index in reversed(indices):
        text = f"item {index}"
        events.extend(
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": index,
                    "delta": text,
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    },
                },
            ]
        )
    events.append(
        {
            "type": "response.completed",
            "response": {"status": "completed", "output": []},
        }
    )
    text, response = completed_response(sse(*events))
    assert text == response.output_text == "".join(f"item {index}" for index in indices)
    assert len(response.output) == len(indices)


@pytest.mark.parametrize("indices", [[-1], [False], [0, 0]])
def test_invalid_completed_output_item_indices_are_rejected(indices: list[int]) -> None:
    events: list[ResponseEvent] = [
        {"type": "response.output_item.done", "output_index": index, "item": {}}
        for index in indices
    ]
    events.append(
        {
            "type": "response.completed",
            "response": {"status": "completed", "output": []},
        }
    )
    with pytest.raises(ValueError, match="completed output item"):
        completed_response(sse(*events))


@pytest.mark.parametrize("index", [0, 1, 3])
def test_streamed_output_item_must_match_completed_text(index: int) -> None:
    with pytest.raises(ValueError, match="Final output differs"):
        completed_response(
            sse(
                {
                    "type": "response.output_text.done",
                    "output_index": index,
                    "text": "original",
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "edited"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": []},
                },
            )
        )


@pytest.mark.parametrize("status", ["failed", "incomplete"])
@pytest.mark.parametrize("index", [0, 1, 3])
def test_terminal_failure_preserves_streamed_output_items(
    status: str, index: int
) -> None:
    with pytest.raises(WitnessError) as failure:
        completed_response(
            sse(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "saved output"}],
                    },
                },
                {
                    "type": f"response.{status}",
                    "response": {"status": status, "output": []},
                },
            )
        )
    assert failure.value.code == f"provider_{status}"
    assert failure.value.response is not None
    assert failure.value.response.output_text == "saved output"


def test_chunked_provider_error_preserves_diagnostics() -> None:
    body = json.dumps(
        {
            "error": {
                "code": "unsupported_value",
                "param": "temperature",
                "message": "This model does not support temperature.",
            }
        }
    ).encode()
    chunks = (body[:20], body[20:])
    raw = b"HTTP/1.1 400 Bad Request\r\nTransfer-Encoding: chunked\r\n\r\n"
    raw += b"".join(
        f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks
    )
    raw += b"0\r\n\r\n"
    with pytest.raises(WitnessError) as failure:
        http_body(raw)
    assert failure.value.source == "provider"
    assert failure.value.status_code == 400
    assert failure.value.code == "provider_rejected"
    assert failure.value.provider_code == "unsupported_value"
    assert failure.value.param == "temperature"
    assert failure.value.provider_message == "This model does not support temperature."


def test_stream_error_preserves_provider_diagnostics() -> None:
    body = b'data: {"type":"error","code":"server_error","message":"Generation failed","param":null}\n\n'
    with pytest.raises(WitnessError) as failure:
        completed_response(body)
    assert failure.value.source == "provider"
    assert failure.value.status_code == 200
    assert failure.value.provider_code == "server_error"
    assert failure.value.provider_message == "Generation failed"
    assert failure.value.param is None


def test_subscription_rejection_preserves_detail() -> None:
    body = b'{"detail":"Input must be a list"}'
    raw = (
        f"HTTP/1.1 400 Bad Request\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    with pytest.raises(WitnessError) as failure:
        http_body(raw)
    error = failure.value
    assert error.source == "provider"
    assert error.status_code == 400
    assert error.code == "provider_rejected"
    assert error.provider_message == "Input must be a list"
    assert error.provider_code is None and error.param is None
    assert provider_error(400, body).provider_message == error.provider_message


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"null",
        b"[]",
        b"{}",
        b'{"detail":null}',
        b'{"detail":{"private":"value"}}',
    ],
)
def test_provider_error_does_not_expose_unstructured_bodies(body: bytes) -> None:
    error = provider_error(400, body)
    assert error.status_code == 400
    assert error.code == "provider_rejected"
    assert error.provider_message is None
    assert error.provider_code is None and error.param is None


def test_nested_stream_error_preserves_provider_diagnostics() -> None:
    body = b'data: {"type":"error","error":{"type":"insufficient_quota","code":"credit_balance_exhausted","message":"No API credits remaining","param":null}}\n\n'
    with pytest.raises(WitnessError) as failure:
        completed_response(body)
    assert failure.value.source == "provider"
    assert failure.value.status_code == 200
    assert failure.value.provider_code == "credit_balance_exhausted"
    assert failure.value.provider_message == "No API credits remaining"
    assert failure.value.param is None


def test_model_stream_preserves_events_then_raises_the_original_sdk_error() -> None:
    import openai
    from witness_client.response import ModelStream, model_result
    body = (b'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"part"}\n\n'
            b'data: {"type":"error","code":"server_error","message":"Interrupted"}\n\n')
    for _ in range(2):
        stream = model_result(200, body, "api", stream=True)
        assert isinstance(stream, ModelStream)
        event = next(stream)
        assert event.type == "response.output_text.delta" and event.delta == "part"
        with pytest.raises(openai.APIError) as failure:
            next(stream)
        assert failure.value.code == "server_error"
        with pytest.raises(StopIteration):
            next(stream)


PARTIAL = (
    b'data: {"type":"response.created","response":{"id":"fixture","status":"in_progress"}}\n\n'
    b'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"partial"}\n\n'
)


@pytest.mark.parametrize("stream", [False, True])
def test_truncated_sse_raises_an_sdk_error_instead_of_successful_eof_or_raw_value_error(stream: bool) -> None:
    import openai
    from witness_client.response import model_result

    with pytest.raises(openai.APIError):
        result = model_result(200, PARTIAL, "api", stream=stream)
        if stream:
            list(result)
