"""Credential-free entry point, executed only inside the miner sandbox."""

import base64
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Literal

from miner_runtime.observations import Execution, Inputs, Observation, ObservedWitness, Schedule, invoke_generate
from pydantic import BaseModel
from renderer_client import RenderOptions, RenderRejected
from renderer_client.client import DEFAULT_OPTIONS, RenderRequest
from witness_client import CapturedResponse
from witness_client.calls import RequestKey


wire = sys.stdout
sys.stdout = sys.stderr
lock = threading.Lock()


def exchange(**fields: object) -> str:
    with lock:
        wire.write(json.dumps(fields, separators=(",", ":")) + "\n")
        wire.flush()
        return sys.stdin.readline()


class ModelReply(BaseModel):
    exchange_id: str
    status: int
    body: str


class RenderReply(BaseModel):
    png: str = ""
    rejection: str | None = None


class Witness(ObservedWitness):
    def read(self, entry: Observation, replay: bool, key: RequestKey, body: bytes) -> CapturedResponse:
        reply = ModelReply.model_validate_json(
            exchange(
                operation="model",
                body=base64.b64encode(body).decode("ascii"),
                request_id=key.request_id,
                session_id=key.session_id,
            )
        )
        return CapturedResponse(reply.exchange_id, reply.status, base64.b64decode(reply.body, validate=True))


class Renderer:
    renderer_id = "recorded"

    def render(self, source: str, options: RenderOptions = DEFAULT_OPTIONS) -> bytes:
        RenderRequest.from_source(self.renderer_id, source, options)
        reply = RenderReply.model_validate_json(exchange(operation="render", source=source, query=options.query()))
        if reply.rejection is not None:
            raise RenderRejected(reply.rejection)
        return base64.b64decode(reply.png, validate=True)


def main(provider: Literal["api", "codex"]) -> None:
    image = Path("/prompt.png").read_bytes()
    seed = int(sys.argv[1])
    spec = importlib.util.spec_from_file_location("submission", "/source/main.py")
    if spec is None or spec.loader is None:
        raise ValueError("Submission has no main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, "/source")
    spec.loader.exec_module(module)
    witness = Witness(Schedule(Execution(inputs=Inputs.supplied(image, seed), provider=provider), record=True))
    output = invoke_generate(module.generate, image, seed, witness, Renderer())
    if output is not None and not isinstance(output, str):
        raise ValueError("generate() must return JavaScript or None")
    wire.write(json.dumps({"operation": "result", "output": output}) + "\n")
    wire.flush()


if __name__ == "__main__":
    try:
        if sys.argv[2] not in ("api", "codex"):
            raise ValueError("Unknown provider")
        main("api" if sys.argv[2] == "api" else "codex")
    except Exception as error:
        wire.write(json.dumps({"operation": "result", "error": type(error).__name__}) + "\n")
        wire.flush()
