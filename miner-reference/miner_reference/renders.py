"""Persist each prompt's ordered render outcomes before returning them to Python."""

import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path

from renderer_client import Renderer, RenderOptions, RenderRejected, RenderRequest
from witness_client.calls import canonical
from witness_client.state import save


class SavedRenderer:
    def __init__(self, renderer: Renderer, directory: Path, slots: threading.Semaphore) -> None:
        self.renderer, self.directory, self.slots = renderer, directory, slots
        self.renderer_id = renderer.renderer_id
        self.position = 0
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def render(self, source: str, options: RenderOptions = RenderOptions()) -> bytes:
        request = canonical(asdict(RenderRequest.from_source(self.renderer_id, source, options)))
        key = hashlib.sha256(request).hexdigest()
        stem = self.directory / f"{self.position:02}"
        self.position += 1
        checkpoint, png_file = stem.with_suffix(".json"), stem.with_suffix(".png")
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_bytes())
            if saved["request_sha256"] != key:
                raise ValueError("Saved render request differs")
            if saved.get("rejection") is not None:
                raise RenderRejected(saved["rejection"])
            png = png_file.read_bytes()
            if hashlib.sha256(png).hexdigest() != saved["png_sha256"]:
                raise ValueError("Saved render PNG differs")
            return png
        save(stem.with_suffix(".js"), source.encode())
        save(stem.with_suffix(".request.json"), request)
        try:
            with self.slots:
                png = self.renderer.render(source, options)
        except RenderRejected as error:
            save(checkpoint, canonical({"request_sha256": key, "rejection": error.detail_json}))
            raise
        save(png_file, png)
        save(checkpoint, canonical({"request_sha256": key, "png_sha256": hashlib.sha256(png).hexdigest()}))
        return png
