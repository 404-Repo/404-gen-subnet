import io
import threading


from loguru import logger
import numpy as np
from PIL import Image
import torch

import constants as const
from renderers.gs_renderer.renderer import Renderer
from renderers.mesh_renderer.renderer import GLBRenderer
from renderers.ply_loader import PlyLoader
from utils import image as img_utils


_thread_local = threading.local()

def _get_glb_renderer() -> GLBRenderer:
    """Get or create a GLBRenderer for the current thread."""
    if not hasattr(_thread_local, 'glb_renderer'):
        logger.info(f"Creating GLBRenderer for thread {threading.current_thread().name}")
        _thread_local.glb_renderer = GLBRenderer()
    return _thread_local.glb_renderer


def grid_from_ply_bytes(ply_bytes: bytes, device: torch.device) -> bytes:
    logger.info(f"Starting PLY rendering, payload size: {len(ply_bytes)} bytes")
    
    if not ply_bytes:
        raise ValueError("Empty PLY payload")

    logger.debug("Initializing PlyLoader and Renderer")
    ply_loader = PlyLoader()
    renderer = Renderer()

    logger.debug("Loading Gaussian splat data from PLY")
    gs_data = ply_loader.from_buffer(io.BytesIO(ply_bytes))
    gs_data = gs_data.send_to_device(device)
    logger.debug(f"Gaussian splat data loaded and sent to {device}")

    theta_angles = const.THETA_ANGLES[const.GRID_VIEW_INDICES].astype("float32")
    phi_angles = const.PHI_ANGLES[const.GRID_VIEW_INDICES].astype("float32")
    bg_color = torch.tensor(const.BG_COLOR, dtype=torch.float32).to(device)
    logger.debug(f"Rendering {len(theta_angles)} views at {const.IMG_WIDTH}x{const.IMG_HEIGHT}")

    images = renderer.render_gs(
        gs_data,
        views_number=4,
        img_width=const.IMG_WIDTH,
        img_height=const.IMG_HEIGHT,
        theta_angles=theta_angles,
        phi_angles=phi_angles,
        cam_rad=const.CAM_RAD,
        cam_fov=const.CAM_FOV_DEG,
        ref_bbox_size=const.REF_BBOX_SIZE,
        bg_color=bg_color,
    )
    logger.info(f"Rendered {len(images)} views, combining into grid")

    images = [Image.fromarray(img.detach().cpu().numpy()) for img in images]
    grid = img_utils.combine4(images)
    buffer = io.BytesIO()
    grid.save(buffer, format="PNG")
    buffer.seek(0)
    png_bytes = buffer.read()
    logger.info(f"PLY rendering complete, output size: {len(png_bytes)} bytes")
    return png_bytes


def grid_from_glb_bytes(glb_bytes: bytes) -> bytes:
    """Convenience wrapper that uses thread-local GLBRenderer instances."""
    renderer = _get_glb_renderer()
    return renderer.render_grid(glb_bytes)
