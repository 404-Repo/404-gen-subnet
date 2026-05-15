"""Tests for subnet_common.competition.pod_config (via installed subnet-common)."""

import pytest

from subnet_common.competition.pod_config import (
    PodConfig,
    resolve_providers,
    runpod_allowed_cuda_versions,
)


def test_pod_config_defaults() -> None:
    c = PodConfig.model_validate({})
    assert c.platforms == []
    assert c.filters.cuda is None


def test_pod_config_full_yaml_dict() -> None:
    c = PodConfig.model_validate(
        {
            "platforms": ["RunPod", "targon"],
            "filters": {"cuda": 13},
        }
    )
    assert c.platforms == ["runpod", "targon"]
    assert c.filters.cuda == 13


def test_pod_config_cuda_yaml_float_13_0() -> None:
    """YAML ``cuda: 13.0`` (unquoted) parses as float 13.0."""
    c = PodConfig.model_validate({"filters": {"cuda": 13.0}})
    assert c.filters.cuda == 13
    assert runpod_allowed_cuda_versions(c.filters.cuda) == ["13.0"]


def test_pod_config_cuda_yaml_float_minor() -> None:
    c = PodConfig.model_validate({"filters": {"cuda": 12.4}})
    assert c.filters.cuda == "12.4"
    assert "12.4" in (runpod_allowed_cuda_versions(c.filters.cuda) or [])


def test_pod_config_invalid_platforms_type() -> None:
    with pytest.raises(TypeError):
        PodConfig.model_validate({"platforms": "runpod"})


def test_resolve_providers_miner_order() -> None:
    miner = PodConfig(platforms=["runpod", "targon"])
    orch = ["targon", "verda", "runpod"]
    assert resolve_providers(miner, orch) == ["runpod", "targon"]


def test_resolve_providers_empty_miner_uses_orch() -> None:
    miner = PodConfig()
    orch = ["verda", "runpod"]
    assert resolve_providers(miner, orch) == orch


def test_resolve_providers_no_overlap_falls_back() -> None:
    miner = PodConfig(platforms=["runpod"])
    orch = ["targon", "verda"]
    assert resolve_providers(miner, orch) == orch


def test_runpod_cuda_13_only() -> None:
    v = runpod_allowed_cuda_versions(13)
    assert v == ["13.0"]


def test_runpod_cuda_12_includes_newer() -> None:
    v = runpod_allowed_cuda_versions(12)
    assert v[0] == "12.0"
    assert v[-1] == "13.0"
    assert "12.5" in v


def test_runpod_cuda_string_minor() -> None:
    v = runpod_allowed_cuda_versions("12.4")
    assert "12.4" in v
    assert "11.8" not in v
    assert "12.3" not in v


def test_runpod_cuda_none() -> None:
    assert runpod_allowed_cuda_versions(None) is None


@pytest.mark.asyncio
async def test_get_file_from_repo_forbidden_returns_none() -> None:
    import httpx

    from subnet_common.github import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as client:
        gh = GitHubClient.with_client(client, repo="competition/repo", token="tok")
        assert await gh.get_file_from_repo("miner/private", "pod_config.yaml", "a" * 40) is None


@pytest.mark.asyncio
async def test_load_pod_config_unreadable_repo_returns_none() -> None:
    import httpx

    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.github import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as client:
        gh = GitHubClient.with_client(client, repo="competition/repo", token="tok")
        assert await load_pod_config(gh, "miner/private", "a" * 40) is None


@pytest.mark.asyncio
async def test_load_pod_config_missing() -> None:
    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.testing.mock_github import MockGitHubClient

    git = MockGitHubClient(files={})
    assert await load_pod_config(git, "owner/repo", "abc" * 10) is None


@pytest.mark.asyncio
async def test_load_pod_config_yaml_float_cuda() -> None:
    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.testing.mock_github import MockGitHubClient

    raw = "platforms:\n  - runpod\nfilters:\n  cuda: 13.0\n"
    git = MockGitHubClient(files={"owner/repo:pod_config.yaml": raw})
    cfg = await load_pod_config(git, "owner/repo", "a" * 40)
    assert cfg is not None
    assert cfg.filters.cuda == 13


@pytest.mark.asyncio
async def test_load_pod_config_from_repo_key() -> None:
    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.testing.mock_github import MockGitHubClient

    raw = "platforms:\n  - runpod\nfilters:\n  cuda: 13\n"
    git = MockGitHubClient(files={"owner/repo:pod_config.yaml": raw})
    cfg = await load_pod_config(git, "owner/repo", "a" * 40)
    assert cfg is not None
    assert cfg.platforms == ["runpod"]
    assert cfg.filters.cuda == 13
