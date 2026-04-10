"""Placeholder Three.js scene generator.

Returns a simple low-poly car scene as a stand-in for real 3D generation.
In a real miner, this would be replaced by your model inference pipeline.
"""

import json


def generate_car_scene() -> bytes:
    """Generate a Three.js-compatible JSON scene of a simple car.

    This is a placeholder — real miners would run their 3D generation model here.
    """
    scene = {
        "metadata": {"version": 4.6, "type": "Object", "generator": "miner-reference"},
        "geometries": [
            {
                "uuid": "body-geom",
                "type": "BoxGeometry",
                "width": 4,
                "height": 1.2,
                "depth": 2,
            },
            {
                "uuid": "cabin-geom",
                "type": "BoxGeometry",
                "width": 2,
                "height": 1,
                "depth": 1.8,
            },
            {
                "uuid": "wheel-geom",
                "type": "CylinderGeometry",
                "radiusTop": 0.4,
                "radiusBottom": 0.4,
                "height": 0.3,
                "radialSegments": 16,
            },
        ],
        "materials": [
            {"uuid": "body-mat", "type": "MeshStandardMaterial", "color": 0xCC0000, "metalness": 0.6, "roughness": 0.4},
            {"uuid": "cabin-mat", "type": "MeshStandardMaterial", "color": 0x333333, "metalness": 0.3, "roughness": 0.5},
            {"uuid": "wheel-mat", "type": "MeshStandardMaterial", "color": 0x111111, "metalness": 0.2, "roughness": 0.8},
        ],
        "object": {
            "uuid": "car-root",
            "type": "Group",
            "name": "PlaceholderCar",
            "children": [
                {
                    "uuid": "body",
                    "type": "Mesh",
                    "name": "Body",
                    "geometry": "body-geom",
                    "material": "body-mat",
                    "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0.6, 0, 1],
                },
                {
                    "uuid": "cabin",
                    "type": "Mesh",
                    "name": "Cabin",
                    "geometry": "cabin-geom",
                    "material": "cabin-mat",
                    "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -0.3, 1.7, 0, 1],
                },
                {
                    "uuid": "wheel-fl",
                    "type": "Mesh",
                    "name": "WheelFL",
                    "geometry": "wheel-geom",
                    "material": "wheel-mat",
                    "matrix": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, -1.3, 0, 1.1, 1],
                },
                {
                    "uuid": "wheel-fr",
                    "type": "Mesh",
                    "name": "WheelFR",
                    "geometry": "wheel-geom",
                    "material": "wheel-mat",
                    "matrix": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, -1.3, 0, -1.1, 1],
                },
                {
                    "uuid": "wheel-rl",
                    "type": "Mesh",
                    "name": "WheelRL",
                    "geometry": "wheel-geom",
                    "material": "wheel-mat",
                    "matrix": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 1.3, 0, 1.1, 1],
                },
                {
                    "uuid": "wheel-rr",
                    "type": "Mesh",
                    "name": "WheelRR",
                    "geometry": "wheel-geom",
                    "material": "wheel-mat",
                    "matrix": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 1.3, 0, -1.1, 1],
                },
            ],
        },
    }
    return json.dumps(scene, separators=(",", ":")).encode()
