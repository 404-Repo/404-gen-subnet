"""Miner-declared pod placement hints for generation-orchestrator."""

from __future__ import annotations

import math
from typing import Annotated, Any, Self

import yaml
from loguru import logger
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from subnet_common.github import GitHubClient

# Host CUDA versions accepted by Runpod REST `allowedCudaVersions` (newest first).
# https://docs.runpod.io/api-reference/pods/POST/pods
_RUNPOD_CUDA_CATALOG: tuple[str, ...] = (
    "13.0",
    "12.9",
    "12.8",
    "12.7",
    "12.6",
    "12.5",
    "12.4",
    "12.3",
    "12.2",
    "12.1",
    "12.0",
    "11.8",
)


def _coerce_cuda_yaml_number(v: Any) -> int | str | None:
    """YAML ``cuda: 13.0`` parses as float; normalize to int or dotted string before ``int | str`` union."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError("filters.cuda must be a number or string, not a boolean")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("filters.cuda must be a finite number")
        if abs(v - round(v)) < 1e-9:
            return int(round(v))
        return format(v, ".6f").rstrip("0").rstrip(".") or "0"
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    raise TypeError(f"filters.cuda must be int, float, str, or null, got {type(v).__name__}")


CudaFilterField = Annotated[
    int | str | None,
    BeforeValidator(_coerce_cuda_yaml_number),
]


class PodFilters(BaseModel):
    """Scheduling filters (provider-specific support varies)."""

    cuda: CudaFilterField = Field(
        default=None,
        description=(
            "Minimum host CUDA capability (major or dotted), e.g. 13, 13.0 (YAML number), "
            "'12.4', or quoted '13.0'"
        ),
    )


# Targon/Verda do not consume pod_config placement hints today (Runpod-only, e.g. allowedCudaVersions).
_PLATFORMS_IGNORED_FOR_HINTS: frozenset[str] = frozenset({"targon", "verda"})


class PodConfig(BaseModel):
    """Contents of miner repo root ``pod_config.yaml``."""

    platforms: list[str] = Field(
        default_factory=list,
        description="Preferred providers in YAML order; only Runpod uses hints today (Targon/Verda stripped).",
    )
    filters: PodFilters = Field(default_factory=PodFilters)

    @field_validator("platforms", mode="before")
    @classmethod
    def _normalize_platforms(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise TypeError("platforms must be a list of strings")
        out: list[str] = []
        for item in v:
            if not isinstance(item, str):
                raise TypeError("platforms entries must be strings")
            s = item.strip().lower()
            if s:
                out.append(s)
        return out

    @model_validator(mode="after")
    def _strip_platforms_without_pod_hints(self) -> Self:
        """Drop Targon/Verda from ``platforms``; only Runpod honors this file's deploy hints today."""
        dropped = [p for p in self.platforms if p in _PLATFORMS_IGNORED_FOR_HINTS]
        if not dropped:
            return self
        kept = [p for p in self.platforms if p not in _PLATFORMS_IGNORED_FOR_HINTS]
        logger.info(
            "pod_config.yaml platforms {} ignored (only Runpod honors placement hints today)",
            dropped,
        )
        self.platforms = kept
        if not kept:
            # No Runpod-relevant entries left; treat like missing pod_config for deploy hints
            # (default provider order via resolve_providers, no allowedCudaVersions).
            self.filters = PodFilters()
            logger.info(
                "pod_config.yaml: no platform hints remain after removing {} — "
                "filters reset to defaults (no Runpod CUDA filter)",
                dropped,
            )
        return self


def _cuda_tuple(version: str) -> tuple[int, int]:
    parts = version.strip().split(".", 1)
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return (major, minor)


def _parse_cuda_minimum(cuda: int | str) -> tuple[int, int]:
    if isinstance(cuda, int):
        return (cuda, 0)
    s = str(cuda).strip()
    if not s:
        raise ValueError("empty cuda filter")
    return _cuda_tuple(s)


def _version_ge(catalog_entry: str, minimum: tuple[int, int]) -> bool:
    return _cuda_tuple(catalog_entry) >= minimum


def runpod_allowed_cuda_versions(cuda_filter: int | str | None) -> list[str] | None:
    """Map ``filters.cuda`` to Runpod ``allowedCudaVersions`` (None = no filter)."""
    if cuda_filter is None:
        return None
    minimum = _parse_cuda_minimum(cuda_filter)
    allowed = [v for v in _RUNPOD_CUDA_CATALOG if _version_ge(v, minimum)]
    if not allowed:
        logger.warning(
            "No Runpod catalog CUDA versions satisfy filters.cuda={!r}; omitting allowedCudaVersions",
            cuda_filter,
        )
        return None
    return sorted(allowed, key=_cuda_tuple)


def resolve_providers(miner: PodConfig, orchestrator_providers: list[str]) -> list[str]:
    """Order deploy attempts: miner ``platforms`` ∩ orchestrator list (miner order), else orchestrator order."""
    orch = [p.strip().lower() for p in orchestrator_providers if p.strip()]
    valid = frozenset({"targon", "verda", "runpod"})
    orch = [p for p in orch if p in valid]
    if not orch:
        return []

    if not miner.platforms:
        return list(orch)

    miner_ordered = [p for p in miner.platforms if p in valid]
    intersection = [p for p in miner_ordered if p in set(orch)]
    if intersection:
        return intersection

    logger.warning(
        "pod_config platforms {!r} do not overlap with orchestrator GPU_PROVIDERS {!r}; using orchestrator order",
        miner.platforms,
        orch,
    )
    return list(orch)


async def load_pod_config(git: GitHubClient, repo: str, commit: str) -> PodConfig | None:
    """Load ``pod_config.yaml`` from ``repo`` at ``commit``.

    Returns ``None`` if the file is missing, invalid, or **unreadable** (private repo without
    token access — ``get_file_from_repo`` yields no content for 401/403/404).
    """
    raw = await git.get_file_from_repo(repo, "pod_config.yaml", commit)
    if raw is None:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("pod_config.yaml YAML error in {}@{}: {}", repo, commit[:7], e)
        return None
    if not isinstance(data, dict):
        logger.warning("pod_config.yaml must be a mapping in {}@{}", repo, commit[:7])
        return None
    try:
        return PodConfig.model_validate(data)
    except Exception as e:
        logger.warning("pod_config.yaml validation failed in {}@{}: {}", repo, commit[:7], e)
        return None
