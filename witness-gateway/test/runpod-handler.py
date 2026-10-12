"""RunPod acknowledgement worker for queue tests."""
from __future__ import annotations

import json
from typing import cast

import runpod


def handler(job: dict[str, object]) -> dict[str, object]:
    data = cast(dict[str, object], job["input"])
    assert data["operation"] == "exchange"
    result = {"queue_smoke": True, "hotkey": data["hotkey"],
              "round": data["round"], "exchange_id": data["exchange_id"], "provider_calls": 0}
    print(json.dumps({"job_id": job["id"], "output": result}), flush=True)
    return result


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
