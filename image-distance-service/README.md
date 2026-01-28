# Image Distance Service

FastAPI service that computes perceptual distance between two images using DINOv3 embeddings.

## Setup

```bash
poetry install
cp example.env .env
```

Then edit `.env` and add your Hugging Face token (required to access the gated DINOv3 model):

```bash
HF_TOKEN=hf_your_token_here
```

Get a token from: https://huggingface.co/settings/tokens

## Run

```bash
poetry run python main.py
```

## Usage

```bash
curl -X POST http://localhost:8000/distance \
  -H "Content-Type: application/json" \
  -d '{"url_a": "https://example.com/img1.png", "url_b": "https://example.com/img2.png"}'
```

Response:
```json
{
  "distance": 0.123
}
```

## API

- `GET /health` - Health check
- `POST /distance` - Compute cosine distance between two image embeddings (0 = identical, 2 = opposite)
  - Body: `{"url_a": "...", "url_b": "..."}`

## Configuration

Environment variables (see `example.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `8000` | Server port |
| `DEVICE` | `auto` | `cuda`, `cpu`, or `auto` |
| `MODEL_ID` | `facebook/dinov3-vits16-pretrain-lvd1689m` | Hugging Face model ID |
| `MODEL_REVISION` | `114c1379950215c8b35dfcd4e90a5c251dde0d32` | Specific model revision/commit hash |
| `HF_TOKEN` | (required) | Hugging Face API token for gated model |
| `DOWNLOAD_TIMEOUT` | `30` | Image download timeout (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level |
