"""Encode PNG bytes for OpenAI image inputs."""

import base64


def png_data_url(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")
