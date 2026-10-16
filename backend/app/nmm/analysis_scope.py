"""Bounded mask geometry and grounded analysis; no model-inferred scope."""

from copy import deepcopy
from io import BytesIO
import math

import numpy as np
from PIL import Image

from .providers import ImageInput
from .workflow_schemas import AnalysisOutput

MAX_PIXELS = 4_000_000
MIN_OVERLAP = 0.5
MIN_CONFIDENCE = 0.7


def validate_mask(mask):
    if (
        not isinstance(mask, np.ndarray)
        or mask.dtype != np.bool_
        or mask.ndim != 2
        or not 0 < mask.size <= MAX_PIXELS
        or not mask.any()
    ):
        raise ValueError('Expected nonempty binary mask with at most 4 MP')


def mask_scope(mask):
    validate_mask(mask)
    y, x = np.nonzero(mask)
    height, width = mask.shape
    return {
        'bbox': [
            float(x.min() / width),
            float(y.min() / height),
            float((x.max() + 1) / width),
            float((y.max() + 1) / height),
        ],
        'coverage': float(mask.mean()),
        'width': width,
        'height': height,
        'coordinates': 'normalized original image, x0/y0/x1/y1, exclusive right/bottom',
    }


def scope_analysis(analysis, mask):
    """Re-evaluate even legacy/checkpoint analysis against the actual current mask."""
    validate_mask(mask)
    parsed = AnalysisOutput.model_validate(analysis).model_dump()
    kept = []
    height, width = mask.shape
    for surface in parsed['surfaces']:
        bbox, confidence = surface['bbox'], surface['confidence']
        if bbox is None or confidence is None or confidence < MIN_CONFIDENCE:
            continue
        x0, y0, x1, y1 = bbox
        left, top = math.floor(x0 * width), math.floor(y0 * height)
        right, bottom = math.ceil(x1 * width), math.ceil(y1 * height)
        if float(mask[top:bottom, left:right].mean()) >= MIN_OVERLAP:
            kept.append(surface)
    output = deepcopy(parsed)
    output['surfaces'] = kept
    dropped = len(parsed['surfaces']) - len(kept)
    if dropped:
        output['warnings'] = (
            output['warnings']
            + [f'{dropped} 个表面缺少可靠定位、置信度不足或不在实际遮罩内，未用于生成。']
        )[-30:]
    return output


def generation_context(analysis):
    """Only locally screened per-surface data, never free global model prose."""
    return {
        'surfaces': [
            {
                key: surface.get(key)
                for key in ('name', 'shape', 'bbox', 'confidence', 'reflection_hint')
            }
            for surface in analysis.get('surfaces', [])
        ]
    }


def analysis_images(original, region):
    """Original + explicit mask + a context crop in a single vision request."""
    with Image.open(BytesIO(original.data)) as source, Image.open(BytesIO(region.data)) as decoded:
        if source.size != decoded.size or source.width * source.height > MAX_PIXELS:
            raise ValueError('Analysis input dimensions do not match')
        mask = np.array(decoded.convert('L')) > 0
        scope = mask_scope(mask)
        width, height = source.size
        x0, y0, x1, y1 = scope['bbox']
        pad = max(2, round(max((x1 - x0) * width, (y1 - y0) * height) * 0.1))
        box = (
            max(0, math.floor(x0 * width) - pad),
            max(0, math.floor(y0 * height) - pad),
            min(width, math.ceil(x1 * width) + pad),
            min(height, math.ceil(y1 * height) + pad),
        )
        rgb = source.convert('RGB').crop(box)
        alpha = decoded.convert('L').crop(box).point(lambda value: 80 if value else 0)
        annotated = Image.composite(Image.new('RGB', rgb.size, 'red'), rgb, alpha)
        annotated.thumbnail((768, 768), Image.Resampling.LANCZOS)
        stream = BytesIO()
        annotated.save(stream, 'PNG')
        scope['context_crop_original_pixels'] = list(box)
        scope['context_crop_size'] = list(annotated.size)
    return [original, region, ImageInput(stream.getvalue(), 'image/png')], scope, mask
