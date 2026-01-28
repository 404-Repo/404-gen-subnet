"""DINOv3-based perceptual image distance computation."""

import numpy as np
import torch
from loguru import logger
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

from image_distance_service.settings import settings


_processor: AutoImageProcessor | None = None
_model: AutoModel | None = None
_current_device: torch.device | None = None


def load_model() -> None:
    """Load the DINOv3 model and processor into memory."""
    global _processor, _model

    if _model is not None:
        logger.warning("Model already loaded, skipping")
        return

    logger.info(f"Loading DINOv3 model: {settings.model_id} (revision: {settings.model_revision})")

    token = settings.hf_token.get_secret_value() if settings.hf_token else None

    _processor = AutoImageProcessor.from_pretrained(
        settings.model_id,
        revision=settings.model_revision,
        token=token,
    )
    _model = AutoModel.from_pretrained(
        settings.model_id,
        revision=settings.model_revision,
        token=token,
    )
    logger.info("DINOv3 model loaded successfully")


def unload_model() -> None:
    """Unload the model and free GPU/CPU memory."""
    global _processor, _model, _current_device

    if _model is None:
        logger.warning("Model not loaded, nothing to unload")
        return

    logger.info("Unloading DINOv3 model")
    del _model
    del _processor
    _model = None
    _processor = None
    _current_device = None

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    logger.info("DINOv3 model unloaded successfully")


def _ensure_model_on_device(device: torch.device) -> None:
    """Move model to device if not already there."""
    global _current_device

    if _model is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")

    if _current_device != device:
        logger.debug(f"Moving model to {device}")
        _model.to(device).eval()
        _current_device = device


def get_embedding(image: Image.Image, device: torch.device) -> np.ndarray:
    """
    Extract normalized embedding from an image using DINOv3.

    Args:
        image: PIL Image to embed
        device: torch device to use for inference

    Returns:
        Normalized embedding as numpy array
    """
    if _processor is None or _model is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")

    inputs = _processor(images=image.convert("RGB"), return_tensors="pt").to(device)

    with torch.inference_mode():
        outputs = _model(**inputs)
        embedding = outputs.pooler_output

    embedding_np = embedding.cpu().numpy().flatten()
    normalized = embedding_np / np.linalg.norm(embedding_np)

    return normalized


def compute_distance(image_a: Image.Image, image_b: Image.Image, device: torch.device) -> float:
    """
    Compute the perceptual distance between two images using DINOv3 embeddings.

    Uses cosine distance: 1 - dot(normalized_a, normalized_b)
    Range: 0 (identical) to 2 (opposite)

    Args:
        image_a: First image
        image_b: Second image
        device: torch device to use for inference

    Returns:
        Cosine distance between the image embeddings
    """
    _ensure_model_on_device(device)

    emb_a = get_embedding(image_a, device)
    emb_b = get_embedding(image_b, device)

    distance = float(1.0 - np.dot(emb_a, emb_b))

    return distance
