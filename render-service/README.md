# Render Service

A GPU-accelerated FastAPI service for rendering 3D Gaussian Splat (`.ply`) and mesh (`.glb`) files into 2×2 multi-view grid images.

## Overview

This service renders 3D models from 4 camera angles, combining them into a single grid image. It's designed for evaluating 3D model quality in automated pipelines.

- **Gaussian Splats (`.ply`)**: Uses [gsplat](https://github.com/nerfstudio-project/gsplat) for high-quality splat rendering
- **Meshes (`.glb`)**: Uses [pyrender](https://github.com/mmatl/pyrender) with 2× supersampling antialiasing

**Output Example:**  
A 1041×1041 PNG image with 4 views (front, right, back, left) arranged in a 2×2 grid with white background.

---

## Quick Start

### Docker (Recommended)

```bash
# Build
docker build -t render-service:latest .

# Run with GPU
docker run --gpus all -p 8000:8000 render-service:latest
```

### Local Development

```bash
# Create conda environment
./setup_env.sh

# Activate environment
conda activate splat-rendering

# Run the service
uvicorn render_service:app --host 0.0.0.0 --port 8000
```

---

## API Reference

### `GET /health`

Health check endpoint.

**Response:**
```json
{"status": "ok"}
```

### `POST /render_ply`

Render a Gaussian Splat PLY file to a 2×2 grid image.

**Request:**
- `file` (multipart/form-data): A `.ply` file containing Gaussian splat data
- `device` (query, optional): `"cuda"` or `"cpu"` (auto-detected if not specified)

**Response:**
- `200 OK`: PNG image (`image/png`)
- `400 Bad Request`: Invalid file format or empty payload
- `500 Internal Server Error`: Rendering failed

**Example:**
```bash
curl -X POST "http://localhost:8000/render_ply" \
  -F "file=@model.ply" \
  -o output.png
```

**Python Example:**
```python
import httpx

with open("model.ply", "rb") as f:
    response = httpx.post(
        "http://localhost:8000/render_ply",
        files={"file": ("model.ply", f, "application/octet-stream")},
    )
    
with open("output.png", "wb") as f:
    f.write(response.content)
```

### `POST /render_glb`

Render a GLB mesh file to a 2×2 grid image with 2× supersampling antialiasing.

**Request:**
- `file` (multipart/form-data): A `.glb` file containing a 3D mesh
- `device` (query, optional): `"cuda"` or `"cpu"` (auto-detected if not specified)

**Response:**
- `200 OK`: PNG image (`image/png`)
- `400 Bad Request`: Invalid file format or empty payload
- `500 Internal Server Error`: Rendering failed

**Example:**
```bash
curl -X POST "http://localhost:8000/render_glb" \
  -F "file=@model.glb" \
  -o output.png
```

**Python Example:**
```python
import httpx

with open("model.glb", "rb") as f:
    response = httpx.post(
        "http://localhost:8000/render_glb",
        files={"file": ("model.glb", f, "application/octet-stream")},
    )
    
with open("output.png", "wb") as f:
    f.write(response.content)
```

---



## Configuration

### Rendering Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `IMG_WIDTH` | 518 | Single view width (px) |
| `IMG_HEIGHT` | 518 | Single view height (px) |
| `CAM_RAD` | 2.5 | Camera distance from origin |
| `CAM_FOV_DEG` | 49.1 | Camera field of view (degrees) |
| `REF_BBOX_SIZE` | 1.5 | Reference bounding box for normalization |
| `GRID_VIEW_GAP` | 5 | Gap between grid cells (px) |

### Camera Angles

The 4 grid views are sampled at θ = [22.5°, 112.5°, 202.5°, 292.5°] with φ = -15° elevation.

---

## Project Structure

```
render-service/
├── Dockerfile              # Multi-stage Docker build
├── .dockerignore
├── conda_env.yml           # Conda environment (CUDA 12.8)
├── requirements.txt        # Python dependencies
├── setup_env.sh            # Local setup script
├── cleanup_env.sh          # Remove conda environment
├── render_service.py       # FastAPI application
├── splats_render_2x2_grid.py  # CLI batch renderer
└── renderers/
    ├── gs_renderer/        # Gaussian splat rendering
    │   ├── renderer.py
    │   ├── camera_utils.py
    │   └── gaussian_splatting/
    │       ├── gs_camera.py
    │       ├── gs_renderer.py
    │       └── gs_utils.py
    └── ply_loader/         # PLY file parsing
        ├── base.py
        └── loader.py
```

---

## Requirements

### Hardware
- NVIDIA GPU with CUDA 12.8 support (recommended)
- CPU fallback available but significantly slower

### Software (Docker)
- Docker with NVIDIA Container Toolkit
- `nvidia-docker` or `--gpus` flag support

### Software (Local)
- Miniconda or Anaconda
- NVIDIA CUDA Toolkit 12.8
- GCC 13.x



