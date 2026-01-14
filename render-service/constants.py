import numpy as np

# View settings
VIEWS_NUMBER = 16
THETA_ANGLES = np.linspace(0, 360, num=VIEWS_NUMBER)
PHI_ANGLES = np.full_like(THETA_ANGLES, -15.0)
GRID_VIEW_INDICES = [1, 5, 9, 13]

# Image settings
IMG_WIDTH = 518
IMG_HEIGHT = 518
GRID_VIEW_GAP = 5
BG_COLOR = [1.0, 1.0, 1.0]

# Camera settings
CAM_RAD = 2.5
CAM_FOV_DEG = 49.1
REF_BBOX_SIZE = 1.5