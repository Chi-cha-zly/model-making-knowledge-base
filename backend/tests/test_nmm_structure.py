"""Offline structural review and mask-scoped analysis regressions."""

import json

import numpy as np
import pytest
from pydantic import ValidationError

from app.api.nmm import ConfirmInput, RunInput
from app.nmm import prompts, workflow_schemas


def analysis(surfaces):
    return dict(
        surfaces=surfaces,
        suggested_light_direction=dict(direction='top', basis='invented knees', conflict=False),
        reflection_hints=['invented knees'],
        uncertain=['invented knees'],
        mask_issues=[],
        summary='invented knees',
        warnings=[],
    )


def surface():
    return dict(
        name='shoulder',
        shape='convex',
        position='top',
        notes='visible',
        bbox=[0.1, 0.01, 0.4, 0.06],
        confidence=0.9,
        reflection_hint='thin highlight',
    )


def test_surface_schema_accepts_grounding_and_filters_nonfinite_boxes():
    raw = surface()
    parsed = workflow_schemas.Surface.model_validate(raw)
    assert parsed.bbox == [0.1, 0.01, 0.4, 0.06]
    for bbox in ([0, 0, float('nan'), 1], [0, 0, 2, 1], [0, 1, 1, 0], [0, 0, 1]):
        assert workflow_schemas.Surface.model_validate({**raw, 'bbox': bbox}).bbox is None


def test_mask_scoping_removes_fabricated_lower_body_and_legacy_prose():
    from app.nmm.analysis_scope import scope_analysis, mask_scope

    mask = np.zeros((1000, 200), dtype=bool)
    mask[:74] = True
    values = [
        surface(),
        {**surface(), 'name': 'knees', 'bbox': [0.1, 0.7, 0.4, 0.9]},
        {**surface(), 'name': 'legacy', 'bbox': None},
        {**surface(), 'name': 'uncertain', 'confidence': 0.1},
        {**surface(), 'name': 'partial', 'bbox': [0.1, 0.05, 0.4, 0.11]},
    ]
    scoped = scope_analysis(analysis(values), mask)
    assert [s['name'] for s in scoped['surfaces']] == ['shoulder']
    assert scoped['warnings']
    context = json.loads(prompts.generation_prompt({}, scoped))
    assert 'invented knees' not in json.dumps(context)
    assert mask_scope(mask)['coverage'] == 0.074
    assert mask_scope(mask)['bbox'] == [0.0, 0.0, 1.0, 0.074]


def structural_fixture():
    original = np.full((128, 128, 3), 30, dtype=np.uint8)
    original[15:50, 15:45] = 180
    original[70:110, 65:100] = 230
    mask = np.ones((128, 128), dtype=bool)
    return original, mask


def test_quality_identity_recolor_never_auto_pass_and_destruction_blocks():
    from app.nmm.candidate_quality import check_candidate

    original, mask = structural_fixture()
    for candidate in (original, 255 - original):
        check = check_candidate(original, candidate, mask)
        assert check['status'] == 'needs_review'
        assert check['version'] and check['reasons']
    for candidate in (np.full_like(original, 30), np.roll(original, 15, axis=1)):
        assert check_candidate(original, candidate, mask)['status'] == 'blocked'
    textureless = np.full_like(original, 100)
    assert check_candidate(textureless, textureless, mask)['status'] == 'needs_review'


def test_quality_rejects_unbounded_or_mismatched_inputs():
    from app.nmm.candidate_quality import check_candidate

    original, mask = structural_fixture()
    with pytest.raises(ValueError):
        check_candidate(original, original[:2], mask)
    with pytest.raises(ValueError):
        check_candidate(original, original, mask.astype(np.uint8))


def test_quality_confirmation_and_resume_use_strict_explicit_boolean():
    for schema, fields in (
        (ConfirmInput, {'action': 'select'}),
        (RunInput, {'confirm_generation': False}),
    ):
        base = {'request_id': 'x', 'revision': 1, **fields}
        assert schema.model_validate(base).quality_acknowledged is False
        assert (
            schema.model_validate({**base, 'quality_acknowledged': True}).quality_acknowledged
            is True
        )
        with pytest.raises(ValidationError):
            schema.model_validate({**base, 'quality_acknowledged': 1})


def test_tiny_boundary_mask_is_reviewable_without_edge_evidence():
    from app.nmm.candidate_quality import check_candidate

    original = np.zeros((2000, 1000, 3), dtype=np.uint8)
    mask = np.zeros(original.shape[:2], dtype=bool)
    mask[0, 0] = True
    check = check_candidate(original, original, mask)
    assert check['status'] == 'needs_review'
    assert check['metrics']['source_edge_pixels'] == 0


def test_removing_one_discrete_part_is_blocked_even_with_other_edges_retained():
    from app.nmm.candidate_quality import check_candidate

    original, mask = structural_fixture()
    candidate = original.copy()
    candidate[15:50, 15:45] = 30
    assert check_candidate(original, candidate, mask)['status'] == 'blocked'


def test_context_crop_has_explicit_original_coordinate_mapping():
    from app.nmm.analysis_scope import analysis_images
    from test_nmm_structure_workflow import image_input

    original = np.zeros((1000, 200, 3), dtype=np.uint8)
    mask = np.zeros((1000, 200), dtype=np.uint8)
    mask[:74] = 255
    images, scope, actual_mask = analysis_images(image_input(original), image_input(mask))
    assert len(images) == 3 and actual_mask.sum() == 14800
    assert scope['context_crop_original_pixels'] == [0, 0, 200, 94]
    assert scope['context_crop_size'] == [200, 94]
