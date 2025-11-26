#!/usr/bin/env python3
import os
import sys
import argparse
import struct
import math
import io
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
import torch
from PIL import Image
from tqdm import tqdm

from renderers.gs_renderer.renderer import Renderer
from renderers.ply_loader import PlyLoader

# Constants
VIEWS_NUMBER = 16
THETA_ANGLES = np.linspace(0, 360, num=VIEWS_NUMBER)
PHI_ANGLES = np.full_like(THETA_ANGLES, -15.0)
GRID_VIEW_INDICES = [1, 5, 9, 13]  # 4 views for 2x2 grid
IMG_WIDTH = 518
IMG_HEIGHT = 518
GRID_VIEW_GAP = 5

# Camera settings
CAM_RAD = 2.5
CAM_FOV_DEG = 49.1
REF_BBOX_SIZE = 1.5


def splat_to_ply_bytes(b: bytes) -> bytes:
    """Convert splat bytes to PLY format bytes"""
    n = len(b) // 32
    out = bytearray()
    out += b"ply\nformat binary_little_endian 1.0\n"
    out += f"element vertex {n}\n".encode()
    out += (b"property float x\nproperty float y\nproperty float z\n"
            b"property float opacity\n"
            b"property float rot_0\nproperty float rot_1\nproperty float rot_2\nproperty float rot_3\n"
            b"property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n"
            b"property float scale_0\nproperty float scale_1\nproperty float scale_2\n"
            b"end_header\n")
    for i in range(0, n * 32, 32):
        px, py, pz, s0, s1, s2, cr, cg, cb, op, r0, r1, r2, r3 = struct.unpack('<fff fff 4B 4B', b[i:i+32])
        s0 = math.log(s0) if s0 > 0 else -1e-6
        s1 = math.log(s1) if s1 > 0 else -1e-6
        s2 = math.log(s2) if s2 > 0 else -1e-6
        f0 = (cr / 255.0 - 0.5) * 2.0 * 1.772196
        f1 = (cg / 255.0 - 0.5) * 2.0 * 1.772196
        f2 = (cb / 255.0 - 0.5) * 2.0 * 1.772196
        o = op / 255.0
        o = max(0.0001, min(0.9999, o))
        o = math.log(o / (1 - o))
        q0 = r0 / 128.0 - 1.0
        q1 = r1 / 128.0 - 1.0
        q2 = r2 / 128.0 - 1.0
        q3 = r3 / 128.0 - 1.0
        px, py = -px, -py
        out += struct.pack('<3f1f4f3f3f', px, py, pz, o, q0, q1, q2, q3, f0, f1, f2, s0, s1, s2)
    return bytes(out)


def combine_images4(images: list[torch.Tensor]) -> Image.Image:
    """Combine 4 images into 2x2 grid - matches pipeline.py combine_images4"""
    row_width = IMG_WIDTH * 2 + GRID_VIEW_GAP
    column_height = IMG_HEIGHT * 2 + GRID_VIEW_GAP
    
    combined_image = Image.new("RGB", (row_width, column_height), color="black")
    
    pil_images = [Image.fromarray(img.detach().cpu().numpy()) for img in images]
    
    combined_image.paste(pil_images[0], (0, 0))
    combined_image.paste(pil_images[1], (IMG_WIDTH + GRID_VIEW_GAP, 0))
    combined_image.paste(pil_images[2], (0, IMG_HEIGHT + GRID_VIEW_GAP))
    combined_image.paste(pil_images[3], (IMG_WIDTH + GRID_VIEW_GAP, IMG_HEIGHT + GRID_VIEW_GAP))
    
    return combined_image


def render_splat_grid(splat_path: Path, device: torch.device, ply_loader: PlyLoader, renderer: Renderer) -> Image.Image | None:
    """Render a splat file to 2x2 grid"""
    try:
        # Read file; convert only if it's a .splat, pass-through if it's a .ply
        with open(splat_path, 'rb') as f:
            file_bytes = f.read()

        if splat_path.suffix.lower() == '.splat':
            ply_bytes = splat_to_ply_bytes(file_bytes)
        else:
            ply_bytes = file_bytes
        ply_buffer = io.BytesIO(ply_bytes)
        
        # Load PLY from buffer
        gs_data = ply_loader.from_buffer(ply_buffer)
        gs_data = gs_data.send_to_device(device)
        
        # Extract only the 4 grid view angles
        theta_angles = THETA_ANGLES[GRID_VIEW_INDICES].astype(np.float32)
        phi_angles = PHI_ANGLES[GRID_VIEW_INDICES].astype(np.float32)
        
        # Render the 4 views with white background
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
        
        # Combine into 2x2 grid
        grid = combine_images4(images)
        
        # Clean up GPU memory
        del gs_data
        del images
        if device.type == 'cuda':
            torch.cuda.empty_cache()
        
        return grid
        
    except Exception as e:
        print(f"[ERROR] Failed to render {splat_path}: {e}")
        if device.type == 'cuda':
            torch.cuda.empty_cache()
        return None


def process_directory(
    input_dir: Path,
    output_folder: Path,
    ply_loader: PlyLoader,
    renderer: Renderer,
    device: torch.device,
    remaining: int | None,
) -> int:
    """Recursively render .splat/.ply files and mirror their structure inside the output root."""
    candidates = sorted(
        [*input_dir.rglob("*.splat"), *input_dir.rglob("*.ply")],
        key=lambda path: path.as_posix(),
    )

    if not candidates:
        print(f"[WARN] No .splat/.ply files found in {input_dir}")
        return 0

    processed = 0
    for splat_path in tqdm(candidates, desc=f"Rendering {input_dir.name}", unit="file"):
        if remaining is not None and remaining <= 0:
            break
        out_dir = output_folder / input_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{splat_path.stem}.png"
        if out_path.exists():
            processed += 1
            if remaining is not None:
                remaining -= 1
            continue

        try:
            grid = render_splat_grid(splat_path, device, ply_loader, renderer)
            if grid is not None:
                grid.save(out_path)
                processed += 1
                if remaining is not None:
                    remaining -= 1
            else:
                tqdm.write(f"[WARN] Failed to render {splat_path}")
        except Exception as e:
            tqdm.write(f"[WARN] Exception rendering {splat_path}: {e}")

    return processed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render 2x2 grids from splat files while mirroring the source structure"
    )
    parser.add_argument(
        "--folders",
        type=str,
        nargs="+",
        default=None,
        help="One or more input folders to scan recursively (required)",
    )
    parser.add_argument(
        "--output-folder",
        type=str,
        default="./outputs/2x2_renders",
        help="Folder where rendered grids are stored",
    )
    parser.add_argument(
        "--N_instances",
        type=int,
        default=None,
        help="Maximum number of files to render (useful for debugging)",
    )
    args = parser.parse_args()

    assert torch.cuda.is_available(), "Rendering requires a CUDA-capable GPU"
    device = torch.device("cuda")
    print("Using device: cuda")

    assert args.folders is not None, "At least one folder must be provided"
    inputs = [Path(p) for p in args.folders]

    out_folder = Path(args.output_folder)
    out_folder.mkdir(parents=True, exist_ok=True)

    ply_loader = PlyLoader()
    renderer = Renderer()

    remaining = args.N_instances
    for in_folder in inputs:
        if remaining is not None and remaining <= 0:
            break
        if not in_folder.exists():
            sys.stderr.write(f"[WARN] Skipping missing directory: {in_folder}\n")
            continue
        processed = process_directory(
            in_folder,
            out_folder,
            ply_loader,
            renderer,
            device=device,
            remaining=remaining,
        )
        if remaining is not None:
            remaining -= processed

if __name__ == '__main__':
    # Set CUDA environment variables before importing torch/gsplat
    if 'CONDA_PREFIX' in os.environ:
        conda_prefix = os.environ['CONDA_PREFIX']
        nvcc_path = os.path.join(conda_prefix, 'bin', 'nvcc')
        if os.path.exists(nvcc_path):
            os.environ['CUDA_HOME'] = conda_prefix
            os.environ['CUDACXX'] = nvcc_path
            os.environ['CUDA_INCLUDE_PATH'] = f"{conda_prefix}/include:{conda_prefix}/targets/x86_64-linux/include"
            os.environ['CPATH'] = f"{conda_prefix}/include:{conda_prefix}/targets/x86_64-linux/include" + (os.pathsep + os.environ.get('CPATH', '') if os.environ.get('CPATH') else '')
            os.environ['PATH'] = os.path.join(conda_prefix, 'bin') + os.pathsep + os.environ.get('PATH', '')
    main()