"""Opt-in Pod artifact storage while preserving the Renderer byte interface."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict
from typing import cast

import httpx

from .client import (
    DEFAULT_OPTIONS,
    MAX_ERROR_BYTES,
    MAX_RESPONSE_BYTES,
    PNG_SIGNATURE,
    RendererClient,
    RenderOptions,
    RenderRejected,
    RenderRequest,
    RenderUnavailable,
    _read_bounded,
)


class ArtifactUnavailable(RenderUnavailable):
    def __init__(self, code: str, status: int) -> None:
        self.code, self.status = code, status
        super().__init__(f"Artifact service {status}: {code}")


class ArtifactClient(RendererClient):
    """A renderer scoped to one prompt, with explicit runner-owned final saving."""

    def __init__(
        self,
        url: str,
        *,
        renderer_id: str,
        round: str,
        prompt_sha256: str,
        prompt_image: bytes,
        seed: int,
        submission_commit: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 150,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round) or not _digest(prompt_sha256):
            raise ValueError("Expected a round and frozen prompt hash")
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("Expected a uint64 seed")
        if not re.fullmatch(r"[0-9a-f]{40}", submission_commit):
            raise ValueError("Expected the frozen source commit")
        if (
            not prompt_image
            or len(prompt_image) > 8 * 1024 * 1024
            or hashlib.sha256(prompt_image).hexdigest() != prompt_sha256
        ):
            raise ValueError("Prompt bytes differ from the frozen input")
        super().__init__(url, renderer_id=renderer_id, transport=transport, timeout=timeout)
        self.generation = {"round": round, "prompt_sha256": prompt_sha256, "seed": seed}
        self.prompt_image = base64.b64encode(prompt_image).decode("ascii")
        self.submission_commit = submission_commit
        self._origin = self._url.removesuffix("/render/grid")

    def _exchange(
        self,
        method: str,
        path: str,
        value: object | None = None,
        *,
        png: bool = False,
    ) -> tuple[bytes, httpx.Headers, int]:
        try:
            with self._http.stream(method, self._origin + path, json=value) as response:
                limit = MAX_RESPONSE_BYTES if png and response.is_success else MAX_ERROR_BYTES
                raw = _read_bounded(response, limit)
                if response.status_code == 422 and png:
                    return raw, response.headers, 422
                if not response.is_success:
                    details = _object(json.loads(raw))
                    code = details.get("error")
                    raise ArtifactUnavailable(
                        code if isinstance(code, str) else "invalid_response", response.status_code
                    )
                return raw, response.headers, response.status_code
        except httpx.HTTPError as error:
            raise RenderUnavailable("Artifact transport failed; retry the same saved operation") from error
        except (ValueError, KeyError, TypeError) as error:
            raise RenderUnavailable("Invalid artifact response") from error

    def _render_request(self, source: str, options: RenderOptions, include_png: bool) -> dict[str, object]:
        RenderRequest.from_source(self.renderer_id, source, options)
        return {
            "generation": self.generation,
            "submission_commit": self.submission_commit,
            "source": source,
            "options": asdict(options),
            "include_png": include_png,
        }

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        raw, headers, status = self._exchange(
            "POST",
            "/artifacts/render",
            self._render_request(source, options, True),
            png=True,
        )
        if (
            not _digest(headers.get("X-Artifact-Id"))
            or headers.get("X-Source-Sha256") != hashlib.sha256(source.encode()).hexdigest()
            or headers.get("X-Renderer-Id") != self.renderer_id
        ):
            raise RenderUnavailable("Saved render binding differs")
        if status == 422:
            try:
                rejection = RenderRejected(raw.decode())
            except (ValueError, UnicodeError) as error:
                raise RenderUnavailable("Invalid saved rejection") from error
            raise rejection
        if (
            headers.get("Content-Type", "").split(";", 1)[0] != "image/png"
            or not raw.startswith(PNG_SIGNATURE)
            or headers.get("X-Png-Sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise RenderUnavailable("Saved render PNG differs")
        return raw

    def render_saved(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> dict[str, object]:
        """Save a render and return its receipt without downloading its PNG."""
        raw, _, _ = self._exchange("POST", "/artifacts/render", self._render_request(source, options, False))
        try:
            artifact = _object(_object(json.loads(raw))["artifact"])
            generation = _object(artifact["generation"])
            if (
                any(generation.get(k) != v for k, v in self.generation.items())
                or artifact["submission_commit"] != self.submission_commit
                or _object(artifact["source"])["sha256"] != hashlib.sha256(source.encode()).hexdigest()
                or _object(artifact["profile"])["image_digest"] != self.renderer_id
                or artifact["options"] != json.loads(json.dumps(asdict(options)))
                or not _digest(artifact["artifact_id"])
            ):
                raise ValueError("Saved render binding differs")
            return artifact
        except (ValueError, KeyError, TypeError) as error:
            raise RenderUnavailable("Invalid saved render receipt") from error

    def _submission(self, raw: bytes) -> dict[str, object]:
        try:
            saved = _object(_object(json.loads(raw))["submission"])
            generation = _object(saved["generation"])
            if (
                any(generation.get(k) != v for k, v in self.generation.items())
                or saved["submission_commit"] != self.submission_commit
                or not _digest(saved["revision"])
                or type(saved["output_is_none"]) is not bool
                or saved["output_is_none"] != (saved["source"] is None)
            ):
                raise ValueError("Saved submission binding differs")
            return saved
        except (ValueError, KeyError, TypeError) as error:
            raise RenderUnavailable("Invalid saved submission") from error

    def save_final(self, output: str | None, *, expected_revision: str | None = None) -> dict[str, object]:
        """Select saved bytes; render a changed or never-rendered result once if needed."""
        if output is not None and not isinstance(output, str):
            raise ValueError("generate() must return JavaScript or None")
        if expected_revision is not None and not _digest(expected_revision):
            raise ValueError("Invalid expected submission revision")
        source_hash = None
        if output is not None:
            source_hash = RenderRequest.from_source(self.renderer_id, output, DEFAULT_OPTIONS).source_sha256
        value = {
            "generation": self.generation,
            "submission_commit": self.submission_commit,
            "source_sha256": source_hash,
            "expected_revision": expected_revision,
            "prompt_image": self.prompt_image if output is not None else None,
        }
        try:
            raw, _, _ = self._exchange("POST", "/submissions", value)
        except ArtifactUnavailable as error:
            if error.status != 404 or error.code != "source_not_saved" or output is None:
                raise
            self.render_saved(output)
            raw, _, _ = self._exchange("POST", "/submissions", value)
        saved = self._submission(raw)
        if source_hash is not None and _object(saved["source"]).get("sha256") != source_hash:
            raise RenderUnavailable("Saved final source differs")
        if saved["output_is_none"] != (output is None):
            raise RenderUnavailable("Saved final outcome differs")
        return saved

    def get_submission(self) -> dict[str, object]:
        raw, _, _ = self._exchange("GET", "/submissions/" + str(self.generation["prompt_sha256"]))
        return self._submission(raw)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return cast(dict[str, object], value)


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
