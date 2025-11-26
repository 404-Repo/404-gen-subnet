import copy
from typing import Any

import numpy as np
import open3d as o3d
import torch

from pydantic import BaseModel, ConfigDict


class GaussianSplattingData(BaseModel):
    points: torch.Tensor  # gaussian centroids
    normals: torch.Tensor  # normals if provided
    features_dc: torch.Tensor  # colour information stored as RGB values
    features_rest: torch.Tensor  # optional attribute field, not being in use
    opacities: torch.Tensor  # opacity value for every gaussian splat presented
    scales: torch.Tensor  # scale values per gaussian splat
    rotations: torch.Tensor  # rotation quaternion for every gaussian splat
    sh_degree: torch.Tensor  # degree of the spherical harmonics

    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow PyTorch tensors

    def send_to_device(self, device: torch.device) -> "GaussianSplattingData":
        """Moves all tensors in the instance to the specified device."""
        return GaussianSplattingData(
            points=self.points.to(device),
            normals=self.normals.to(device),
            features_dc=self.features_dc.to(device),
            features_rest=self.features_rest.to(device),
            opacities=self.opacities.to(device),
            scales=self.scales.to(device),
            rotations=self.rotations.to(device),
            sh_degree=self.sh_degree.to(device),
        )


def sigmoid(x: torch.Tensor, slope: float = 1.0, x_shift: float = 0.0) -> Any:
    """Function for remapping input data using sigmoid function"""

    return 1.0 / (1.0 + torch.exp(-slope * (x - x_shift)))


def recenter_gs_points(points: np.ndarray) -> Any:
    """Function for recentering Gaussian Splatting centroids"""

    recentered_points = (points - points.mean()).astype(np.float32)
    return recentered_points


def transform_gs_data(gs_data: GaussianSplattingData, ref_bbox_size: float = 1.5) -> GaussianSplattingData:
    """Function for rescaling the model to the fixed bbox size"""

    gs_data_out = copy.deepcopy(gs_data)

    points = gs_data.points.detach().cpu().numpy()
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points.astype(np.float64))

    bbox = o3d.geometry.AxisAlignedBoundingBox()
    bbox = bbox.create_from_points(pcd.points)
    extent = np.array(bbox.get_extent())

    max_size = np.max(extent)
    scaling = ref_bbox_size / max_size

    centered_points = recenter_gs_points(points)
    gs_data_out.points = torch.tensor(centered_points, dtype=torch.float32) * scaling
    gs_data_out.scales *= scaling
    volume_scale = np.prod(scaling)
    gs_data_out.opacities = torch.clip(gs_data.opacities * (1.0 / volume_scale), 0.0, 1.0).to(torch.float32)

    return gs_data_out
