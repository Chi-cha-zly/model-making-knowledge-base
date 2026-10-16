"""Descriptive, masked intermediate metrics; no I/O or learned quality score.

Every frame is assigned to the same final-image Lab palette with Euclidean
distance, matching the renderer's assignment rule. Pixel error is CIEDE2000.
"""

import numpy as np
from skimage.color import deltaE_ciede2000, rgb2lab

CHUNK_SIZE = 16384
MAX_PIXELS = 4_000_000


def match_intermediates(reference_count, generated_count):
    """Match first=0 through final=1 progress, excluding BOTH final frames.

    Each reference intermediate has one nearest generated intermediate; ties use
    the earlier generated frame. Repeated matches are intentional and reported.
    This is a temporal proxy, not semantic alignment of painting operations.
    """
    for count, maximum in ((reference_count, 30), (generated_count, 9)):
        if type(count) is not int or not 2 <= count <= maximum:
            raise ValueError(f'Step count must be an integer between 2 and {maximum}.')
    pairs = []
    for real in range(reference_count - 1):
        # Integer cross-products avoid unstable floating-point ties.
        generated = min(
            range(generated_count - 1),
            key=lambda index: abs(real * (generated_count - 1) - index * (reference_count - 1)),
        )
        real_progress = real / (reference_count - 1)
        generated_progress = generated / (generated_count - 1)
        pairs.append(
            {
                'reference_step': real + 1,
                'generated_step': generated + 1,
                'reference_progress': real_progress,
                'generated_progress': generated_progress,
                'progress_distance': abs(real_progress - generated_progress),
            }
        )
    return pairs


def compare_frames(reference, generated, mask, palette_lab):
    """Mean masked ΔE2000 and signed generated-minus-reference populations.

    Area ratios sum to one independently for each image; they are palette-bin
    populations, not base paint masks or the renderer's adjacent-step diff areas.
    Workspaces are chunked; outside-mask pixels never affect either metric.
    """
    for image in (reference, generated):
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.ndim != 3
            or image.shape[2] != 3
            or min(image.shape[:2]) < 1
            or image.shape[0] * image.shape[1] > MAX_PIXELS
        ):
            raise ValueError('Frames must be nonempty RGB uint8 arrays up to 4MP.')
    if (
        reference.shape != generated.shape
        or not isinstance(mask, np.ndarray)
        or mask.dtype != bool
        or mask.shape != reference.shape[:2]
        or not mask.any()
    ):
        raise ValueError('Frames and nonempty boolean mask must have identical dimensions.')
    centers = np.asarray(palette_lab, dtype=np.float64)
    if (
        centers.ndim != 2
        or not 1 <= len(centers) <= 7
        or centers.shape[1] != 3
        or not np.isfinite(centers).all()
    ):
        raise ValueError('Shared final palette must contain 1–7 finite Lab colors.')
    positions = np.flatnonzero(mask)
    populations = np.zeros((2, len(centers)), dtype=np.int64)
    total = 0.0
    frames = [reference.reshape(-1, 3), generated.reshape(-1, 3)]
    for start in range(0, len(positions), CHUNK_SIZE):
        selected = positions[start : start + CHUNK_SIZE]
        labs = [rgb2lab(image[selected].astype(np.float64) / 255) for image in frames]
        total += float(deltaE_ciede2000(labs[0], labs[1]).sum(dtype=np.float64))
        for side, lab in enumerate(labs):
            distances = np.sum((lab[:, None, :] - centers[None, :, :]) ** 2, axis=2)
            populations[side] += np.bincount(distances.argmin(axis=1), minlength=len(centers))
    ratios = populations / len(positions)
    differences = ratios[1] - ratios[0]
    return {
        'mean_delta_e2000': total / len(positions),
        'reference_area_ratios': ratios[0].tolist(),
        'generated_area_ratios': ratios[1].tolist(),
        'area_ratio_signed_difference': differences.tolist(),
        'area_ratio_absolute_difference': np.abs(differences).tolist(),
        'mean_absolute_area_ratio_difference': float(np.abs(differences).mean()),
    }
