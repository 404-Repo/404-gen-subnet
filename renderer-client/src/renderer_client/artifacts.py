"""A prompt-scoped renderer with final result saving through the witness Pod."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import cast

import httpx

from .client import (
    MAX_ERROR_BYTES,
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
    """Render previews and save final JavaScript or an explicit generation failure."""

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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", round):
            raise ValueError("Expected a round")
        if hashlib.sha256(prompt_image).hexdigest() != prompt_sha256:
            raise ValueError("Prompt bytes differ from the frozen input")
        super().__init__(url, renderer_id=renderer_id, transport=transport, timeout=timeout)
        self.generation = {"round": round, "prompt_sha256": prompt_sha256, "seed": seed}
        self.prompt_image = base64.b64encode(prompt_image).decode("ascii")
        self.submission_commit = submission_commit
        self._origin = self._url.removesuffix("/render/grid")

    def _exchange(self, method: str, path: str, value: object | None = None) -> dict[str, object]:
        try:
            with self._http.stream(method, self._origin + path, json=value) as response:
                raw = _read_bounded(response, MAX_ERROR_BYTES)
                if response.status_code == 422:
                    raise RenderRejected(raw.decode())
                if not response.is_success:
                    details = json.loads(raw)
                    raise ArtifactUnavailable(str(details.get("error", "invalid_response")), response.status_code)
                result = json.loads(raw)["submission"]
                if not isinstance(result, dict):
                    raise ValueError("Expected a saved result")
                return cast(dict[str, object], result)
        except httpx.HTTPError as error:
            raise RenderUnavailable("Final-save transport failed; retry saving") from error
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise RenderUnavailable("Invalid final-save response") from error

    def save_final(self, output: str | None) -> dict[str, object]:
        """Save the source, judging views and embeddings; errors leave saving retryable."""
        if output is not None:
            if not isinstance(output, str):
                raise ValueError("generate() must return JavaScript or None")
            RenderRequest.from_source(self.renderer_id, output, RenderOptions())
        return self._exchange(
            "POST",
            "/submissions",
            {
                "generation": self.generation,
                "submission_commit": self.submission_commit,
                "source": output,
                "prompt_image": self.prompt_image if output is not None else None,
            },
        )

    def get_submission(self) -> dict[str, object]:
        return self._exchange("GET", "/submissions/" + str(self.generation["prompt_sha256"]))
