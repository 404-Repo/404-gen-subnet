from __future__ import annotations

import asyncio
import io
from typing import Literal

import torch
from fastapi import FastAPI, File, HTTPException, Response, UploadFile

from renderers.gs_renderer.renderer import Renderer
from renderers.ply_loader import PlyLoader
from splats_render_2x2_grid import (
    CAM_FOV_DEG,
    CAM_RAD,
    GRID_VIEW_INDICES,
    IMG_HEIGHT,
    IMG_WIDTH,
    PHI_ANGLES,
    REF_BBOX_SIZE,
    THETA_ANGLES,
    combine_images4,
)


app = FastAPI(title="Splat Render Service", version="1.0.0")


def _resolve_device(preferred: str | None) -> torch.device:
    if preferred is not None:
        return torch.device(preferred)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _render_grid_from_ply_bytes(ply_bytes: bytes, device: torch.device) -> bytes:
    if not ply_bytes:
        raise ValueError("Empty PLY payload")

    ply_loader = PlyLoader()
    renderer = Renderer()

    try:
        gs_data = ply_loader.from_buffer(io.BytesIO(ply_bytes))
        gs_data = gs_data.send_to_device(device)

        theta_angles = THETA_ANGLES[GRID_VIEW_INDICES].astype("float32")
        phi_angles = PHI_ANGLES[GRID_VIEW_INDICES].astype("float32")
        bg_color = torch.tensor([1.0, 1.0, 1.0], dtype=torch.float32).to(device)

        images = renderer.render_gs(
            gs_data,
            views_number=4,
            img_width=IMG_WIDTH,
            img_height=IMG_HEIGHT,
            theta_angles=theta_angles,
            phi_angles=phi_angles,
            cam_rad=CAM_RAD,
            cam_fov=CAM_FOV_DEG,
            ref_bbox_size=REF_BBOX_SIZE,
            bg_color=bg_color,
        )

        grid = combine_images4(images)
        buffer = io.BytesIO()
        grid.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer.read()
    finally:
        del ply_loader
        del renderer
        if device.type == "cuda":
            torch.cuda.empty_cache()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/render")
async def render(
    file: UploadFile = File(...),
    device: Literal["cuda", "cpu"] | None = None,
) -> Response:
    filename = file.filename or ""
    if not filename.lower().endswith(".ply"):
        raise HTTPException(status_code=400, detail="Only .ply files are supported")

    payload = await file.read()
    torch_device = _resolve_device(device)

    try:
        png_bytes = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _render_grid_from_ply_bytes(payload, torch_device)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to render uploaded file: {exc}"
        ) from exc

    return Response(content=png_bytes, media_type="image/png")

