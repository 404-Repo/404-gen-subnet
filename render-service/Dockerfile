# -----------------------------------------------------------------------------
# Stage 1: Builder - compile gsplat with CUDA
# -----------------------------------------------------------------------------
FROM nvidia/cuda:12.8.0-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3-pip \
    git \
    cmake \
    ninja-build \
    gcc-12 \
    g++-12 \
    && rm -rf /var/lib/apt/lists/*

# Use gcc-13 as default
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 100

# Set up Python
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && python3 -m pip install --upgrade pip setuptools wheel

# Set CUDA environment
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${CUDA_HOME}/bin:${PATH}"
ENV LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"
ENV TORCH_CUDA_ARCH_LIST="7.0;7.5;8.0;8.6;8.9;9.0"

# Install PyTorch with CUDA 12.8 first (needed to build gsplat)
RUN pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128

# Build and install gsplat
RUN pip install --no-build-isolation git+https://github.com/nerfstudio-project/gsplat.git@v1.5.3


# -----------------------------------------------------------------------------
# Stage 2: Runtime - minimal image with pre-built packages
# -----------------------------------------------------------------------------
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYOPENGL_PLATFORM=egl

# Install Python runtime and headless OpenGL dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3-pip \
    libgomp1 \
    # Headless EGL/OpenGL support
    libegl1 \
    libgl1 \
    libgles2 \
    libglvnd0 \
    # Fallback/compatibility
    libosmesa6 \
    # Other deps
    libglib2.0-0 \
    libxrender1 \
    libxext6 \
    libsm6 \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# Set CUDA environment
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${CUDA_HOME}/bin:${PATH}"
ENV LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/dist-packages /usr/local/lib/python3.11/dist-packages
COPY --from=builder /usr/lib/python3.11 /usr/lib/python3.11

# Install remaining dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY renderers/ ./renderers/
COPY render_service.py .
COPY render.py .
COPY constants.py .
COPY utils/ ./utils/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Expose the FastAPI port
EXPOSE 8000

# Run the service
CMD ["python3", "-m", "uvicorn", "render_service:app", "--host", "0.0.0.0", "--port", "8000"]

