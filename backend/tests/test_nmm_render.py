"""Synthetic pixel-level contracts for the offline renderer."""

import cv2
import numpy as np
import pytest
from skimage.color import deltaE_ciede2000, rgb2lab

from app.nmm.render import RenderParams, make_contact_sheet, render_steps


def scene():
    original = np.full((24, 40, 3), (55, 65, 75), dtype=np.uint8)
    final = np.zeros_like(original)
    for i, color in enumerate(
        [(25, 28, 30), (80, 82, 85), (130, 133, 135), (185, 188, 190), (250, 250, 250)]
    ):
        final[:, i * 8 : (i + 1) * 8] = color
    mask = np.zeros(original.shape[:2], dtype=bool)
    mask[3:-3, 3:-3] = True
    return original, final, mask


def test_composite_nested_layers_real_diff_and_input_preservation():
    original, final, mask = scene()
    snapshots = [x.copy() for x in (original, final, mask)]
    result = render_steps(original, final, mask)
    assert len(result.steps) == 7
    assert [s.kind for s in result.steps] == ['base'] * 5 + ['transition', 'final']
    previous = original
    for step in result.steps:
        np.testing.assert_array_equal(step.image[~mask], original[~mask])
        expected = (deltaE_ciede2000(rgb2lab(previous), rgb2lab(step.image)) > 2.0) & mask
        np.testing.assert_array_equal(step.diff_mask, expected)
        assert step.area_ratio == pytest.approx(expected.sum() / mask.sum())
        assert step.diff_mask.dtype == step.paint_mask.dtype == bool
        previous = step.image
    bases = [s for s in result.steps if s.kind == 'base']
    np.testing.assert_array_equal(bases[0].paint_mask, mask)
    for earlier, later in zip(bases, bases[1:]):
        assert not np.any(later.paint_mask & ~earlier.paint_mask)
    transition = result.steps[-2]
    np.testing.assert_array_equal(
        transition.image[~transition.paint_mask], bases[-1].image[~transition.paint_mask]
    )
    expected_final = original.copy()
    expected_final[mask] = final[mask]
    np.testing.assert_array_equal(result.steps[-1].image, expected_final)
    for value, snapshot in zip((original, final, mask), snapshots):
        np.testing.assert_array_equal(value, snapshot)
    assert sum(p.area_ratio for p in result.palette) == pytest.approx(1)
    assert [p.lab[0] for p in result.palette] == sorted(p.lab[0] for p in result.palette)


def test_determinism_and_global_state():
    original, final, mask = scene()
    before = np.random.get_state()
    threads = cv2.getNumThreads()
    cv2.setRNGSeed(9381)
    expected_cv = np.empty((4, 4), dtype=np.float32)
    cv2.randu(expected_cv, 0, 1)
    cv2.setRNGSeed(9381)
    first = render_steps(original, final, mask)
    second = render_steps(original, final, mask)
    actual_cv = np.empty((4, 4), dtype=np.float32)
    cv2.randu(actual_cv, 0, 1)
    np.testing.assert_array_equal(actual_cv, expected_cv)
    after = np.random.get_state()
    assert before[0] == after[0] and before[2:] == after[2:]
    np.testing.assert_array_equal(before[1], after[1])
    assert cv2.getNumThreads() == threads
    assert first.palette == second.palette and first.warnings == second.warnings
    for a, b in zip(first.steps, second.steps):
        np.testing.assert_array_equal(a.image, b.image)
        np.testing.assert_array_equal(a.diff_mask, b.diff_mask)


def test_rare_boundary_highlight_and_thin_line_survive_sampling():
    original = np.full((120, 120, 3), 70, dtype=np.uint8)
    final = np.full_like(original, 95)
    mask = np.zeros((120, 120), dtype=bool)
    mask[10:110, 10:110] = True
    final[10, 10] = 255  # Less than 0.02%, removed by ordinary erosion.
    result = render_steps(original, final, mask, params=RenderParams(max_samples=32))
    assert len(result.palette) == 2
    highlight = result.palette[-1]
    assert highlight.lab[0] > 99 and highlight.area_ratio == pytest.approx(1 / 10000)
    assert result.steps[1].paint_mask[10, 10]
    thin = np.zeros((120, 120), dtype=bool)
    thin[10, 10:110] = True
    thin_result = render_steps(original, final, thin)
    np.testing.assert_array_equal(thin_result.steps[0].paint_mask, thin)
    assert len(thin_result.palette) == 2


def test_single_color_full_mask_one_pixel_and_resize():
    original = np.full((8, 12, 3), 30, dtype=np.uint8)
    final = np.full((3, 4, 3), (110, 120, 130), dtype=np.uint8)
    for mask in (np.ones((8, 12), dtype=bool), np.eye(8, 12, dtype=bool)):
        result = render_steps(original, final, mask)
        assert len(result.palette) == 1 and len(result.steps) == 3
        assert result.warnings
        np.testing.assert_array_equal(
            result.steps[-1].image[mask], np.broadcast_to([110, 120, 130], (mask.sum(), 3))
        )
    one = np.zeros((8, 12), dtype=bool)
    one[0, 0] = True
    result = render_steps(original, final, one)
    assert result.palette[0].area_ratio == 1
    assert np.isfinite(result.palette[0].lab).all()


def test_details_are_replaced_once_and_lower_layers_never_darken_again():
    original, final, mask = scene()
    original[::2, :, :] = 100
    result = render_steps(original, final, mask, params=RenderParams(detail_strength=0.6))
    bases = result.steps[:-2]
    for previous, current in zip(bases, bases[1:]):
        unchanged = mask & ~current.paint_mask
        np.testing.assert_array_equal(current.image[unchanged], previous.image[unchanged])
    # Final quantized low layer is exactly its first paint, even after four layers.
    dark = mask & ~bases[1].paint_mask
    np.testing.assert_array_equal(bases[-1].image[dark], bases[0].image[dark])
    no_detail = render_steps(original, final, mask, params=RenderParams(detail_strength=0))
    assert np.any(result.steps[0].image[mask] != no_detail.steps[0].image[mask])


def test_transition_does_not_blend_nonadjacent_levels_or_background():
    original = np.full((16, 30, 3), (255, 0, 0), dtype=np.uint8)
    final = np.zeros_like(original)
    # Dark touches light directly; middle exists in a disconnected component.
    mask = np.zeros((16, 30), dtype=bool)
    mask[2:8, 2:8] = True
    mask[2:8, 8:14] = True
    mask[10:14, 20:28] = True
    final[2:8, 2:8] = 20
    final[2:8, 8:14] = 240
    final[10:14, 20:28] = 120
    result = render_steps(
        original, final, mask, step_count=5, params=RenderParams(detail_strength=0)
    )
    assert not result.steps[-2].paint_mask.any()
    np.testing.assert_array_equal(result.steps[-2].image, result.steps[-3].image)


@pytest.mark.parametrize(
    'field,value',
    [
        ('detail_strength', float('nan')),
        ('detail_sigma', float('inf')),
        ('diff_threshold', -1),
        ('erosion_radius', 1.5),
        ('transition_width', True),
        ('seed', -1),
        ('max_samples', 1),
        ('detail_strength', 100),
        ('detail_sigma', 1000),
        ('transition_width', 1000),
    ],
)
def test_bad_params_are_rejected(field, value):
    with pytest.raises((ValueError, TypeError)):
        RenderParams(**{field: value})


@pytest.mark.parametrize(
    'case',
    [
        'empty',
        'mask_shape',
        'mask_dtype',
        'float_rgb',
        'rgba',
        'zero_image',
        'oversized',
        'bad_steps',
    ],
)
def test_invalid_inputs(case):
    original, final, mask = scene()
    count = 7
    if case == 'empty':
        mask[:] = False
    elif case == 'mask_shape':
        mask = mask[:-1]
    elif case == 'mask_dtype':
        mask = mask.astype(np.uint8)
    elif case == 'float_rgb':
        original = original.astype(np.float32)
    elif case == 'rgba':
        final = np.zeros((24, 40, 4), dtype=np.uint8)
    elif case == 'zero_image':
        final = final[:0]
    elif case == 'oversized':
        final = np.broadcast_to(np.uint8(0), (2001, 2000, 3))
    elif case == 'bad_steps':
        count = True
    with pytest.raises((ValueError, TypeError)):
        render_steps(original, final, mask, step_count=count)


def test_boundary_params_zero_transition_and_contact_sheet_order():
    original, final, mask = scene()
    result = render_steps(
        original,
        final,
        mask,
        step_count=4,
        params=RenderParams(
            detail_strength=0,
            detail_sigma=0,
            erosion_radius=0,
            transition_width=0,
            diff_threshold=0,
            seed=0,
            max_samples=8,
        ),
    )
    np.testing.assert_array_equal(result.steps[-2].image, result.steps[-3].image)
    snapshots = [s.image.copy() for s in result.steps]
    sheet = make_contact_sheet(result.steps, columns=2, cell_size=64)
    assert sheet.mode == 'RGB' and sheet.size == (128, 128)
    image = np.asarray(sheet)
    assert np.any(image[:20, :20] != 255)  # Visible step number in top left.
    for step, snapshot in zip(result.steps, snapshots):
        np.testing.assert_array_equal(step.image, snapshot)
    with pytest.raises(ValueError):
        make_contact_sheet([])


def test_transition_width_one_operates_only_on_touching_adjacent_pixels():
    original = np.full((12, 30, 3), (255, 0, 0), dtype=np.uint8)
    final = np.zeros_like(original)
    final[:, :10] = 20
    final[:, 10:20] = 120
    final[:, 20:] = 240
    mask = np.zeros((12, 30), dtype=bool)
    mask[2:-2, 2:-2] = True
    result = render_steps(
        original,
        final,
        mask,
        step_count=5,
        params=RenderParams(detail_strength=0, transition_width=1),
    )
    transition, prior = result.steps[-2], result.steps[-3]
    expected_band = np.zeros_like(mask)
    expected_band[:, [9, 10, 19, 20]] = True
    expected_band &= mask
    np.testing.assert_array_equal(transition.paint_mask, expected_band)
    assert np.any(transition.image[expected_band] != prior.image[expected_band])
    np.testing.assert_array_equal(transition.image[~expected_band], prior.image[~expected_band])
    # Grayscale input within mask must stay neutral despite the red background.
    assert np.max(np.ptp(transition.image[mask].astype(int), axis=1)) <= 1


def test_contact_sheet_row_major_order_and_aspect_ratio():
    from app.nmm.render import StepRender

    colors = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]
    steps = []
    for color in colors:
        image = np.full((10, 20, 3), color, dtype=np.uint8)
        blank = np.zeros((10, 20), dtype=bool)
        steps.append(StepRender(image, blank, blank, None, 0, 'final'))
    sheet = np.asarray(make_contact_sheet(steps, columns=3, cell_size=80))
    assert sheet.shape == (160, 240, 3)
    for index, color in enumerate(colors):
        y, x = (index // 3) * 80, (index % 3) * 80
        np.testing.assert_array_equal(sheet[y + 48, x + 40], color)
        np.testing.assert_array_equal(sheet[y + 22, x + 40], [255, 255, 255])
        assert np.any(sheet[y : y + 18, x : x + 22] < 255)
    assert np.all(sheet[80:, 80:] == 255)


@pytest.mark.parametrize('count', [4, 9])
def test_requested_step_bounds_and_noncontiguous_arrays(count):
    original, final, mask = scene()
    result = render_steps(original[:, ::-1], final[:, ::-1], mask[:, ::-1], step_count=count)
    assert len(result.steps) <= count
    np.testing.assert_array_equal(
        result.steps[-1].image[mask[:, ::-1]], final[:, ::-1][mask[:, ::-1]]
    )
