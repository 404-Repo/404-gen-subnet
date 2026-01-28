from contextlib import asynccontextmanager
from io import BytesIO
from typing import AsyncIterator

import httpx
import torch
from fastapi import FastAPI, HTTPException
from loguru import logger
from PIL import Image
from pydantic import BaseModel

from image_distance_service.distance import compute_distance, load_model, unload_model
from image_distance_service.settings import settings


class DistanceRequest(BaseModel):
    url_a: str
    url_b: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle - load model on startup, unload on shutdown."""
    load_model()
    yield
    unload_model()


app = FastAPI(title="Image Distance Service", version="1.0.0", lifespan=lifespan)


def resolve_device() -> torch.device:
    """Resolve the device to use based on settings."""
    if settings.device == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(settings.device)


async def download_image(client: httpx.AsyncClient, url: str) -> Image.Image:
    """Download an image from a URL and return as PIL Image."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image from {url}: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image from {url}: {e}")

    try:
        return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to decode image from {url}: {e}")


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/distance")
async def distance(request: DistanceRequest) -> dict[str, float]:
    """
    Compute the perceptual distance between two images.

    Args:
        request: JSON body with url_a and url_b

    Returns:
        Dictionary with the computed distance value
    """
    device = resolve_device()
    logger.info(f"Computing distance using device: {device}")

    timeout = httpx.Timeout(settings.download_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        logger.debug(f"Downloading image A: {request.url_a}")
        image_a = await download_image(client, request.url_a)

        logger.debug(f"Downloading image B: {request.url_b}")
        image_b = await download_image(client, request.url_b)

    logger.info(f"Images downloaded: A={image_a.size}, B={image_b.size}")

    try:
        result = compute_distance(image_a, image_b, device)
    except RuntimeError as e:
        logger.error(f"Model not ready: {e}")
        raise HTTPException(status_code=503, detail="Model not loaded")
    except Exception as e:
        logger.exception(f"Distance computation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Distance computation failed: {e}")

    logger.info(f"Calculated distance: {result:.6f}")

    return {"distance": result}
