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
    assert c.platforms == ["runpod"]
    assert c.filters.cuda == 13


def test_pod_config_platforms_only_targon_verda_becomes_empty() -> None:
    c = PodConfig.model_validate({"platforms": ["verda", "targon"]})
    assert c.platforms == []


def test_pod_config_only_targon_verda_resets_filters_to_defaults() -> None:
    """After stripping targon/verda, nothing Runpod-relevant remains — ignore filters too."""
    c = PodConfig.model_validate(
        {"platforms": ["verda", "targon"], "filters": {"cuda": 13}},
    )
    assert c.platforms == []
    assert c.filters.cuda is None


def test_pod_config_explicit_empty_platforms_still_honors_filters() -> None:
    """Empty platforms without a strip pass does not clear filters (e.g. cuda-only hint)."""
    c = PodConfig.model_validate({"platforms": [], "filters": {"cuda": 13}})
    assert c.platforms == []
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


def test_pod_config_cuda_yaml_float_12_8() -> None:
    """Unquoted ``cuda: 12.8`` (YAML float) is supported."""
    c = PodConfig.model_validate({"filters": {"cuda": 12.8}})
    assert c.filters.cuda == "12.8"
    assert "12.8" in (runpod_allowed_cuda_versions(c.filters.cuda) or [])


def test_pod_config_invalid_cuda_string_becomes_none() -> None:
    for bad in ("abc", "not-a-version", "12.4.x", "12..4", ".12"):
        c = PodConfig.model_validate({"filters": {"cuda": bad}})
        assert c.filters.cuda is None


@pytest.mark.asyncio
async def test_load_pod_config_invalid_cuda_ignores_filter_keeps_rest() -> None:
    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.testing.mock_github import MockGitHubClient

    raw = "platforms:\n  - runpod\nfilters:\n  cuda: not-a-version\n"
    git = MockGitHubClient(files={"owner/repo:pod_config.yaml": raw})
    cfg = await load_pod_config(git, "owner/repo", "a" * 40)
    assert cfg is not None
    assert cfg.platforms == ["runpod"]
    assert cfg.filters.cuda is None


@pytest.mark.asyncio
async def test_load_pod_config_yaml_cuda_12_8_float() -> None:
    """YAML ``cuda: 12.8`` (float); a space after ``:`` is required — ``cuda:12.8`` is invalid YAML."""
    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.testing.mock_github import MockGitHubClient

    raw = "filters:\n  cuda: 12.8\n"
    git = MockGitHubClient(files={"owner/repo:pod_config.yaml": raw})
    cfg = await load_pod_config(git, "owner/repo", "a" * 40)
    assert cfg is not None
    assert cfg.filters.cuda == "12.8"


def test_pod_config_invalid_platforms_type() -> None:
    with pytest.raises(TypeError):
        PodConfig.model_validate({"platforms": "runpod"})


def test_resolve_providers_miner_order() -> None:
    miner = PodConfig(platforms=["runpod", "targon"])
    orch = ["targon", "verda", "runpod"]
    assert resolve_providers(miner, orch) == ["runpod"]


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
async def test_load_pod_config_github_5xx_returns_none() -> None:
    """Transient GitHub errors must not propagate (generation uses defaults)."""
    import httpx

    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.github import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as client:
        gh = GitHubClient.with_client(client, repo="competition/repo", token="tok")
        assert await load_pod_config(gh, "owner/miner", "a" * 40) is None


@pytest.mark.asyncio
async def test_load_pod_config_github_connect_error_returns_none() -> None:
    import httpx

    from subnet_common.competition.pod_config import load_pod_config
    from subnet_common.github import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as client:
        gh = GitHubClient.with_client(client, repo="competition/repo", token="tok")
        assert await load_pod_config(gh, "owner/miner", "a" * 40) is None


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
