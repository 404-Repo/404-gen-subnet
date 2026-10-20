"""Generate and render a Three.js candidate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .images import png_data_url

if TYPE_CHECKING:
    from renderer_client import Renderer
    from witness_client import WitnessClient


def run(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None:
    """Generate one candidate, check that it renders, and return its JavaScript."""
    image_url = png_data_url(image)
    result = witness.responses.create(
        stream=True,
        model="gpt-6.1-sol",
        instructions=(
            "Return only a Three.js ES module with export default function generate(THREE). "
            "Use procedural geometry and MeshStandardMaterial. Fit within [-0.5, 0.5], Y-up, +Z forward. "
            "No imports, external assets or randomness."
        ),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"Reconstruct this image. Generation seed: {seed}."},
                    {"type": "input_image", "image_url": image_url, "detail": "auto"},
                ],
            }
        ],
    )
    source: str = result.output_text
    if not source:
        return None
    renderer.render(source)
    return source
