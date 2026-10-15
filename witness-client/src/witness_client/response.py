"""Decode one explicitly framed HTTP response and its completed Responses SSE."""

import json
import re
from collections.abc import Iterator

from openai.types.responses import Response

from .errors import WitnessError
from .types import CompletedResponse, OutputItem, ResponseEvent


def http_body(raw: bytes) -> bytes:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or len(head) > 32768:
        raise ValueError("Invalid HTTP response headers")
    lines = head.decode("latin1").split("\r\n")
    status = re.match(r"^HTTP/1\.1 ([2-5][0-9]{2})(?: |$)", lines.pop(0))
    if status is None:
        raise ValueError("Invalid provider HTTP status")
    headers: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(":")
        name = name.lower()
        if not sep or (
            name in headers
            and name in ("content-length", "transfer-encoding", "content-encoding")
        ):
            raise ValueError("Ambiguous HTTP response framing")
        headers[name] = value.strip()
    if headers.get("content-encoding", "identity") != "identity":
        raise ValueError("Compressed responses unsupported")
    if "content-length" in headers:
        content_length = headers["content-length"]
        if (
            "transfer-encoding" in headers
            or not re.fullmatch(r"0|[1-9][0-9]*", content_length)
            or int(content_length) != len(body)
        ):
            raise ValueError("Incomplete/additional HTTP body")
        if status[1] != "200":
            raise provider_error(int(status[1]), body)
        return body
    if headers.get("transfer-encoding") != "chunked":
        raise ValueError("Explicit HTTP response framing required")
    output = bytearray()
    offset = 0
    while offset < len(body):
        size_end = body.find(b"\r\n", offset)
        if size_end < 0:
            raise ValueError("Invalid HTTP chunk")
        size = body[offset:size_end]
        if not re.fullmatch(rb"[0-9a-fA-F]{1,8}", size):
            raise ValueError("Invalid HTTP chunk")
        length = int(size, 16)
        chunk_start = size_end + 2
        chunk_end = chunk_start + length
        if chunk_end + 2 > len(body) or body[chunk_end : chunk_end + 2] != b"\r\n":
            raise ValueError("Truncated HTTP chunk")
        if length == 0:
            if chunk_end + 2 != len(body):
                raise ValueError("Extra HTTP response bytes")
            if status[1] != "200":
                raise provider_error(int(status[1]), bytes(output))
            return bytes(output)
        output.extend(body[chunk_start:chunk_end])
        offset = chunk_end + 2
    raise ValueError("Missing final HTTP chunk")


def provider_error(status: int, body: bytes) -> WitnessError:
    """Keep structured OpenAI diagnostics without exposing arbitrary HTTP bodies."""
    details: dict[str, object] = {}
    try:
        value = json.loads(body)
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            details = value["error"]
        elif isinstance(value, dict):
            details = {"message": value.get("detail")}
    except ValueError:
        pass
    message, code, param = (details.get(key) for key in ("message", "code", "param"))
    return WitnessError(
        (
            "OpenAI reported a streaming error; inference was not retried"
            if status == 200
            else f"OpenAI returned HTTP {status}; inference was not retried"
        ),
        source="provider",
        status_code=status,
        code=(
            "provider_rejected"
            if status in (400, 401, 403, 404, 429)
            else "provider_error"
        ),
        provider_code=code if isinstance(code, str) else None,
        param=param if isinstance(param, str) else None,
        provider_message=message if isinstance(message, str) else None,
    )


def sse_events(body: bytes) -> Iterator[ResponseEvent]:
    """Decode JSON events from SSE frames without interpreting their contents."""
    for event in body.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip(" ")
            for line in event.splitlines()
            if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        value: ResponseEvent = json.loads(data)
        yield value


def completed_response(body: bytes) -> tuple[str, Response]:
    completed: CompletedResponse | None = None
    items: dict[int, OutputItem] = {}
    texts: dict[tuple[int, int], str] = {}
    deltas: dict[tuple[int, int], list[str]] = {}
    for value in sse_events(body):
        kind = value.get("type")
        key = (value.get("output_index", 0), value.get("content_index", 0))
        if kind == "response.output_text.delta":
            deltas.setdefault(key, []).append(value["delta"])
        elif kind == "response.output_text.done":
            if key in texts:
                raise ValueError("Duplicate completed output text")
            texts[key] = value["text"]
        elif kind == "response.output_item.done":
            index = value["output_index"]
            if type(index) is not int or index < 0 or index in items:
                raise ValueError("Invalid or duplicate completed output item index")
            items[index] = value["item"]
        elif kind in ("response.completed", "response.failed", "response.incomplete"):
            if completed is not None:
                raise ValueError("Multiple completed responses")
            completed = value["response"]
            if completed.get("status") != kind.removeprefix("response."):
                raise ValueError("Inconsistent terminal response status")
        elif kind == "error":
            details = (
                value if isinstance(value.get("error"), dict) else {"error": value}
            )
            raise provider_error(200, json.dumps(details).encode())
    if completed is None:
        raise ValueError("Missing completed OpenAI response")
    if not completed.get("output") and items:
        # Subscription completions can omit output already sent in item events.
        indexed_output = sorted(items.items())
        completed["output"] = [item for _, item in indexed_output]
    else:
        indexed_output = list(enumerate(completed.get("output", [])))
    final_texts: dict[tuple[int, int], str] = {}
    for i, item in indexed_output:
        if item.get("type") == "message":
            for j, content in enumerate(item.get("content", [])):
                if content.get("type") == "output_text":
                    final_texts[i, j] = content["text"]
    if final_texts and texts and final_texts != texts:
        raise ValueError("Final output differs from streamed text")
    texts = final_texts or texts
    if any("".join(parts) != texts.get(key) for key, parts in deltas.items()):
        raise ValueError("Missing/inconsistent completed output text")
    response = Response.model_construct(**completed)
    if response.status != "completed":
        raise WitnessError(
            f"OpenAI response {response.status}; inference was not retried",
            source="provider",
            code=f"provider_{response.status}",
            provider_code=response.error.code if response.error else None,
            provider_message=response.error.message if response.error else None,
            response=response,
        )
    return "".join(texts[key] for key in sorted(texts)), response
