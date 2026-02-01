"""Shared types for GPU provider clients."""

from enum import Enum

from pydantic import BaseModel


class GPUProvider(str, Enum):
    TARGON = "targon"
    VERDA = "verda"


class ContainerInfo(BaseModel):
    """Unified container information from any GPU provider."""

    name: str
    url: str
    delete_identifier: str
    provider: GPUProvider


class GPUClientError(Exception):
    """Base exception for all GPU client errors."""
