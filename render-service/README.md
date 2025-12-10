# Render Service

A GPU-accelerated FastAPI service for rendering 3D Gaussian Splat (`.ply`) files into 2×2 multi-view grid images.

## Overview

This service uses [gsplat](https://github.com/nerfstudio-project/gsplat) to render PLY files containing Gaussian splat data from 4 camera angles, combining them into a single grid image. It's designed for evaluating 3D model quality in automated pipelines.

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

### `POST /render`

Render a PLY file to a 2×2 grid image.

**Request:**
- `file` (multipart/form-data): A `.ply` file containing Gaussian splat data
- `device` (query, optional): `"cuda"` or `"cpu"` (auto-detected if not specified)

**Response:**
- `200 OK`: PNG image (`image/png`)
- `400 Bad Request`: Invalid file format or empty payload
- `500 Internal Server Error`: Rendering failed

**Example:**
```bash
curl -X POST "http://localhost:8000/render" \
  -F "file=@model.ply" \
  -o output.png
```

**Python Example:**
```python
import httpx

with open("model.ply", "rb") as f:
    response = httpx.post(
        "http://localhost:8000/render",
        files={"file": ("model.ply", f, "application/octet-stream")},
    )
    
with open("output.png", "wb") as f:
    f.write(response.content)
```

---

## Batch Rendering (CLI)

For bulk rendering without the HTTP server:

```bash
python splats_render_2x2_grid.py \
  --folders /path/to/splats /path/to/more/splats \
  --output-folder ./outputs/renders \
  --N_instances 100 \
  --device cuda
```

**Arguments:**
| Argument | Description |
|----------|-------------|
| `--folders` | Input directories containing `.ply` or `.splat` files |
| `--output-folder` | Output directory for rendered PNGs |
| `--N_instances` | Max files to render (default: all) |
| `--device` | `cuda` or `cpu` (auto-detect if omitted) |

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

---

## Troubleshooting

### `CUDA out of memory`
The service clears GPU cache after each render. If issues persist:
- Use `device=cpu` for large models
- Reduce `IMG_WIDTH`/`IMG_HEIGHT` in `splats_render_2x2_grid.py`

### `gsplat` build fails
Ensure CUDA environment variables are set:
```bash
export CUDA_HOME=$CONDA_PREFIX
export CPATH="$CONDA_PREFIX/targets/x86_64-linux/include:$CONDA_PREFIX/include"
```

### Docker GPU not detected
Verify NVIDIA runtime:
```bash
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
```

