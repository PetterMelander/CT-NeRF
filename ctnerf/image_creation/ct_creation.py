"""Functions for generating CT images from a trained model."""

import numpy as np
import SimpleITK as sitk
import torch
from tqdm import tqdm

from ctnerf.constants import MU_AIR, MU_WATER
from ctnerf.model import XRayModel
from ctnerf.setup.config import InferenceConfig


@torch.no_grad()
def generate_ct(conf: InferenceConfig) -> None:
    """Generate a CT image from a trained model.

    Args:
        conf (InferenceConfig): The inference configuration.

    """
    # Define image size and spacing, giving x the same value as y
    original_size = [conf.xray_metadata["size"][0]] + conf.xray_metadata["size"]
    original_spacing = [conf.xray_metadata["spacing"][0]] + conf.xray_metadata["spacing"]

    # Determine image size and spacing
    if conf.voxel_spacing is not None:
        original_spacing = np.array(original_spacing)
        original_size = np.array(original_size)
        new_spacing = np.array(conf.voxel_spacing)
        image_size = (original_size / original_spacing * new_spacing).astype(int)
        voxel_spacing = conf.voxel_spacing
    elif conf.image_size is not None:
        original_spacing = np.array(original_spacing)
        original_size = np.array(original_size)
        new_size = np.array(conf.image_size)
        voxel_spacing = original_spacing * original_size / new_size
        image_size = conf.image_size

    output = run_inference(
        conf.fine_model or conf.coarse_model,
        image_size,
        conf.chunk_size,
        conf.attenuation_scaling_factor,
        conf.device,
    )
    ct_image = tensor_to_sitk(
        output,
        conf.xray_metadata,
        conf.image_direction,
        conf.image_origin,
        voxel_spacing,
    )

    sitk.WriteImage(ct_image, conf.output_path)


@torch.no_grad()
def run_inference(
    model: XRayModel,
    img_size: tuple[int, int, int],
    chunk_size: int,
    attenuation_scaling_factor: float | None,
    device: torch.device,
) -> torch.Tensor:
    """Run inference on the model.

    Args:
        model (XRayModel): The model to run inference on.
        img_size (tuple[int, int, int]): The size of the output image.
        chunk_size (int): Number of coordinate points to process in each batch to avoid OOM errors.
        attenuation_scaling_factor (float | None): Scaling factor for attenuation values.
        device (torch.device): The device to run the model inference on.

    Returns:
        torch.Tensor: The output image.

    """
    model.eval()

    # Generate coordinates
    x = torch.linspace(-1, 1, img_size[0])
    y = torch.linspace(-1, 1, img_size[1])
    z = torch.linspace(-1, 1, img_size[2])
    coords = torch.stack(torch.meshgrid((x, y, z), indexing="xy"), dim=-1)
    coords = coords.view(-1, 3)

    # To avoid oom, inference is done in batches and result stored on cpu
    output = torch.empty(len(coords), device=torch.device("cpu"))
    coords = coords.split(chunk_size, dim=0)
    for i, chunk in enumerate(tqdm(coords, desc="Generating", total=len(coords), leave=False)):
        chunk = chunk.to(device)
        output_chunk = model(chunk)
        output_chunk = output_chunk.view(-1)
        output[i * chunk_size : (i + 1) * chunk_size] = output_chunk.cpu()

    # Convert to hounsfield
    if attenuation_scaling_factor is not None:
        output = output * attenuation_scaling_factor
    output = 1000 * (output - MU_WATER) / (MU_WATER - MU_AIR)
    output = output.clamp_min(-1024)

    # Reshape and permute to get correct orientation
    output = output.reshape(img_size[0], img_size[1], img_size[2])
    output = torch.permute(output, (2, 0, 1))

    model.train()
    return output


def tensor_to_sitk(
    image_tensor: torch.Tensor,
    metadata: dict | None = None,
    direction: tuple[float] | None = None,
    origin: tuple[float, float, float] | None = None,
    spacing: tuple[float, float, float] | None = None,
) -> sitk.Image:
    """Convert a tensor to a sitk image.

    Args:
        image_tensor (torch.Tensor): The tensor to convert to a sitk image.
        metadata (dict, optional): Metadata to add to the image. Defaults to None.
        direction (tuple[float], optional): Direction of the image. Defaults to None.
        origin (tuple[float, float, float], optional): Origin of the image. Defaults to None.
        spacing (tuple[float, float, float], optional): Spacing of the image. Defaults to None.

    Returns:
        sitk.Image: The sitk image.

    """
    ct_array = image_tensor.numpy().astype(np.int16)
    ct_image = sitk.GetImageFromArray(ct_array)

    if direction is not None:
        ct_image.SetDirection(direction)
    if origin is not None:
        ct_image.SetOrigin(origin)
    if spacing is not None:
        ct_image.SetSpacing(spacing)

    if metadata is not None and "ct_meta" in metadata:
        for key, value in metadata["ct_meta"].items():
            ct_image.SetMetaData(key, value)

    return ct_image
