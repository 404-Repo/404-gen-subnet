"""Build a local generation image from development or integrated release source."""

import argparse
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from leader_generation_service.runtime import ROOT, digest, execute


SOURCES = (
    "leader-generation-service/Dockerfile.generation",
    "leader-generation-service/build_image.py",
    "leader-generation-service/image_manifest.py",
    "miner-runtime",
    "renderer-client",
    "witness-client",
    "shared/subnet_common/witness/params",
    "witness-server/worker/witness_pod/verifier/params",
    "witness-server/worker/witness_pod/verifier/THIRD_PARTY_NOTICES.md",
    "NOTICE.md",
)
DOCKERFILE = "leader-generation-service/Dockerfile.generation"


def source_commit(root: Path, release_branch: str | None) -> str:
    head = execute(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    if release_branch is not None:
        if execute(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"]):
            raise RuntimeError("Release image requires a clean, committed checkout")
        branch = execute(["git", "-C", str(root), "rev-parse", f"refs/heads/{release_branch}"]).decode().strip()
        if branch != head:
            raise RuntimeError("Release source must be the agreed release branch tip")
        remote = (
            execute(["git", "-C", str(root), "ls-remote", "--exit-code", "origin", f"refs/heads/{release_branch}"])
            .decode()
            .split()
        )
        if not remote or remote[0] != head:
            raise RuntimeError("Release source must be pushed to the agreed remote release branch")
    return head


def prepare_context(root: Path, target: Path, commit: str, *, development: bool) -> dict[str, object]:
    hashes = {}
    if development:
        names = execute(["git", "-C", str(root), "ls-files", "-z", "--cached", "--", *SOURCES])
        entries = set(names.split(b"\0")) - {b""}
        entries.update(name.encode() for name in SOURCES if (root / name).is_file())
        for raw in sorted(entries):
            name = raw.decode()
            source = root / name
            if not source.is_file():
                continue
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    else:
        archive = execute(["git", "-C", str(root), "archive", "--format=tar", commit, *SOURCES])
        with tarfile.open(fileobj=io.BytesIO(archive)) as package:
            package.extractall(target, filter="data")
    for path in sorted(target.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(target))] = digest(path)
    metadata: dict[str, object] = {
        "runtime_commit": commit,
        "purpose": "development" if development else "release",
        "source_digest": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "files": hashes,
    }
    (target / "source.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--development", action="store_true")
    mode.add_argument("--release-branch", help="Agreed local and origin release branch; requires remote verification")
    parser.add_argument("--tag", required=True, help="Local image tag; this command never publishes")
    parser.add_argument(
        "--offline", action="store_true", help="Use cached bases and wheels with build networking disabled"
    )
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--python-image", help="Development-only Python base override")
    parser.add_argument("--go-image", help="Development-only Go base override")
    args = parser.parse_args()
    if not args.development and (args.offline or args.python_image or args.go_image):
        parser.error("Offline builds and base overrides are development-only")
    commit = source_commit(ROOT, args.release_branch)
    with tempfile.TemporaryDirectory(prefix="leader-image-") as temporary:
        context = Path(temporary)
        metadata = prepare_context(ROOT, context, commit, development=args.development)
        wheels = context / ".wheelhouse"
        if args.wheelhouse is None:
            wheels.mkdir()
        else:
            shutil.copytree(args.wheelhouse, wheels)
        bases = dict(
            re.findall(r"^ARG (PYTHON_IMAGE|GO_IMAGE)=(.+)$", (context / DOCKERFILE).read_text(), re.MULTILINE)
        )
        for key, override in (("PYTHON_IMAGE", args.python_image), ("GO_IMAGE", args.go_image)):
            if override is not None:
                bases[key] = override
        metadata["base_images"] = bases
        (context / "source.json").write_text(json.dumps(metadata, indent=2) + "\n")
        if args.offline:
            for image in set(bases.values()):
                execute(["docker", "image", "inspect", image])
        command = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--pull=false",
            "--tag",
            args.tag,
            "--file",
            DOCKERFILE,
        ]
        for key, image in bases.items():
            command.extend(["--build-arg", f"{key}={image}"])
        command.extend(["--build-arg", f"SOURCE_COMMIT={commit}"])
        if args.offline:
            command.extend(["--network=none", "--build-arg", "OFFLINE=1"])
        execute([*command, "."], cwd=context, timeout=3600, capture=False)


if __name__ == "__main__":
    main()
