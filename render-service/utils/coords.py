import math
import numpy as np

def spherical_to_cartesian(theta, phi, radius):
    """
    Convert spherical coords to Cartesian.
    theta: azimuth (radians)
    phi: elevation (radians)
    """
    phi = np.deg2rad(phi)
    theta = np.deg2rad(theta)
    return np.array([
        radius * math.cos(phi) * math.sin(theta),
        - radius * math.sin(phi),
        radius * math.cos(phi) * math.cos(theta),
    ])

def look_at(camera_pos, target=np.zeros(3), up=np.array([0, 1, 0])):
    """
    Create a camera-to-world pose matrix.
    """
    camera_pos = np.asarray(camera_pos)

    forward = target - camera_pos
    forward /= np.linalg.norm(forward)

    right = np.cross(forward, up)
    right /= np.linalg.norm(right)

    up = np.cross(right, forward)

    pose = np.eye(4)
    pose[:3, 0] = right
    pose[:3, 1] = up
    pose[:3, 2] = -forward
    pose[:3, 3] = camera_pos
    return pose