"""Bounded, deterministic RGB step rendering; no database, providers or files.

Palette extraction uses Lab Euclidean k-means. Change masks use CIEDE2000.
Only base paint masks are nested; transition/final changes need not be monotone.
"""

from dataclasses import dataclass
from math import isfinite
from numbers import Real

import cv2
import numpy as np
from PIL import Image, ImageDraw
from skimage.color import deltaE_ciede2000, lab2rgb, rgb2lab

RENDERER_VERSION = 'nmm-lab-layers-1'
MAX_PIXELS = 4_000_000
_CHUNK = 16384


def _integer(name, value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{name} must be an integer')
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')


@dataclass(frozen=True)
class RenderParams:
    detail_strength: float = 0.6
    detail_sigma: float = 1.2
    erosion_radius: int = 1
    transition_width: int = 2
    diff_threshold: float = 2.0
    seed: int = 42
    max_samples: int = 32768

    def __post_init__(self):
        for name, maximum in (
            ('detail_strength', 2),
            ('detail_sigma', 20),
            ('diff_threshold', 100),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f'{name} must be a finite number')
            if not isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f'{name} must be finite and between 0 and {maximum}')
        _integer('erosion_radius', self.erosion_radius, 0, 16)
        _integer('transition_width', self.transition_width, 0, 16)
        _integer('seed', self.seed, 0, 2**32 - 1)
        _integer('max_samples', self.max_samples, 8, 131072)


@dataclass(frozen=True)
class PaletteColor:
    hex: str
    lab: tuple[float, float, float]
    area_ratio: float


@dataclass(frozen=True)
class StepRender:
    image: np.ndarray
    diff_mask: np.ndarray
    paint_mask: np.ndarray
    palette_index: int | None
    area_ratio: float
    kind: str


@dataclass(frozen=True)
class RenderResult:
    steps: list[StepRender]
    palette: list[PaletteColor]
    warnings: list[str]


def _rgb(name, image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        raise TypeError(f'{name} must be an RGB uint8 ndarray')
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 1:
        raise ValueError(f'{name} must have nonempty H x W x 3 shape')
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise ValueError(f'{name} exceeds the {MAX_PIXELS:,} pixel limit')


def _lab(rgb):
    # Float32 is retained by scikit-image; avoid full-image float64 workspaces.
    return rgb2lab(rgb.astype(np.float32) / np.float32(255))


def _rgb_from_lab(lab):
    rgb = lab2rgb(lab.reshape(-1, 1, 3)).reshape(-1, 3)
    return np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8)


def _sample(points, mask, params, warnings):
    if len(points) <= params.max_samples:
        return points
    rng = np.random.default_rng(params.seed)
    radius = params.erosion_radius
    if radius:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        interior = cv2.erode(
            mask.astype(np.uint8), kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0
        )
        pool = np.flatnonzero(interior[mask])
    else:
        pool = np.arange(len(points))
    if not len(pool):
        warnings.append('Sampling erosion removed the region; sampling the complete mask instead.')
        pool = np.arange(len(points))
    # Always retain full-mask extrema, even a single bright boundary pixel.
    anchors = np.unique(np.concatenate((points.argmin(axis=0), points.argmax(axis=0))))
    budget = params.max_samples - len(anchors)
    full_count = min(len(points), budget // 4)
    full = rng.choice(len(points), size=full_count, replace=False)
    remaining = budget - full_count
    core = rng.choice(pool, size=min(len(pool), remaining), replace=False)
    indices = np.unique(np.concatenate((anchors, full, core)))
    return points[indices]


def _assign(points, centers):
    labels = np.empty(len(points), dtype=np.int16)
    for start in range(0, len(points), _CHUNK):
        chunk = points[start : start + _CHUNK]
        distances = np.empty((len(chunk), len(centers)), dtype=np.float32)
        for index, center in enumerate(centers):
            difference = chunk - center
            distances[:, index] = np.einsum('ij,ij->i', difference, difference)
        labels[start : start + len(chunk)] = distances.argmin(axis=1)
    return labels


def _centers(sample, requested, warnings):
    unique = np.unique(sample, axis=0)
    count = min(requested, len(unique))
    if count < requested:
        warnings.append(f'Only {count} effective colors; reduced palette from {requested} colors.')
    if len(unique) <= requested:
        centers = unique.copy()
    else:
        # Local farthest-point initialization with explicit L endpoints keeps a
        # tiny extreme highlight from being missed by probabilistic seeding.
        chosen = [int(unique[:, 0].argmin()), int(unique[:, 0].argmax())]
        chosen = list(dict.fromkeys(chosen))
        while len(chosen) < count:
            distances = np.full(len(unique), np.inf, dtype=np.float32)
            for index in chosen:
                difference = unique - unique[index]
                distances = np.minimum(distances, np.einsum('ij,ij->i', difference, difference))
            distances[chosen] = -1
            chosen.append(int(distances.argmax()))
        centers = unique[chosen].copy()
        for _ in range(32):
            labels = _assign(sample, centers)
            updated = [
                sample[labels == index].mean(axis=0)
                for index in range(len(centers))
                if np.any(labels == index)
            ]
            if len(updated) < len(centers):
                warnings.append('Empty color cluster removed; actual steps were reduced.')
            updated = np.asarray(updated, dtype=np.float32)
            converged = updated.shape == centers.shape and np.max(np.abs(updated - centers)) < 1e-3
            centers = updated
            if converged:
                break
    return centers[np.argsort(centers[:, 0], kind='stable')]


def _diff(previous, image, indices, shape, threshold):
    result = np.zeros(shape[0] * shape[1], dtype=bool)
    old, new = previous.reshape(-1, 3), image.reshape(-1, 3)
    for start in range(0, len(indices), _CHUNK):
        positions = indices[start : start + _CHUNK]
        differences = deltaE_ciede2000(_lab(old[positions]), _lab(new[positions]))
        result[positions] = differences > threshold
    return result.reshape(shape)


def _transition(previous, labels, mask, width):
    image = previous.copy()
    painted = np.zeros(mask.shape, dtype=bool)
    if not width:
        return image, painted
    # Seeds already cover the two touching edge pixels; width is the number
    # of operated pixels on each side, not an extra radius beyond those seeds.
    kernel = np.ones((2 * width - 1, 2 * width - 1), dtype=np.uint8)
    for lower in range(int(labels.max())):
        # Only touching consecutive palette levels produce a transition band.
        pair = mask & ((labels == lower) | (labels == lower + 1))
        seeds = np.zeros(mask.shape, dtype=np.uint8)
        for first, second, target_first, target_second in (
            (labels[:, :-1], labels[:, 1:], seeds[:, :-1], seeds[:, 1:]),
            (labels[:-1, :], labels[1:, :], seeds[:-1, :], seeds[1:, :]),
        ):
            touching = ((first == lower) & (second == lower + 1)) | (
                (first == lower + 1) & (second == lower)
            )
            target_first[touching] = 1
            target_second[touching] = 1
        band = cv2.dilate(seeds, kernel).astype(bool) & pair
        if not band.any():
            continue
        # Normalized convolution excludes background and all other color levels.
        weights = pair.astype(np.float32)
        denominator = cv2.boxFilter(
            weights,
            -1,
            (2 * width + 1, 2 * width + 1),
            normalize=False,
            borderType=cv2.BORDER_CONSTANT,
        )
        for channel in range(3):
            numerator = cv2.boxFilter(
                previous[:, :, channel].astype(np.float32) * weights,
                -1,
                (2 * width + 1, 2 * width + 1),
                normalize=False,
                borderType=cv2.BORDER_CONSTANT,
            )
            image[:, :, channel][band] = (
                np.rint(numerator[band] / denominator[band]).clip(0, 255).astype(np.uint8)
            )
        painted |= band
    return image, painted


def render_steps(original, final, mask, step_count=7, params=None):
    """Render K base layers, an adjacent-band transition, and an exact final.

    Empty masks are errors. Reduced effective K yields fewer steps and warnings.
    Final resizing uses OpenCV linear interpolation (area when both axes shrink).
    Sigma zero disables the detail high-pass; width zero is a no-op transition.
    """
    _rgb('original', original)
    _rgb('final', final)
    _integer('step_count', step_count, 4, 9)
    if not isinstance(mask, np.ndarray) or mask.dtype != bool:
        raise TypeError('mask must be a bool ndarray')
    if mask.ndim != 2 or mask.shape != original.shape[:2]:
        raise ValueError('mask must match the original image dimensions')
    if not mask.any():
        raise ValueError('mask must contain at least one editable pixel')
    if params is None:
        params = RenderParams()
    if not isinstance(params, RenderParams):
        raise TypeError('params must be RenderParams')
    params.__post_init__()
    height, width = mask.shape
    if final.shape != original.shape:
        shrinking = final.shape[0] >= height and final.shape[1] >= width
        final = cv2.resize(
            final, (width, height), interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
        )
    warnings = []
    final_lab = _lab(final)
    points = final_lab[mask]
    del final_lab
    centers = _centers(_sample(points, mask, params, warnings), step_count - 2, warnings)
    assigned = _assign(points, centers)
    del points
    # Full-mask populations, never sample frequencies, define palette areas.
    populations = np.bincount(assigned, minlength=len(centers))
    if np.any(populations == 0):
        warnings.append('Unassigned palette colors removed; actual steps were reduced.')
        centers = centers[populations > 0]
        assigned = _assign(_lab(final)[mask], centers)
        populations = np.bincount(assigned, minlength=len(centers))
    palette_rgb = _rgb_from_lab(centers)
    palette = [
        PaletteColor(
            '#' + ''.join(f'{int(c):02x}' for c in rgb),
            tuple(float(x) for x in center),
            float(population / mask.sum()),
        )
        for center, rgb, population in zip(centers, palette_rgb, populations)
    ]
    labels = np.full(mask.shape, -1, dtype=np.int16)
    labels[mask] = assigned
    indices = np.flatnonzero(mask)
    del assigned
    if params.detail_strength and params.detail_sigma:
        light = _lab(original)[:, :, 0].copy()
        blurred = cv2.GaussianBlur(
            light, (0, 0), params.detail_sigma, borderType=cv2.BORDER_REFLECT_101
        )
        detail = (light - blurred) * params.detail_strength
        del light, blurred
    else:
        detail = np.zeros(mask.shape, dtype=np.float32)
    steps = []
    previous = original

    def append(image, painted, index, kind):
        differences = _diff(previous, image, indices, mask.shape, params.diff_threshold)
        steps.append(
            StepRender(
                image, differences, painted, index, float(differences.sum() / len(indices)), kind
            )
        )

    for index, center in enumerate(centers):
        painted = mask & (labels >= index)
        image = previous.copy()
        positions = np.flatnonzero(painted)
        # Each overwritten pixel gets center + original detail exactly once.
        for start in range(0, len(positions), _CHUNK):
            selected = positions[start : start + _CHUNK]
            values = np.broadcast_to(center, (len(selected), 3)).copy()
            values[:, 0] = np.clip(values[:, 0] + detail.reshape(-1)[selected], 0, 100)
            image.reshape(-1, 3)[selected] = _rgb_from_lab(values)
        append(image, painted, index, 'base')
        previous = image
    del detail
    image, painted = _transition(previous, labels, mask, params.transition_width)
    append(image, painted, None, 'transition')
    previous = image
    image = original.copy()
    image[mask] = final[mask]
    append(image, mask.copy(), None, 'final')
    return RenderResult(steps, palette, warnings)


def make_contact_sheet(steps, *, columns=3, cell_size=256):
    """White, aspect-preserving cells with an unobstructed numbered header."""
    _integer('columns', columns, 1, 9)
    _integer('cell_size', cell_size, 32, 1024)
    if not steps or len(steps) > 9:
        raise ValueError('contact sheet requires between 1 and 9 steps')
    sheet = Image.new(
        'RGB', (columns * cell_size, ((len(steps) + columns - 1) // columns) * cell_size), 'white'
    )
    draw = ImageDraw.Draw(sheet)
    for index, step in enumerate(steps):
        _rgb('step image', step.image)
        thumb = Image.fromarray(step.image)
        thumb.thumbnail((cell_size - 12, cell_size - 24), Image.Resampling.LANCZOS)
        x, y = (index % columns) * cell_size, (index // columns) * cell_size
        sheet.paste(
            thumb,
            (x + (cell_size - thumb.width) // 2, y + 20 + (cell_size - 24 - thumb.height) // 2),
        )
        draw.text((x + 6, y + 4), str(index + 1), fill='black')
    return sheet
