"""Run inside the built worker, with the committed source mounted at argv[1]."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
from witness_pod import PROOF_PARAMETERS, VERIFY_SCRIPT, worker
from witness_pod.renderer import render_service


def main() -> None:
    source = Path(sys.argv[1])
    assert worker.PARAMETERS == PROOF_PARAMETERS
    assert VERIFY_SCRIPT.is_file()
    expected_key = source / "shared/subnet_common/witness/params/verifying.bin"
    assert (
        hashlib.sha256((PROOF_PARAMETERS / "verifying.bin").read_bytes()).digest()
        == hashlib.sha256(expected_key.read_bytes()).digest()
    )
    assert not (PROOF_PARAMETERS / "proving.bin").exists()
    assert not (PROOF_PARAMETERS / "circuit.bin").exists()
    assert not list(PROOF_PARAMETERS.parent.glob("*.test.mjs"))
    assert not (VERIFY_SCRIPT.parent / "tests").exists()
    assert Path("/usr/local/bin/privateauth").is_file()
    licences = {
        "LICENSE": source / "witness-client/LICENSE",
        "THIRD_PARTY_NOTICES.md": source / "witness-client/THIRD_PARTY_NOTICES.md",
        "VERIFIER_THIRD_PARTY_NOTICES.md": source
        / "witness-server/worker/witness_pod/verifier/THIRD_PARTY_NOTICES.md",
    }
    for name, original in licences.items():
        assert (
            hashlib.sha256((Path("/app/licenses") / name).read_bytes()).digest()
            == hashlib.sha256(original.read_bytes()).digest()
        )
    subprocess.run(
        ["node", str(VERIFY_SCRIPT), "health", "/tmp", str(PROOF_PARAMETERS)],
        check=True,
    )

    model = """export default function generate(THREE) {
      return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial());
    }"""
    statuses: dict[str, int] = {}
    with render_service(lambda: None) as renderer:
        assert renderer is not None
        status = Path(f"/proc/{renderer.pid}/status").read_text()
        uid = next(
            line for line in status.splitlines() if line.startswith("Uid:")
        ).split()[1:]
        assert uid == ["1001"] * 4, uid
        with httpx.Client(timeout=30, trust_env=False) as client:
            for route in ("/render", "/render/grid"):
                response = client.post(
                    "http://127.0.0.1:8002" + route,
                    params={
                        "lighting": "follow",
                        "thetas": "30,330,0,0",
                        "phis": "0,0,15,-30",
                        "img_size": 64,
                    },
                    headers={"X-Render-Purpose": "final"},
                    json={"source": model},
                )
                assert response.status_code == 200, response.text
                if route == "/render":
                    images = response.json()["images"]
                    assert isinstance(images, list) and len(images) == 4
                    for image in images:
                        assert isinstance(image, str)
                        assert base64.b64decode(image, validate=True).startswith(
                            b"\x89PNG\r\n\x1a\n"
                        )
                else:
                    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
                statuses[route] = response.status_code
        assert renderer.poll() is None
    print(
        json.dumps(
            {
                "renderer_uid": uid,
                "render_statuses": statuses,
                "verifier_and_licences": "passed",
            }
        )
    )


if __name__ == "__main__":
    main()
