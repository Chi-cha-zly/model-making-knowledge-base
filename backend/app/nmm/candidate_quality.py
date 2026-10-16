"""Local edge heuristic, not a geometry classifier or a model quality guarantee."""

import numpy as np
from PIL import Image
from scipy.ndimage import binary_erosion, distance_transform_edt, label
from skimage.feature import canny

from .analysis_scope import validate_mask

QUALITY_VERSION = 'nmm-structure-1'


def unavailable_check():
    return {
        'status': 'blocked',
        'reasons': ['无法验证候选图与原图/遮罩的一致性，请检查图片后重新生成。'],
        'metrics': {},
        'version': QUALITY_VERSION,
    }


def check_candidate(original, candidate, mask):
    validate_mask(mask)
    if any(
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.shape != (*mask.shape, 3)
        for image in (original, candidate)
    ):
        raise ValueError('Expected matching RGB uint8 images and bool mask')
    height, width = mask.shape
    scale = min(1, 768 / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    region = np.array(Image.fromarray(mask).resize(size, Image.Resampling.NEAREST))
    interior = binary_erosion(region, iterations=2, border_value=0)

    def edges(image):
        if not interior.any():
            return np.zeros(region.shape, dtype=bool)
        rgb = (
            np.asarray(
                Image.fromarray(image).resize(size, Image.Resampling.LANCZOS), dtype=np.float64
            )
            / 255
        )
        # Channel-wise edges tolerate recoloring, hue changes and inverted light.
        edge = np.zeros(region.shape, dtype=bool)
        for channel in range(3):
            value = rgb[:, :, channel]
            low, high = np.percentile(value[region], (2, 98))
            if high - low >= 0.04:
                normalized = np.clip((value - low) / (high - low), 0, 1)
                edge |= canny(normalized, sigma=1.2, low_threshold=0.1, high_threshold=0.25)
        return edge & interior

    source_edges, candidate_edges = edges(original), edges(candidate)
    count = int(source_edges.sum())
    if candidate_edges.any():
        distances = distance_transform_edt(~candidate_edges)[source_edges]
    else:
        distances = np.full(count, max(size), dtype=float)
    retained = float(np.mean(distances <= 2.5)) if count else 0.0
    displacement = float(np.median(distances)) if count else 0.0
    components, _ = label(source_edges, structure=np.ones((3, 3)))
    identities = components[source_edges]
    sizes = np.bincount(identities)
    missing = np.bincount(identities, weights=distances > 2.5, minlength=len(sizes))
    erased = int(np.sum((sizes >= 40) & (missing / np.maximum(sizes, 1) > 0.85)))
    blocked = (count >= 30 and retained < 0.45 and displacement > 2.5) or erased > 0
    reasons = (
        [
            '本地边缘检查发现大量原有轮廓消失或移位，可能存在结构改动，已阻止采用；该启发式也可能误判涂装明暗变化。'
        ]
        if blocked
        else ['本地启发式不能证明结构保真，请逐项对照原图检查零件、轮廓和浮雕后再确认。']
    )
    if count < 30:
        reasons.append('遮罩内可辨边缘不足，自动检查无法可靠判断结构。')
    return {
        'status': 'blocked' if blocked else 'needs_review',
        'reasons': reasons,
        'metrics': {
            'source_edge_pixels': count,
            'edge_retention': retained,
            'median_edge_displacement': displacement,
            'analysis_scale': scale,
            'lost_edge_components': erased,
        },
        'version': QUALITY_VERSION,
    }
