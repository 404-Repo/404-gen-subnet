import io
from loguru import logger
import numpy as np
from PIL import Image
from OpenGL.GL import GL_LINEAR
import pyrender
import trimesh
import torch

import constants as const
from renderers.gs_renderer.renderer import Renderer
from renderers.ply_loader import PlyLoader
from utils import coords
from utils import image as img_utils

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



def grid_from_glb_bytes(glb_bytes: bytes):
    logger.info(f"Starting GLB rendering, payload size: {len(glb_bytes)} bytes")
    
    logger.debug("Loading mesh with trimesh")
    mesh = trimesh.load(
        file_obj=io.BytesIO(glb_bytes),
        file_type='glb',
        force='mesh'
    )
    logger.debug(f"Mesh loaded: {mesh}")
    
    logger.debug("Creating pyrender scene")
    scene = pyrender.Scene(bg_color=[255, 255, 255, 0], ambient_light=[0.3, 0.3, 0.3])

    # Convert to pyrender mesh
    logger.debug("Converting trimesh to pyrender mesh")
    pyr_mesh = pyrender.Mesh.from_trimesh(mesh, smooth=True)

    # Disable mipmaps on all textures
    for primitive in pyr_mesh.primitives:
        if primitive.material is not None:
            mat = primitive.material
            for attr in ['baseColorTexture', 'metallicRoughnessTexture', 'normalTexture',
                         'occlusionTexture', 'emissiveTexture']:
                tex = getattr(mat, attr, None)
                if tex is not None and hasattr(tex, 'sampler') and tex.sampler is not None:
                    tex.sampler.minFilter = GL_LINEAR
                    tex.sampler.magFilter = GL_LINEAR
    logger.debug("Mipmaps disabled on mesh textures")

    scene.add(pyr_mesh)

    # Camera
    cam = pyrender.PerspectiveCamera(yfov=const.CAM_FOV_DEG*np.pi/180.0)
    cam_node = scene.add(cam)
    logger.debug("Camera added to scene")

    # Light
    light = pyrender.DirectionalLight(color=[255,255,255], intensity=6.0)
    light_node = scene.add(light)
    logger.debug("Light added to scene")

    theta_angles = const.THETA_ANGLES[const.GRID_VIEW_INDICES].astype("float32")
    phi_angles = const.PHI_ANGLES[const.GRID_VIEW_INDICES].astype("float32")
    logger.debug(f"Rendering {len(theta_angles)} theta angles x {len(phi_angles)} phi angles")

    # Render with 2x supersampling for antialiasing
    ssaa_factor = 2
    render_width = const.IMG_WIDTH * ssaa_factor
    render_height = const.IMG_HEIGHT * ssaa_factor
    logger.debug(f"Initializing OffscreenRenderer ({render_width}x{render_height}) with {ssaa_factor}x SSAA")
    renderer = pyrender.OffscreenRenderer(render_width, render_height)
    logger.info("OffscreenRenderer initialized successfully")

    images = []
    view_count = 0

    for theta, phi in zip(theta_angles, phi_angles):
        cam_pos = coords.spherical_to_cartesian(theta, phi, const.CAM_RAD_MESH)
        pose = coords.look_at(cam_pos)

        scene.set_pose(cam_node, pose)
        light_offset = np.array([1.0, 1.0, 0]) 
        light_pos = cam_pos + light_offset
        light_pose = coords.look_at(light_pos)
        scene.set_pose(light_node, light_pose)

        image, _ = renderer.render(scene)
        
        # Downsample with high-quality Lanczos filter for antialiasing
        image_pil = Image.fromarray(image).resize(
            (const.IMG_WIDTH, const.IMG_HEIGHT),
            resample=Image.LANCZOS
        )
        images.append(image_pil)
        view_count += 1
        logger.debug(f"Rendered view {view_count}: theta={theta:.2f}, phi={phi:.2f}")
    
    logger.info(f"All {view_count} views rendered with {ssaa_factor}x SSAA, combining into grid")
    grid = img_utils.combine4(images)
    buffer = io.BytesIO()
    grid.save(buffer, format="PNG")
    buffer.seek(0)
    png_bytes = buffer.read()
    logger.info(f"GLB rendering complete, output size: {len(png_bytes)} bytes")
    return png_bytes