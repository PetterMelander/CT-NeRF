"""Contains various functions for computing and using rays."""

from collections.abc import Callable

import torch

from ctnerf import ray_sampling


def beer_lambert_law(
    attenuation_coeffs: torch.Tensor,
    distances: torch.Tensor,
    s: float | None,
    k: float | None,
    slice_size_cm: float,
) -> torch.Tensor:
    """Use Beer-Lambert law to calculate transmittance of a ray.

    Uses Beer-Lambert law to calculate transmittance given the attenuation coefficients and
    distances of sampled points along ray. If s and k are provided, uses a version of the law that
    has been adjusted for scaling of the transmittance by adding k, taking the log, and dividing by
    s.

    Args:
        attenuation_coeffs (torch.Tensor): shape (B, N)
        distances (torch.Tensor): shape (B, N)
        s (float | None): scaling factor
        k (float | None): offset
        slice_size_cm (float): size of an axial slice in centimetres

    Returns:
        torch.Tensor: shape (B,). Transmittance

    """
    # scale to cm because the CT creation scripts uses attenuation per cm
    distances = distances * slice_size_cm / 2

    exp = torch.exp(-torch.sum(attenuation_coeffs * distances, dim=1))
    if s is not None and k is not None:
        return torch.log(exp + k) / s
    return exp


@torch.no_grad()
def get_rays(
    pixel_pos: torch.Tensor,
    angles: torch.Tensor,
    img_shape: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Get the start position, heading vector, and ray bounds defining each ray.

    Given the positions of pixels in an image and the angles of the xray source, compute the start
    position and heading vector for each ray.

    Args:
        pixel_pos (torch.Tensor): shape (B, 2)
        angles (torch.Tensor): shape (B,)
        img_shape (torch.Tensor): shape (2,)

    Returns:
        torch.Tensor: shape (B, 3). Start position
        torch.Tensor: shape (B, 3). Heading vector
        torch.Tensor: shape (B, 2). Ray bounds

    """
    # get normalized position before accounting for angle
    normalized_pos = 2 * pixel_pos / (img_shape - 1) - 1

    # start position is always at x = 1
    x = torch.ones((pixel_pos.shape[0], 1), device=pixel_pos.device)
    normalized_pos = torch.cat((x, normalized_pos), dim=1)

    # rotate to account for angle
    rotation_matrix, heading_vector = _create_z_rotation_matrix(angles)
    start_pos = torch.bmm(rotation_matrix, normalized_pos.unsqueeze(2)).squeeze(2)
    ray_bounds = _get_ray_bounds(start_pos, heading_vector)

    return start_pos, heading_vector, ray_bounds


@torch.no_grad()
def _get_ray_bounds(start_pos: torch.Tensor, heading_vector: torch.Tensor) -> torch.Tensor:
    """Get the two t values that define the bounds of the ray.

    Given a start position (a, b, c) and a direction vector (v_x, v_y, 0), a ray can be
    parameterized as (a + t*v_x, b + t*v_y, c). This function returns the t's that correspond to the
    ray passing through the cylinder x^2 + y^2 = 1, which defines the bounds of the image.

    Args:
        start_pos (torch.Tensor): shape (B, 3)
        heading_vector (torch.Tensor): shape (B, 3)

    Returns:
        torch.Tensor: A tensor of shape (B, 2), containing the two t's

    """
    a = start_pos[:, 0]
    b = start_pos[:, 1]
    v_x = heading_vector[:, 0]
    v_y = heading_vector[:, 1]
    # p_half = (a*v_x + b*v_y) / (v_x**2 + v_y**2)  # noqa: ERA001
    # q = (a**2 + b**2 - 1) / (v_x**2 + v_y**2)  # noqa: ERA001
    p_half = a * v_x + b * v_y
    q = a**2 + b**2 - 1
    sq_root = torch.sqrt(p_half**2 - q)
    sq_root = torch.nan_to_num(sq_root, nan=0)  # rounding errors can cause imaginary sq_root
    t1 = -p_half - sq_root
    t2 = -p_half + sq_root
    return torch.stack((t1, t2), dim=1)


@torch.no_grad()
def get_coarse_samples(
    start_pos: torch.Tensor,
    heading_vector: torch.Tensor,
    n_samples: int,
    batch_size: int,
    device: torch.device,
    ray_bounds: torch.Tensor | None,
    plateau_ratio: float | None,
    sampling_function: Callable[[int, int, torch.device, dict], torch.Tensor],
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Get the coarse samples along the ray.

    Samples n_samples points along the ray using plateau sampling within the cylinder bounds.

    Args:
        start_pos (torch.Tensor): shape (B, 3). Starting position of the ray.
        heading_vector (torch.Tensor): shape (B, 3). Heading vector of the ray.
        n_samples (int): number of samples.
        batch_size (int): size of the batch being processed.
        device (torch.device): device to place the tensors on.
        ray_bounds (torch.Tensor): shape (B, 2). Ray bounds.
        plateau_ratio (float): ratio of plateau width to standard deviation.
        sampling_function (Callable[[int, int, torch.device, dict], torch.Tensor]): function that
            performs the sampling.

    Returns:
        torch.Tensor: shape (B, n_samples). t values of the sampled points.
        torch.Tensor: shape (B * n_samples, 3). Sampled points.
        torch.Tensor: shape (B, n_samples). Distances between adjacent samples.

    """
    t_samples = sampling_function(
        n_samples,
        batch_size,
        device,
        plateau_ratio=plateau_ratio,
        ray_bounds=ray_bounds,
    )
    sampling_distances = get_sampling_distances(t_samples, ray_bounds)

    # sampled points should have shape (B, n_samples, 3)
    sampled_points = start_pos.unsqueeze(1) + t_samples.unsqueeze(2) * heading_vector.unsqueeze(1)
    sampled_points = sampled_points.reshape(-1, 3)

    return t_samples, sampled_points, sampling_distances


@torch.no_grad()
def get_fine_samples(
    start_pos: torch.Tensor,
    heading_vector: torch.Tensor,
    ray_bounds: torch.Tensor,
    coarse_sample_ts: torch.Tensor,
    coarse_sample_values: torch.Tensor,
    coarse_sampling_distances: torch.Tensor,
    n_samples: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Get the fine samples along the ray.

    Samples n_samples points along the ray using edge focused sampling based on the outputs of the
    coarse model.

    Args:
        start_pos (torch.Tensor): shape (B, 3). Starting position of the ray.
        heading_vector (torch.Tensor): shape (B, 3). Heading vector of the ray.
        ray_bounds (torch.Tensor): shape (B, 2). Ray bounds.
        coarse_sample_ts (torch.Tensor): shape (B, n_samples). Contains the t values for the coarse
            samples.
        coarse_sample_values (torch.Tensor): shape (B, n_samples). Contains the coarse model's
            outputs for the coarse samples.
        coarse_sampling_distances (torch.Tensor): shape (B, n_samples). Contains the sampling
            distances of the coarse samples.
        n_samples (int): number of samples
    Returns:
        torch.Tensor: shape (B, n_samples). Contains the sampled t's
        torch.Tensor: shape (B, n_samples, 3). Contains the distances between adjacent samples.

    """
    t_samples = ray_sampling.edge_focused_fine_sampling(
        n_samples,
        coarse_sample_values,
        coarse_sampling_distances,
    )

    # concatenate the coarse samples with the fine samples and
    # sort them so distance between adjacent samples can be calculated
    t_samples = torch.cat([coarse_sample_ts, t_samples], dim=1)
    t_samples = torch.sort(t_samples, dim=1)[0]
    sampling_distances = get_sampling_distances(t_samples, ray_bounds)

    # sampled points should have shape (B, n_samples, 3)
    sampled_points = start_pos.unsqueeze(1) + t_samples.unsqueeze(2) * heading_vector.unsqueeze(1)
    sampled_points = sampled_points.reshape(-1, 3)

    return sampled_points, sampling_distances


@torch.no_grad()
def _create_z_rotation_matrix(angles: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Get rotation matrix for z-axis rotation and heading vector.

    Create a batch of 3D rotation matrices for rotations around the z-axis.
    Also returns the heading vector.

    Args:
        angles (torch.Tensor): shape (B,). Contains the rotation angles in radians.

    Returns:
        torch.Tensor: A tensor of shape (B, 3, 3) containing the rotation matrices.
        torch.Tensor: A tensor of shape (B, 3) containing the heading vectors.

    """
    cos_angles = torch.cos(angles)
    sin_angles = torch.sin(angles)

    rotation_matrices = torch.zeros((angles.shape[0], 3, 3), device=angles.device)
    rotation_matrices[:, 0, 0] = cos_angles
    rotation_matrices[:, 0, 1] = -sin_angles
    rotation_matrices[:, 1, 0] = sin_angles
    rotation_matrices[:, 1, 1] = cos_angles
    rotation_matrices[:, 2, 2] = 1

    # heading vector should be in the negative x direction since the start position is at x = 1
    heading_vector = -torch.stack(
        (cos_angles, sin_angles, torch.zeros(angles.shape[0], device=angles.device)),
        dim=1,
    )

    return rotation_matrices, heading_vector


@torch.no_grad()
def get_sampling_distances(
    t_samples: torch.Tensor,
    ray_bounds: torch.Tensor | None,
) -> torch.Tensor:
    """Get distances between adjacent sampled points along the ray.

    Gets the distance between adjacent sampled points along the ray. The distance associated with
    each sample is the distance between that sample and the next sample. The distance for the last
    sample is the distance between the last sample and the far bound of the image.

    This distance can be calculated directly from t values since the heading vector has magnitue 1.

    Args:
        t_samples (torch.Tensor): shape (B, n_samples). Contains the t values for the samples.
        ray_bounds (torch.Tensor): shape (B, 2). Ray bounds.

    Returns:
        torch.Tensor: shape (B, n_samples). Distances between adjacent samples.

    """
    # Append upper ray bound to end of each batch so last sample will have a distance.
    if ray_bounds is not None:
        ray_limit = torch.ones([t_samples.shape[0], 1], device=t_samples.device) * ray_bounds[:, 1:]
    else:
        ray_limit = torch.ones([t_samples.shape[0], 1], device=t_samples.device) * 2
    return torch.diff(t_samples, dim=1, append=ray_limit)
