"""DINOv3 image embeddings for the multi-stage judge.

Lazy-loads `facebook/dinov3-vits16-pretrain-lvd1689m` (or a caller-pinned revision) on
first call and caches the model + processor in module globals. Producers compute
embeddings for the prompt image and the rendered views, persist them as an .npz alongside
the view PNGs, and the judge reads them back to pick the best prompt-matching view per
side.

Embeddings are L2-normalized so cosine similarity reduces to dot product, matching
the convention used by `image-distance-service`.

Configuration (HF token, model id, revision, device) is passed in by the caller — the
library does not read environment variables. Each service owns its own typed config
and threads it through to keep coupling explicit.

Third-party model. DINOv3 is published by Meta AI Research under the DINOv3 License
(separate from this repo's MIT license). The model is gated on Hugging Face — operators
must accept Meta's terms and supply `HF_TOKEN`. See `NOTICE.md` at the repo root for
attribution, citation, and operator obligations.
"""

from __future__ import annotations

import asyncio
import io
import threading
from typing import Any

import numpy as np


_DEFAULT_MODEL_ID = "facebook/dinov3-vits16-pretrain-lvd1689m"

_lock = threading.Lock()
_processor: Any = None
_model: Any = None
_device: Any = None
_cached_key: tuple[str, str] | None = None  # (model_id, revision)


def _resolve_device(device_setting: str | None) -> Any:
    import torch

    if not device_setting or device_setting == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(device_setting)


def _load_model(
    model_id: str,
    revision: str,
    hf_token: str | None,
    device: str | None,
) -> tuple[Any, Any, Any]:
    """Load DINOv3 + processor once per (model_id, revision). Cache invalidates on change."""
    global _processor, _model, _device, _cached_key

    key = (model_id, revision)
    with _lock:
        if _model is not None and _cached_key == key:
            return _processor, _model, _device

        from loguru import logger
        from transformers import AutoImageProcessor, AutoModel

        logger.info(f"Loading DINOv3 embedding model: {model_id} (revision={revision})")

        # B615 (HF unsafe download): the revision kwarg is required at the call site (no
        # default), so this is always a concrete branch/tag/commit chosen by the operator
        # via service settings. Pin to a commit hash in production for reproducibility.
        _processor = AutoImageProcessor.from_pretrained(model_id, revision=revision, token=hf_token)  # nosec B615
        _model = AutoModel.from_pretrained(model_id, revision=revision, token=hf_token)  # nosec B615
        _device = _resolve_device(device)
        _model.to(_device).eval()
        _cached_key = key

        logger.info(f"DINOv3 embedding model loaded on {_device}")
        return _processor, _model, _device


def _embed_sync(
    images: list[bytes],
    model_id: str,
    revision: str,
    hf_token: str | None,
    device: str | None,
) -> np.ndarray:
    """Compute L2-normalized DINOv3 CLS embeddings for a batch of PNG/JPEG bytes."""
    if not images:
        return np.zeros((0, 0), dtype=np.float32)

    import torch
    from PIL import Image

    processor, model, dev = _load_model(model_id, revision, hf_token, device)

    pil_images = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
    inputs = processor(images=pil_images, return_tensors="pt").to(dev)

    with torch.inference_mode():
        outputs = model(**inputs)
        embeddings = outputs.pooler_output  # (N, D), pre-normalized

    arr: np.ndarray = embeddings.cpu().numpy().astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # avoid div-by-zero on degenerate embeddings
    normalized: np.ndarray = arr / norms
    return normalized


async def calculate_embeddings(
    images: list[bytes],
    *,
    revision: str,
    hf_token: str | None = None,
    model_id: str = _DEFAULT_MODEL_ID,
    device: str | None = None,
) -> np.ndarray:
    """L2-normalized DINOv3 embeddings, shape (N, D). Async wrapper around blocking inference.

    First call loads + caches the model. Subsequent calls reuse the cache as long as
    `(model_id, revision)` is unchanged.

    `revision` is required (a branch, tag, or commit hash on Hugging Face). Pin to a
    commit hash in production for reproducibility — drift between model revisions
    changes embedding outputs and corrupts judge comparisons across rounds.
    `hf_token` is required for gated models (e.g. DINOv3). `device` accepts "cuda",
    "cpu", or None/"auto" to pick automatically.
    """
    return await asyncio.to_thread(_embed_sync, images, model_id, revision, hf_token, device)


async def build_embeddings_npz(
    prompt_bytes: bytes,
    white_views: dict[str, bytes],
    log_id: str,
    *,
    revision: str,
    hf_token: str | None,
) -> bytes:
    """Compute DINOv3 embeddings for prompt + 8 white views; pack into a single .npz.

    Embeddings of the same view are identical between white and gray bg in practice,
    so we only embed the white set — saves 4 forward passes per stem.

    The npz layout is the consumer contract for the judge: an array named "prompt"
    plus one "view_<name>" per WHITE_VIEWS entry, in WHITE_VIEWS order.

    `revision` is the Hugging Face revision (branch/tag/commit) for the embedding model;
    see `calculate_embeddings` for why it must be pinned in production.
    """
    from loguru import logger

    from subnet_common.render import WHITE_VIEWS

    view_names = [v.name for v in WHITE_VIEWS]
    view_bytes = [white_views[name] for name in view_names]

    logger.debug(f"{log_id}: computing embeddings for prompt + {len(view_names)} views")
    start = asyncio.get_running_loop().time()
    embeds = await calculate_embeddings([prompt_bytes, *view_bytes], revision=revision, hf_token=hf_token)
    elapsed = asyncio.get_running_loop().time() - start
    if embeds.shape[0] != 1 + len(view_names):
        raise RuntimeError(f"expected {1 + len(view_names)} embeddings, got {embeds.shape[0]}")

    arrays: dict[str, np.ndarray] = {"prompt": embeds[0]}
    for i, name in enumerate(view_names, start=1):
        arrays[f"view_{name}"] = embeds[i]

    buf = io.BytesIO()
    np.savez(buf, **arrays)  # type: ignore[arg-type]  # numpy stubs miss the **kwargs form
    logger.debug(f"{log_id}: embeddings computed in {elapsed:.1f}s; npz packed ({buf.tell() / 1024:.1f}KB)")
    return buf.getvalue()
