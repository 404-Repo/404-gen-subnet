import io
import time

from loguru import logger
import numpy as np
from OpenGL.GL import GL_LINEAR
from PIL import Image
import pyrender
import trimesh

import constants as const
from utils import coords
from utils import image as img_utils


class GLBRenderer:
    """Reusable GLB renderer with pre-initialized scene, camera, light, and renderer."""

    def __init__(self):
        self._scene: pyrender.Scene | None = None
        self._cam_node: pyrender.Node | None = None
        self._light_node: pyrender.Node | None = None
        self._renderer: pyrender.OffscreenRenderer | None = None
        self._ssaa_factor = 2
        self._theta_angles: np.ndarray | None = None
        self._phi_angles: np.ndarray | None = None

    def init(self) -> None:
        """Initialize scene, camera, light, and renderer (call once on startup)."""
        if self._renderer is not None:
            logger.warning("GLBRenderer already initialized, skipping")
            return

        logger.info("Initializing GLBRenderer...")
        init_start = time.perf_counter()

        # Create scene
        self._scene = pyrender.Scene(bg_color=[255, 255, 255, 0], ambient_light=[0.3, 0.3, 0.3])

        # Camera
        cam = pyrender.PerspectiveCamera(yfov=const.CAM_FOV_DEG * np.pi / 180.0)
        self._cam_node = self._scene.add(cam)

        # Light
        light = pyrender.DirectionalLight(color=[255, 255, 255], intensity=6.0)
        self._light_node = self._scene.add(light)

        # Pre-compute view angles
        self._theta_angles = const.THETA_ANGLES[const.GRID_VIEW_INDICES].astype("float32")
        self._phi_angles = const.PHI_ANGLES[const.GRID_VIEW_INDICES].astype("float32")

        # Create offscreen renderer with SSAA
        render_width = const.IMG_WIDTH * self._ssaa_factor
        render_height = const.IMG_HEIGHT * self._ssaa_factor
        self._renderer = pyrender.OffscreenRenderer(render_width, render_height)

        init_time = time.perf_counter() - init_start
        logger.info(f"GLBRenderer initialized in {init_time:.3f}s ({render_width}x{render_height}, {self._ssaa_factor}x SSAA)")



    def render_grid(self, glb_bytes: bytes) -> bytes:
        """Load mesh, add to scene, render views, remove mesh, return PNG."""
        # Lazy init: OpenGL contexts are thread-local, so we must initialize
        # in the same thread that will use the renderer (the executor thread)
        if self._renderer is None:
            self.init()

        logger.info(f"Starting GLB rendering, payload size: {len(glb_bytes)} bytes")

        overall_start = time.perf_counter()

        # Load mesh
        load_start = time.perf_counter()
        mesh = trimesh.load(
            file_obj=io.BytesIO(glb_bytes),
            file_type='glb',
            force='mesh'
        )
        load_time = time.perf_counter() - load_start
        logger.info(f"Mesh loaded in {load_time:.3f}s: {mesh}")

        self._assert_model_size(mesh)

        # Convert to pyrender mesh and disable mipmaps
        pyr_mesh = pyrender.Mesh.from_trimesh(mesh, smooth=True)
        for primitive in pyr_mesh.primitives:
            if primitive.material is not None:
                mat = primitive.material
                for attr in ['baseColorTexture', 'metallicRoughnessTexture', 'normalTexture',
                             'occlusionTexture', 'emissiveTexture']:
                    tex = getattr(mat, attr, None)
                    if tex is not None and hasattr(tex, 'sampler') and tex.sampler is not None:
                        tex.sampler.minFilter = GL_LINEAR
                        tex.sampler.magFilter = GL_LINEAR

        # Add mesh to scene
        mesh_node = self._scene.add(pyr_mesh)
        logger.debug("Mesh added to scene")

        # Render views
        images = []
        view_count = 0
        render_start = time.perf_counter()

        for theta, phi in zip(self._theta_angles, self._phi_angles):
            cam_pos = coords.spherical_to_cartesian(theta, phi, const.CAM_RAD_MESH)
            pose = coords.look_at(cam_pos)

            self._scene.set_pose(self._cam_node, pose)
            light_offset = np.array([1.0, 1.0, 0])
            light_pos = cam_pos + light_offset
            light_pose = coords.look_at(light_pos)
            self._scene.set_pose(self._light_node, light_pose)

            image, _ = self._renderer.render(self._scene)

            # Downsample with high-quality Lanczos filter for antialiasing
            image_pil = Image.fromarray(image).resize(
                (const.IMG_WIDTH, const.IMG_HEIGHT),
                resample=Image.LANCZOS
            )
            images.append(image_pil)
            view_count += 1
            logger.debug(f"Rendered view {view_count}: theta={theta:.2f}, phi={phi:.2f}")

        render_time = time.perf_counter() - render_start
        logger.info(f"All {view_count} views rendered in {render_time:.3f}s ({render_time/view_count:.3f}s per view, {self._ssaa_factor}x SSAA)")

        # Remove mesh from scene
        self._scene.remove_node(mesh_node)
        overall_time = time.perf_counter() - overall_start
        logger.info(f"Mesh processing complete in {overall_time:.3f}s (load + render + cleanup)")

        # Combine into grid and encode as PNG
        grid = img_utils.combine4(images)
        buffer = io.BytesIO()
        grid.save(buffer, format="PNG")
        buffer.seek(0)
        png_bytes = buffer.read()
        logger.info(f"GLB rendering complete, output size: {len(png_bytes)} bytes")
        return png_bytes

    def _assert_model_size(self, mesh: trimesh.Trimesh) -> None:
        """Check if model fits within a unit cube."""
        if mesh.bounds.max() <= 0.6 and mesh.bounds.min() >= -0.6:
            return
        else:
            raise ValueError("Model exceeds unit cube size constraint")