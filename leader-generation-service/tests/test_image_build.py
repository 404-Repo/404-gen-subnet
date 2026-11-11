import json
import shutil

import pytest

import build_image
import image_manifest
from leader_generation_service.runtime import execute


@pytest.fixture
def source(tmp_path):
    def git(*args):
        return execute(["git", "-C", str(tmp_path), *args]).decode().strip()

    git("init", "-b", "main")
    package = tmp_path / "miner-runtime"
    package.mkdir()
    (package / "source.py").write_text("committed source\n")
    key = tmp_path / "shared/subnet_common/witness/params/verifying.bin"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"fixture verification key")
    params = tmp_path / "witness-server/worker/witness_pod/verifier/params"
    params.mkdir(parents=True)
    (params / "verifying.bin").symlink_to("../../../../../shared/subnet_common/witness/params/verifying.bin")
    git("add", ".")
    git(
        "-c",
        "user.name=Offline Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Image source",
    )
    return tmp_path, git("rev-parse", "HEAD")


@pytest.mark.parametrize("state", ["pushed", "dirty", "unmerged", "unpushed"])
def test_release_build_requires_clean_integrated_pushed_source(source, monkeypatch, state):
    root, commit = source
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if "ls-remote" in argv:
            ref = commit if state != "unpushed" else "0" * 40
            return f"{ref}\trefs/heads/main\n".encode()
        if "rev-parse" in argv and argv[-1] == "refs/heads/main" and state == "unmerged":
            return b"0" * 40
        return execute(argv, **kwargs)

    monkeypatch.setattr(build_image, "execute", command)
    if state == "dirty":
        (root / "miner-runtime/source.py").write_text("uncommitted source\n")
    if state == "pushed":
        assert build_image.source_commit(root, "main") == commit
    else:
        with pytest.raises(RuntimeError, match="clean|release branch|pushed"):
            build_image.source_commit(root, "main")
    if state in ("dirty", "unmerged"):
        assert all("ls-remote" not in call for call in calls)


@pytest.mark.parametrize("development", [False, True])
def test_image_context_uses_committed_release_or_tracked_development_source(source, tmp_path, monkeypatch, development):
    root, commit = source
    monkeypatch.setattr(build_image, "SOURCES", ("miner-runtime",))
    (root / "miner-runtime/source.py").write_text("development source\n")
    (root / "miner-runtime/auth.json").write_text("fixture private untracked login")
    context = tmp_path / "context"
    context.mkdir()
    metadata = build_image.prepare_context(root, context, commit, development=development)
    assert (context / "miner-runtime/source.py").read_text() == (
        "development source\n" if development else "committed source\n"
    )
    assert set(metadata["files"]) == {"miner-runtime/source.py"}
    assert metadata["purpose"] == ("development" if development else "release")
    assert json.loads((context / "source.json").read_bytes())["runtime_commit"] == commit


def test_release_context_provides_a_self_contained_verification_key(source, tmp_path, monkeypatch):
    root, commit = source
    params = "witness-server/worker/witness_pod/verifier/params"
    monkeypatch.setattr(build_image, "SOURCES", (params, "shared/subnet_common/witness/params"))
    context = tmp_path / "context"
    context.mkdir()

    build_image.prepare_context(root, context, commit, development=False)
    image_params = tmp_path / "image-params"
    shutil.copytree(context / params, image_params, symlinks=True)

    assert (image_params / "verifying.bin").read_bytes() == b"fixture verification key"


def test_offline_development_build_uses_local_bases_and_disables_network(source, tmp_path, monkeypatch):
    root, _ = source
    path = root / build_image.DOCKERFILE
    path.parent.mkdir(parents=True)
    path.write_text("ARG GO_IMAGE=fixture-go:cached\nARG PYTHON_IMAGE=fixture-python:cached\n")
    commands = []
    command = build_image.execute

    def capture(argv, **kwargs):
        if argv[0] == "docker":
            commands.append(argv)
            return b"fixture local image"
        return command(argv, **kwargs)

    monkeypatch.setattr(build_image, "ROOT", root)
    monkeypatch.setattr(build_image, "execute", capture)
    monkeypatch.setattr("sys.argv", ["build_image.py", "--development", "--offline", "--tag", "fixture:local"])
    build_image.main()
    assert {call[-1] for call in commands[:-1]} == {"fixture-go:cached", "fixture-python:cached"}
    assert all(call[:3] == ["docker", "image", "inspect"] for call in commands[:-1])
    assert "--network=none" in commands[-1]
    assert "--pull=false" in commands[-1]


@pytest.mark.parametrize("mismatch", [None, "version", "library", "key"])
def test_image_manifest_checks_installed_sdk_and_native_prover(tmp_path, monkeypatch, mismatch):
    source = tmp_path / "source"
    for package, version in (("witness-client", "0.2.18"), ("renderer-client", "0.1.5"), ("miner-runtime", "0.1.6")):
        path = source / package
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text(f'[project]\nname = "404-{package}"\nversion = "{version}"\n')
    wheels = source / "wheels"
    wheels.mkdir()
    for name in ("witness", "renderer", "runtime"):
        (wheels / f"{name}.whl").write_bytes(name.encode())
    (source / "go-build.txt").write_text("fixture Go build metadata")
    built_library = source / "witness-client/src/witness_client/libwitnessproof.so"
    built_library.parent.mkdir(parents=True)
    built_library.write_bytes(b"fresh Linux library")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "libwitnessproof.so").write_bytes(b"wrong" if mismatch == "library" else built_library.read_bytes())
    monkeypatch.setattr(image_manifest.witness_client, "__file__", str(installed / "__init__.py"))
    parameters = tmp_path / "params"
    parameters.mkdir()
    (parameters / "verifying.bin").write_bytes(b"public proof key")
    expected = image_manifest.digest(parameters / "verifying.bin")
    monkeypatch.setattr(image_manifest, "PARAMETERS", {"verifying.bin": expected})
    monkeypatch.setattr(
        image_manifest,
        "Prover",
        lambda path: type("Prover", (), {"verifying_key_sha256": "wrong" if mismatch == "key" else expected})(),
    )
    versions = {"404-witness-client": "0.2.18", "404-renderer-client": "0.1.5", "404-miner-runtime": "0.1.6"}
    monkeypatch.setattr(
        image_manifest.importlib.metadata, "version", lambda name: "0.0.0" if mismatch == "version" else versions[name]
    )
    metadata = tmp_path / "source.json"
    metadata.write_text(json.dumps({"runtime_commit": "a" * 40, "purpose": "release"}))
    output = tmp_path / "provenance.json"
    if mismatch is None:
        image_manifest.record(source, metadata, parameters, output)
        recorded = json.loads(output.read_bytes())
        assert recorded["runtime_commit"] == "a" * 40
        assert recorded["library_sha256"] == image_manifest.digest(built_library)
        assert recorded["parameters"]["verifying.bin"] == expected
        assert len(recorded["wheels"]) == 3
    else:
        with pytest.raises(RuntimeError, match="differs"):
            image_manifest.record(source, metadata, parameters, output)
